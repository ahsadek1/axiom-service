"""
alerts.py — Unified alert routing to Ahmed. Deduped, cooldown-protected.
Spec: NEXUS_RESILIENCE_BASE v1.1 (Cipher, 2026-05-02)
Status: Zero conflicts — replaces scattered direct Telegram bot calls.

Rule: alert_ahmed() is the ONE path for agent-to-Ahmed Telegram alerts.
Never call the bot API directly from service code.

Dedup: key= gives a per-alert-type cooldown (default 30 min).
       key=None → always sends (one-time critical events only).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger("nexus.alerts")

# In-process dedup store — resets on process restart (intentional).
# Persistent outage survivors should use key=None or distinct keys.
_alert_history: dict[str, datetime] = {}

ALERT_COOLDOWN = timedelta(minutes=30)

# Ahmed's Telegram chat ID (personal DM).
AHMED_CHAT_ID = "8573754783"

# BOT_TOKEN resolution order:
#   1. CIPHER_BOT_TOKEN env var (set in plist EnvironmentVariables)
#   2. Hard-coded fallback token from IDENTITY.md
#
# The fallback exists so alert delivery never silently fails just because
# a service doesn't have the env var set. It does NOT bypass cooldown logic.
_FALLBACK_BOT_TOKEN = "8736004775:AAG3v_7tcXk8SXh5whgpKRT3Dr3-C71VtQI"


def _get_bot_token() -> str:
    return os.environ.get("CIPHER_BOT_TOKEN", "") or _FALLBACK_BOT_TOKEN


def alert_ahmed(
    message: str,
    key: Optional[str] = None,
    severity: str = "WARNING",
) -> bool:
    """
    Send an alert to Ahmed via Telegram. Deduped by *key* within a 30-min
    cooldown window.

    Args:
        message  — alert body text (plain or Markdown-safe)
        key      — dedup key; same key won't fire again for 30 min.
                   Pass key=None for one-time critical events that must
                   always deliver (e.g. service startup failures).
        severity — "INFO" | "WARNING" | "CRITICAL"

    Returns:
        True  — message sent
        False — suppressed by cooldown, or delivery failed
    """
    if key:
        last = _alert_history.get(key)
        if last and (datetime.utcnow() - last) < ALERT_COOLDOWN:
            logger.debug("Alert suppressed (cooldown active): key=%s", key)
            return False
        _alert_history[key] = datetime.utcnow()

    prefix = {"INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🔴"}.get(severity, "⚠️")
    text = f"{prefix} *[{severity}]*\n{message}"

    token = _get_bot_token()
    if not token:
        logger.error(
            "No bot token available — alert not delivered: %s", text[:100]
        )
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": AHMED_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=5,
        )
        resp.raise_for_status()
        logger.info("Alert sent [%s] key=%s", severity, key)
        return True
    except Exception as e:
        logger.error(
            "Alert delivery failed: %s — %s", message[:80], e
        )
        return False


def reset_cooldown(key: str) -> None:
    """
    Clear the cooldown for a given key. Useful after a problem is resolved
    so the next recurrence fires immediately.

    Example:
        reset_cooldown("axiom_skip_threshold")
    """
    _alert_history.pop(key, None)


def reset_cooldowns() -> None:
    """
    Clear ALL active cooldowns. Used in tests and after full system restarts.
    """
    _alert_history.clear()


def cooldown_remaining(key: str) -> int:
    """
    Returns seconds remaining on a key's cooldown, or 0 if not active.
    Useful for health endpoint diagnostics.
    """
    last = _alert_history.get(key)
    if not last:
        return 0
    elapsed = (datetime.utcnow() - last).total_seconds()
    remaining = ALERT_COOLDOWN.total_seconds() - elapsed
    return max(0, int(remaining))
