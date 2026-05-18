"""
genesis/alerts/alert_dispatcher.py — Core Alert Send Handler
=============================================================

Entry point for sending alerts to Ahmed via Telegram.
Generates unique message IDs for ACK tracking.
Handles network failures with fallback to bus.

Usage:
    from genesis.alerts.alert_dispatcher import send_alert

    msg_id = send_alert(
        title="FATAL: Execution halted",
        body="401 auth from Alpaca — manual action required",
        tier="ESCALATE",
        agent="alpha-execution"
    )
    # Returns message ID for ACK tracking

Author: GENESIS 🌱
Date: 2026-05-16
"""

import logging
import os
import time
import uuid
from typing import Optional

import requests

logger = logging.getLogger("genesis.alerts.dispatcher")

# ── Configuration ────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c")
AHMED_CHAT_ID = os.getenv("AHMED_CHAT_ID", "8573754783")
BUS_URL = os.getenv("NEXUS_BUS_URL", "http://192.168.1.141:9999")
TELEGRAM_TIMEOUT_S = 5
BUS_TIMEOUT_S = 3

# ── Message ID generation ────────────────────────────────────────────────────

_MESSAGE_ID_COUNTER: int = 0
_MESSAGE_ID_LOCK = __import__("threading").Lock()


def _generate_message_id() -> str:
    """
    Generate a unique, sortable message ID for ACK tracking.
    Format: genesis-<timestamp>-<uuid>
    """
    global _MESSAGE_ID_COUNTER
    with _MESSAGE_ID_LOCK:
        _MESSAGE_ID_COUNTER += 1
        ts = int(time.time())
        return f"genesis-{ts}-{_MESSAGE_ID_COUNTER}"


# ── Telegram send ────────────────────────────────────────────────────────────

def _send_telegram(title: str, body: str, agent: str, tier: str) -> Optional[str]:
    """
    Send alert via Telegram direct to Ahmed.

    Returns:
        Message ID if sent successfully, None on failure.
    """
    message_id = _generate_message_id()
    text = (
        f"🚨 <b>{title}</b>\n"
        f"<i>Agent: {agent.upper()}</i>\n"
        f"<i>Tier: {tier}</i>\n"
        f"<i>ID: {message_id}</i>\n\n"
        f"{body}"
    )

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": AHMED_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=TELEGRAM_TIMEOUT_S,
        )
        resp.raise_for_status()
        logger.info(
            "alert_dispatcher: Telegram sent — id=%s title=%s agent=%s",
            message_id, title[:40], agent,
        )
        return message_id

    except requests.exceptions.Timeout:
        logger.warning("alert_dispatcher: Telegram timeout — falling back to bus")
        return None
    except requests.exceptions.RequestException as e:
        logger.warning("alert_dispatcher: Telegram failed — %s", e)
        return None


# ── Bus fallback ─────────────────────────────────────────────────────────────

def _send_bus(title: str, body: str, agent: str, tier: str) -> Optional[str]:
    """
    Send alert via message bus as fallback.

    Returns:
        Message ID if sent successfully, None on failure.
    """
    message_id = _generate_message_id()
    message = (
        f"[ALERT-FALLBACK] {tier}: {title}\n"
        f"Agent: {agent.upper()}\n"
        f"ID: {message_id}\n"
        f"{body}"
    )

    try:
        resp = requests.post(
            f"{BUS_URL}/send",
            json={
                "from": "genesis-alerts",
                "to": "sovereign",
                "message": message,
            },
            timeout=BUS_TIMEOUT_S,
        )
        resp.raise_for_status()
        logger.info(
            "alert_dispatcher: Bus sent (fallback) — id=%s title=%s agent=%s",
            message_id, title[:40], agent,
        )
        return message_id

    except requests.exceptions.RequestException as e:
        logger.warning("alert_dispatcher: Bus fallback also failed — %s", e)
        return None


# ── Public API ───────────────────────────────────────────────────────────────

def send_alert(
    title: str,
    body: str,
    agent: str = "genesis",
    tier: str = "ESCALATE",
) -> Optional[str]:
    """
    Send alert to Ahmed via Telegram with automatic fallback to bus.

    Primary: Telegram direct DM
    Fallback: NEXUS message bus → SOVEREIGN

    Args:
        title:  Short one-line summary (max 80 chars for Telegram readability).
        body:   Full message body.
        agent:  Source agent name (default: "genesis").
        tier:   Alert tier: "ESCALATE", "WARN", "INFO" (default: "ESCALATE").

    Returns:
        Unique message ID for ACK tracking, or None if both channels failed.

    Examples:
        # Simple alert
        msg_id = send_alert(
            title="Buffer stall detected",
            body="Alpha concordance queue >500 items. Check Axiom."
        )

        # With full context
        msg_id = send_alert(
            title="FATAL: Execution auth failed",
            body="401 Unauthorized from Alpaca. Manual intervention required.",
            agent="alpha-execution",
            tier="ESCALATE"
        )
    """
    if not title:
        raise ValueError("Alert title cannot be empty")

    # Try Telegram first
    msg_id = _send_telegram(title, body, agent, tier)
    if msg_id:
        return msg_id

    # Fall back to bus
    logger.warning("alert_dispatcher: Telegram failed, routing to bus fallback")
    return _send_bus(title, body, agent, tier)


def get_message_id_prefix() -> str:
    """Return the prefix used for generated message IDs (for filtering/parsing)."""
    return "genesis-"
