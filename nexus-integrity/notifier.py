"""
notifier.py — Telegram alert notifier for nexus-integrity.

Alert tiering (V9 amendment — derived from TRS sub-component weights):
  P0: TRS=0 or store dead → immediate page Ahmed, no suppression
  P1: TRS < GREEN threshold → Health Group alert, ACK within 15 min
      Suppress repeat until state changes
  P2: Single probe failure, TRS still GREEN → Health Group alert, once per failure
      Suppress 30 min per unique failure key
  P3: Anomaly detected, system healthy → CHRONICLE only, no alert
  P4: Informational → daily digest only, no immediate alert

Routing hierarchy:
  SOVEREIGN = primary operational recipient
  Ahmed = P0 only (decision-level), never routed routine reports
  Health Group = P1/P2 alerts
"""

import logging
import time
from typing import Dict, Optional

import requests

import config
from models import AlertTier

logger = logging.getLogger("integrity.notifier")

# ---------------------------------------------------------------------------
# Suppression state
# ---------------------------------------------------------------------------

_suppression: Dict[str, float] = {}   # key → expiry timestamp
_last_p1_reason: Optional[str] = None
_P2_SUPPRESSION_S: int = 1800   # 30 min

# ---------------------------------------------------------------------------
# C2 — CHRONICLE-backed cooldown registry (Cipher Amendment)
# Prevents alert fatigue from repeated identical failures.
# Cooldowns stored in monitoring_thresholds so Ahmed can tune without redeploy.
# ---------------------------------------------------------------------------

# Default cooldowns (seconds) — overridden by CHRONICLE monitoring_thresholds
_DEFAULT_COOLDOWNS: Dict[str, int] = {
    "P0": 0,       # P0 always fires — no suppression ever
    "P1": 1200,    # 20 min
    "P2": 3600,    # 60 min
    "P3": 14400,   # 240 min
}
_cooldown_cache: Dict[str, int] = dict(_DEFAULT_COOLDOWNS)
_cooldown_cache_ts: float = 0.0
_COOLDOWN_REFRESH_S: int = 300   # Refresh from CHRONICLE every 5 min


def _get_cooldowns() -> Dict[str, int]:
    """Get current alert cooldowns, refreshing from CHRONICLE if stale."""
    global _cooldown_cache, _cooldown_cache_ts
    now = time.time()
    if now - _cooldown_cache_ts < _COOLDOWN_REFRESH_S:
        return _cooldown_cache
    try:
        import requests as _req
        import config as _cfg
        resp = _req.get(
            f"{_cfg.CHRONICLE_URL}/governance/thresholds",
            headers={"X-Chronicle-Secret": getattr(_cfg, 'CHRONICLE_SECRET', '')},
            timeout=2.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            # CHRONICLE stores alert_cooldown_P0, alert_cooldown_P1 etc.
            for tier in ("P0", "P1", "P2", "P3"):
                key = f"alert_cooldown_{tier}"
                if key in data:
                    _cooldown_cache[tier] = int(data[key])
            _cooldown_cache_ts = now
    except Exception:
        pass  # Use cached defaults — never block on CHRONICLE read
    return _cooldown_cache


def _is_in_cooldown(alert_key: str, tier_str: str) -> bool:
    """Check if alert is in cooldown. P0 is never in cooldown."""
    if tier_str == "P0":
        return False
    return _is_suppressed(alert_key)


def _set_cooldown(alert_key: str, tier_str: str) -> None:
    """Set cooldown for an alert key based on tier."""
    cooldowns = _get_cooldowns()
    duration = cooldowns.get(tier_str, _DEFAULT_COOLDOWNS.get(tier_str, 0))
    if duration > 0:
        _suppress(alert_key, duration)


def _is_suppressed(key: str) -> bool:
    """Check if an alert key is currently suppressed.

    Args:
        key: Unique alert key.

    Returns:
        True if suppressed (should not alert), False if alert should fire.
    """
    expiry = _suppression.get(key)
    if expiry is None:
        return False
    return time.time() < expiry


def _suppress(key: str, duration_s: int) -> None:
    """Suppress an alert key for a duration.

    Args:
        key: Unique alert key.
        duration_s: Suppression duration in seconds.
    """
    _suppression[key] = time.time() + duration_s


MESSAGE_BUS_URL: str = "http://192.168.1.141:9999"


def _notify_genesis_via_bus(title: str, detail: str, tier: str) -> None:
    """Post a P1/P0 alert to the Nexus message bus.

    GENESIS-FIX-NOTIFIER-001 2026-04-30: Added bus routing for P1 alerts.
    GENESIS-FIX-NOTIFIER-002 2026-05-01: Rerouted from GENESIS to SOVEREIGN.

    Root cause of repeated unresolved alerts: alerts were routed to GENESIS
    which is NOT always-on (only active during open chat sessions). Alerts
    fired, sat in GENESIS inbox unread for hours, system degraded with no
    intervention. SOVEREIGN is always-on and owns operational response.
    SOVEREIGN acts immediately on P1s and calls GENESIS only for code changes.

    Args:
        title:  Alert title.
        detail: Alert detail.
        tier:   Alert tier string ("P0" or "P1").
    """
    try:
        requests.post(
            f"{MESSAGE_BUS_URL}/send",
            json={
                "from": "nexus-integrity",
                "to": "sovereign",
                "message": f"{tier} ALERT — {title}: {detail[:300]}",
            },
            timeout=3,
        )
        logger.info("Bus: %s alert routed to SOVEREIGN: %s", tier, title)
    except Exception as exc:
        logger.warning("Bus: failed to notify SOVEREIGN (%s): %s", title, exc)


def _send_telegram(chat_id: str, message: str) -> bool:
    """Send a Telegram message.

    Args:
        chat_id: Telegram chat ID.
        message: Message text (Markdown supported).

    Returns:
        True if sent successfully, False otherwise.
    """
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10.0)
        if resp.status_code == 200:
            return True
        logger.error("Telegram send failed (%d): %s", resp.status_code, resp.text[:100])
        return False
    except requests.Timeout:
        logger.error("Telegram send timed out")
        return False
    except requests.RequestException as e:
        logger.error("Telegram send error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Public alert methods
# ---------------------------------------------------------------------------

def send_alert(
    tier: AlertTier,
    title: str,
    detail: str = "",
    suppression_key: Optional[str] = None,
    body: str = "",   # C2: alias for detail (used by addendum callers)
) -> None:
    # C2: body is an alias for detail
    if body and not detail:
        detail = body
    """Route and send an alert based on tier.

    P0: Ahmed + Health Group (no suppression)
    P1: Health Group (suppressed until state changes)
    P2: Health Group (suppressed 30 min per key)
    P3: CHRONICLE only (no Telegram alert)
    P4: No alert

    Args:
        tier: Alert severity tier.
        title: Short alert title.
        detail: Full alert detail.
        suppression_key: Unique key for suppression tracking (P1/P2 only).
    """
    global _last_p1_reason

    if tier == AlertTier.P4:
        logger.debug("P4 alert (digest only): %s", title)
        return

    if tier == AlertTier.P3:
        logger.info("P3 alert (CHRONICLE only): %s — %s", title, detail)
        return

    if tier == AlertTier.P2:
        key = suppression_key or f"P2:{title}"
        if _is_in_cooldown(key, "P2"):  # C2: CHRONICLE-backed cooldown
            logger.debug("P2 alert in cooldown (%s)", key)
            return
        _set_cooldown(key, "P2")  # C2
        message = _format_message("⚠️ P2", title, detail)
        _send_telegram(config.TELEGRAM_HEALTH_GROUP_CHAT_ID, message)
        logger.info("P2 alert sent to Health Group: %s", title)
        return

    if tier == AlertTier.P1:
        key = suppression_key or f"P1:{title}"
        current_reason = detail[:50]
        # C2: CHRONICLE-backed cooldown — suppress if state unchanged AND in cooldown
        if _last_p1_reason == current_reason and _is_in_cooldown(key, "P1"):
            logger.debug("P1 alert in cooldown (state unchanged): %s", key)
            return
        _last_p1_reason = current_reason
        _set_cooldown(key, "P1")  # C2: use CHRONICLE-configured cooldown
        message = _format_message("🟡 P1", title, detail)
        _send_telegram(config.TELEGRAM_HEALTH_GROUP_CHAT_ID, message)
        logger.info("P1 alert sent to Health Group: %s", title)
        # GENESIS-FIX-NOTIFIER-001 2026-04-30: Wire GENESIS as direct P1 escalation target.
        # P1 alerts previously went only to the health group — no agent owned them.
        # Now: post to message bus so GENESIS session receives and acts immediately.
        _notify_genesis_via_bus(title, detail, "P1")
        return

    if tier == AlertTier.P0:
        # P0: No suppression — page Ahmed AND Health Group
        message = _format_message("🚨 P0 CRITICAL", title, detail)
        _send_telegram(config.TELEGRAM_AHMED_CHAT_ID, message)
        _send_telegram(config.TELEGRAM_HEALTH_GROUP_CHAT_ID, message)
        logger.critical("P0 alert sent to Ahmed + Health Group: %s", title)
        return


def send_trs_alert(score: float, tier: AlertTier, reason: str) -> None:
    """Send a TRS-derived alert.

    Args:
        score: Current TRS score.
        tier: Alert tier derived from TRS.
        reason: TRS reason string.
    """
    title = f"TRS={score:.1f} — Trading {'BLOCKED' if tier == AlertTier.P0 else 'DEGRADED'}"
    detail = (
        f"*Score:* {score:.1f}/100\n"
        f"*Tier:* {tier.value}\n"
        f"*Reason:* {reason}\n"
        f"*Time:* {_now_str()}\n"
        f"*Service:* nexus-integrity"
    )
    send_alert(tier, title, detail, suppression_key=f"trs:{tier.value}")


def send_flow_stage_escalation(stage: int, stage_name: str, detail: str) -> None:
    """Send a flow stage escalation alert (fix attempted but recovery assert failed).

    Routes to SOVEREIGN (operational), not Ahmed.

    Args:
        stage: Stage number (1-5).
        stage_name: Stage name.
        detail: Failure detail.
    """
    # Escape underscores in dynamic fields — Telegram Markdown V1 treats _ as italic delimiters
    _safe_stage = stage_name.replace("_", "\\_")
    _safe_detail = detail.replace("_", "\\_")
    message = (
        f"🔴 *NEXUS-INTEGRITY: Stage {stage} ESCALATED*\n\n"
        f"*Stage:* {_safe_stage}\n"
        f"*Status:* Auto-fix attempted, recovery assertion FAILED\n"
        f"*Detail:* {_safe_detail}\n"
        f"*Time:* {_now_str()}\n"
        f"*Action required:* SOVEREIGN — manual intervention needed"
    )
    # Route to SOVEREIGN (operational) via Health Group — not Ahmed
    _send_telegram(config.TELEGRAM_HEALTH_GROUP_CHAT_ID, message)
    logger.warning("Flow stage %d escalated to SOVEREIGN: %s", stage, detail)


def _format_message(prefix: str, title: str, detail: str) -> str:
    """Format an alert message with consistent structure.

    Args:
        prefix: Tier prefix (e.g. '🚨 P0 CRITICAL').
        title: Alert title.
        detail: Alert detail body.

    Returns:
        Formatted Telegram message string.
    """
    # Escape underscores in title — Telegram Markdown V1 uses _ as italic delimiter
    _safe_title = title.replace("_", "\\_")
    return (
        f"{prefix} — *{_safe_title}*\n\n"
        f"{detail}\n\n"
        f"_nexus-integrity @ {_now_str()}_"
    )


def _now_str() -> str:
    """Return current time as a human-readable ET string.

    Returns:
        Formatted time string.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S UTC")


def clear_suppression(key: str) -> None:
    """Clear suppression for a key (e.g. when state changes back to healthy).

    Args:
        key: Suppression key to clear.
    """
    _suppression.pop(key, None)
    logger.debug("Suppression cleared for key: %s", key)
