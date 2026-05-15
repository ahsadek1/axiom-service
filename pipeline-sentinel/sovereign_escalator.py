"""
sovereign_escalator.py — SENTINEL cross-machine SOVEREIGN escalation routing.

SENTINEL escalation protocol (Ahmed Sadek, April 22 2026):
  Route → SOVEREIGN via NNS message bus (192.168.1.141:9999)
  Fallback → Nexus V2 Telegram group (-1003579956463)
  NEVER → openclaw sessions_send — cross-gateway, architecturally broken

This module replaces any sessions_send-based escalation paths.
"""

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger("sentinel.sovereign_escalator")

# ── Config (from env) ─────────────────────────────────────────────────────────

BUS_URL            = os.environ.get("NNS_BUS_URL",          "http://192.168.1.141:9999")
BUS_SEND_PATH      = "/send"
BUS_TIMEOUT_S      = float(os.environ.get("BUS_TIMEOUT_S",  "5"))

# Fallback: Nexus V2 Telegram group
FALLBACK_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN",   "")
FALLBACK_CHAT_ID   = os.environ.get("TELEGRAM_V2_GROUP_ID", "-1003579956463")

# Dedup window per escalation key (prevents bus floods)
_DEDUP_WINDOW_S    = 120   # 2 minutes between identical escalations
_last_escalated:   dict = {}  # key → unix ts


def escalate_to_sovereign(
    message: str,
    level: str = "WARNING",
    dedup_key: Optional[str] = None,
) -> bool:
    """
    Route an escalation to SOVEREIGN via NNS message bus.

    Falls back to Nexus V2 Telegram group if the bus is unreachable.
    Never raises — monitoring must never crash the sentinel service.

    Args:
        message:    Human-readable escalation text.
        level:      Escalation level: WARNING, ERROR, CRITICAL.
        dedup_key:  Optional dedup key. Same key within DEDUP_WINDOW_S is skipped.

    Returns:
        True if escalation was delivered (bus or fallback), False if both failed.
    """
    now = time.time()

    if dedup_key:
        last = _last_escalated.get(dedup_key, 0.0)
        if now - last < _DEDUP_WINDOW_S:
            logger.debug(
                "Escalation suppressed (dedup): key=%s last_sent=%.0fs ago",
                dedup_key, now - last,
            )
            return True  # Suppressed counts as "delivered" (already sent recently)

    # ── Primary: NNS message bus ──────────────────────────────────────────────
    bus_ok = _send_via_bus(f"[SENTINEL] {level}: {message}")
    if bus_ok:
        if dedup_key:
            _last_escalated[dedup_key] = now
        return True

    # ── Fallback: Nexus V2 Telegram group ────────────────────────────────────
    logger.warning("Bus unreachable — falling back to Telegram V2 group")
    tg_ok = _send_via_telegram(f"🔴 SENTINEL {level}\n{message}")
    if tg_ok:
        if dedup_key:
            _last_escalated[dedup_key] = now
        return True

    logger.error(
        "SENTINEL escalation FAILED (bus + Telegram both down): [%s] %s",
        level, message,
    )
    return False


def _send_via_bus(message: str) -> bool:
    """
    POST message to SOVEREIGN via NNS message bus.

    Returns True on success (2xx response), False on any error.
    """
    if not BUS_URL:
        logger.debug("NNS_BUS_URL not configured — skipping bus escalation")
        return False

    try:
        resp = requests.post(
            f"{BUS_URL}{BUS_SEND_PATH}",
            json={"from": "sentinel", "to": "sovereign", "message": message},
            timeout=BUS_TIMEOUT_S,
        )
        if resp.status_code < 300:
            logger.info("Escalated to SOVEREIGN via bus: %s", message[:120])
            return True
        logger.warning("Bus escalation returned %d", resp.status_code)
        return False
    except Exception as exc:
        logger.warning("Bus escalation failed: %s", exc)
        return False


def _send_via_telegram(message: str) -> bool:
    """
    Send escalation to Nexus V2 Telegram group as fallback.

    Returns True on success, False on any error.
    """
    if not FALLBACK_BOT_TOKEN:
        logger.debug("TELEGRAM_BOT_TOKEN not configured — skipping Telegram fallback")
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{FALLBACK_BOT_TOKEN}/sendMessage",
            json={"chat_id": FALLBACK_CHAT_ID, "text": message},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("Escalated to V2 group via Telegram fallback")
            return True
        logger.warning("Telegram fallback returned %d", resp.status_code)
        return False
    except Exception as exc:
        logger.warning("Telegram fallback failed: %s", exc)
        return False
