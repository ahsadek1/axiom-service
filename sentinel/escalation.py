"""
escalation.py — Pipeline Sentinel Escalation Ladder

Tier 1: Auto-heal + Alert #1 to Ahmed
Tier 2: Alert #2 + OpenClaw GENESIS notification (5 min after Tier 1)
Tier 3: OMNI + Cipher AI diagnosis (3 min after Tier 2)
"""

import logging
import subprocess
import sys
import time
from datetime import datetime

import requests

from config import (
    ET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TIER2_WAIT_SECONDS, TIER3_WAIT_SECONDS,
)
from checks import check_service_health, check_execution_health, check_omni_health, get_log_tail
from healer import heal
from omni_consult import run_tier3_consult

log = logging.getLogger("sentinel.escalation")

# Route through alert broker (dedup + rate limiting)
try:
    sys.path.insert(0, "/Users/ahmedsadek/nexus")
    from shared.alert_client import send_alert as _broker_send
    _BROKER_OK = True
except Exception as _be:
    _BROKER_OK = False
    log.warning("escalation: alert_client unavailable, falling back to direct Telegram: %s", _be)


def _now_str() -> str:
    return datetime.now(ET).strftime("%H:%M ET")


# Tier labels → broker levels
_TIER_LEVELS = {1: "WARNING", 2: "WARNING", 3: "CRITICAL"}


def _telegram(text: str, tier: int = 2) -> bool:
    """Send alert through broker (dedup) with direct Telegram fallback."""
    level = _TIER_LEVELS.get(tier, "WARNING")
    # Ahmed DM only for Tier 3; Tier 1/2 go to health group only
    targets = ["ahmed", "nexus_health_group"] if tier >= 3 else ["nexus_health_group"]

    if _BROKER_OK:
        try:
            _broker_send(
                source="sentinel",
                level=level,
                title=text[:200],
                body=text[200:] if len(text) > 200 else "",
                dedup_key=f"sentinel:{text[:80]}",
                targets=targets,
            )
            return True
        except Exception as _be:
            log.warning("escalation: broker send failed: %s", _be)

    # Fallback: direct Telegram to Ahmed
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        log.error("Telegram fallback failed: %s", e)
        return False


def _inject_vector_event(component: str, failure_detail: str, heal_status: str) -> bool:
    """
    Inject an OpenClaw system event to wake VECTOR immediately at Tier 1.
    VECTOR investigates root cause — not just restarts.
    """
    text = (
        f"SENTINEL ALERT — TIER 1: {component} is failing. "
        f"Failure: {failure_detail}. "
        f"Auto-heal result: {heal_status}. "
        f"Investigate root cause immediately. Do not stand by."
    )
    try:
        result = subprocess.run(
            ["openclaw", "system", "event", "--text", text, "--mode", "now"],
            capture_output=True, timeout=15,
        )
        success = result.returncode == 0
        if not success:
            log.error("openclaw vector event failed: %s", result.stderr.decode()[:200])
        return success
    except Exception as e:
        log.error("Failed to inject VECTOR OpenClaw event: %s", e)
        return False


def _inject_genesis_event(component: str, failure_detail: str, log_tail: str) -> bool:
    """
    Inject an OpenClaw system event to notify GENESIS to fix the issue.
    Uses the `openclaw system event` CLI.
    """
    text = (
        f"SENTINEL ESCALATION — TIER 2: Fix {component} immediately. "
        f"Auto-heal failed. Failure: {failure_detail}. "
        f"Last log lines:\n{log_tail[-800:]}"
    )
    try:
        result = subprocess.run(
            ["openclaw", "system", "event", "--text", text, "--mode", "now"],
            capture_output=True, timeout=15,
        )
        success = result.returncode == 0
        if not success:
            log.error("openclaw event failed: %s", result.stderr.decode()[:200])
        return success
    except Exception as e:
        log.error("Failed to inject OpenClaw event: %s", e)
        return False


def _is_component_resolved(component: str) -> bool:
    """Re-check whether the failed component is now healthy."""
    if component.endswith("-picks"):
        # Can't re-check picks in real-time without waiting a full window
        return False
    if component.endswith("-logs"):
        return True   # log-level issue — treat as resolved after restart
    if component.endswith("-execution"):
        system = component.replace("-execution", "")
        passed, _ = check_execution_health(system)
        return passed
    if component == "omni":
        passed, _ = check_omni_health()
        return passed
    passed, _ = check_service_health(component)
    return passed



def _vector_ack_timeout() -> int:
    """
    FLAW 3 fix: Market-aware ack timeout.
    During market hours (09:25–16:15 ET): 90 seconds — P0s can't wait.
    Outside market hours: 300 seconds.
    """
    now = datetime.now(ET)
    market_open  = now.replace(hour=9,  minute=25, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=15, second=0, microsecond=0)
    if market_open <= now <= market_close and now.weekday() < 5:
        return 90    # market hours — 90s, not 5 min
    return 300       # off-hours — 5 min is fine


def _check_vector_ack(component: str, timeout_seconds: int = None) -> bool:
    """
    P1-D: Poll sentinel inbox for a VECTOR ack after a Tier 1 event.
    Returns True if ack received within timeout, False otherwise.
    Polls every 15s (market hours) or 30s (off-hours).
    If ack arrives early, returns immediately.
    """
    import time as _t
    if timeout_seconds is None:
        timeout_seconds = _vector_ack_timeout()
    deadline = _t.time() + timeout_seconds
    poll_interval = 15 if timeout_seconds <= 90 else 30
    log.info("P1-D: Waiting for VECTOR ack for %s (timeout=%ds, poll=%ds)", component, timeout_seconds, poll_interval)

    while _t.time() < deadline:
        try:
            resp = requests.get("http://192.168.1.141:9999/inbox/sentinel", timeout=5)
            if resp.status_code == 200:
                messages = resp.json().get("messages", [])
                for msg in messages:
                    body = msg.get("message", "")
                    if "ACK_TIER1" in body and component in body:
                        log.info("P1-D: VECTOR ack received for %s: %s", component, body)
                        return True
        except Exception as e:
            log.debug("P1-D: ack poll error: %s", e)
        _t.sleep(poll_interval)

    log.warning("P1-D: No VECTOR ack received for %s within %ds — escalating to Tier 2", component, timeout_seconds)
    return False


def run_escalation(component: str, failure_detail: str) -> None:
    """
    Full escalation sequence for a single failed component.

    Tier 1 → Tier 2 → Tier 3 in sequence, stopping when resolved.

    Args:
        component:      Name of the failed component (from checks.py)
        failure_detail: Human-readable failure description
    """
    actions_tried: list[str] = []
    log_tail = ""

    # Gather log context
    agent_name = component.replace("-picks", "").replace("-logs", "")
    if agent_name in ("cipher", "atlas", "sage"):
        log_tail = get_log_tail(agent_name)

    # ── TIER 1 — Auto-Heal ─────────────────────────────────────────────────────
    log.warning("TIER 1 — Attempting auto-heal for %s: %s", component, failure_detail)
    healed, action = heal(component)
    actions_tried.append(action)

    if healed:
        status = "✅ Healed successfully"
        log.info("TIER 1 healed %s via %s", component, action)
    else:
        status = "⚠️ Still failing after heal attempt"
        log.warning("TIER 1 heal FAILED for %s", component)

    _telegram(
        f"⚠️ <b>SENTINEL — Auto-Heal Fired</b>\n"
        f"Component: <code>{component}</code>\n"
        f"Failure: {failure_detail}\n"
        f"Action: {action}\n"
        f"Status: {status}\n"
        f"Time: {_now_str()}"
    )

    # ── VECTOR Wake — fire immediately at Tier 1 regardless of heal outcome ──
    vector_notified = _inject_vector_event(component, failure_detail, status)
    if vector_notified:
        log.info("VECTOR woken via OpenClaw system event for %s", component)
    else:
        log.warning("Failed to wake VECTOR for %s", component)

    if healed:
        return   # Resolved at Tier 1

    # ── TIER 2 — GENESIS Notification ─────────────────────────────────────────
    # P1-D: Wait for VECTOR ack. If ack arrives, VECTOR is actively investigating.
    # Still escalate after TIER2_WAIT_SECONDS regardless — this is a safety net,
    # not a block. If no ack within TIER2_WAIT, escalate immediately (don't wait longer).
    _ack_timeout = _vector_ack_timeout()
    log.warning("TIER 1 failed — waiting for VECTOR ack (timeout=%ds)", _ack_timeout)
    _check_vector_ack(component, timeout_seconds=_ack_timeout)

    if _is_component_resolved(component):
        _telegram(
            f"✅ <b>SENTINEL — Resolved</b>\n"
            f"Component: <code>{component}</code> recovered on its own.\n"
            f"Time: {_now_str()}"
        )
        return

    log.warning("TIER 2 — GENESIS notification for %s", component)
    _telegram(
        f"🚨 <b>SENTINEL — Auto-Heal Failed</b>\n"
        f"Component: <code>{component}</code>\n"
        f"Duration: failing for {TIER2_WAIT_SECONDS // 60 + 1}+ min\n"
        f"GENESIS has been notified and is fixing this.\n"
        f"Time: {_now_str()}"
    )

    genesis_notified = _inject_genesis_event(component, failure_detail, log_tail)
    if not genesis_notified:
        log.error("Failed to notify GENESIS — proceeding to Tier 3 immediately")

    # Monitor for GENESIS resolution (check every 60s for TIER3_WAIT_SECONDS)
    checks_remaining = TIER3_WAIT_SECONDS // 60
    resolved_by_genesis = False
    for i in range(checks_remaining):
        time.sleep(60)
        if _is_component_resolved(component):
            resolved_by_genesis = True
            break
        log.info("TIER 2 monitoring: %d/%d checks done, %s still failing", i + 1, checks_remaining, component)

    if resolved_by_genesis:
        _telegram(
            f"✅ <b>SENTINEL — GENESIS Fixed It</b>\n"
            f"Component: <code>{component}</code> is healthy.\n"
            f"Time: {_now_str()}",
            tier=2,
        )
        return

    # ── TIER 3 — OMNI + Cipher AI Diagnosis ────────────────────────────────────
    log.error("TIER 3 — GENESIS could not fix %s — escalating to OMNI + Cipher", component)
    _telegram(
        f"🆘 <b>SENTINEL — Escalating to OMNI + Cipher</b>\n"
        f"Component: <code>{component}</code>\n"
        f"GENESIS has not resolved this in {(TIER2_WAIT_SECONDS + TIER3_WAIT_SECONDS) // 60} minutes.\n"
        f"Requesting independent AI diagnosis now.\n"
        f"Time: {_now_str()}",
        tier=3,
    )

    result = run_tier3_consult(component, failure_detail, log_tail, actions_tried)

    summary = result["combined_summary"][:1500]   # Telegram limit
    _telegram(
        f"🔬 <b>SENTINEL — AI Diagnosis Complete</b>\n"
        f"Component: <code>{component}</code>\n\n"
        f"<b>Recommendations:</b>\n{summary}\n\n"
        f"Time: {_now_str()}",
        tier=3,
    )

    log.error(
        "TIER 3 complete for %s. OMNI: %s | Cipher: %s",
        component,
        "received" if result["omni_recommendation"] else "no response",
        "received" if result["cipher_recommendation"] else "no response",
    )
