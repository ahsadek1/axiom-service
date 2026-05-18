"""
genesis/alerts/alert_failover.py — Multi-Channel Failover & Escalation
========================================================================

Coordinates retry and escalation logic.
Runs periodic checks for unacknowledged alerts.
Escalates to multiple channels if Ahmed doesn't ACK in time.

Escalation sequence:
    T=0     → Alert sent via primary channel (Telegram DM)
    T=5min  → No ACK? Resend via Telegram
    T=10min → Still no ACK? Page via:
              1. Telegram (second escalation message)
              2. Message bus (SOVEREIGN emergency)
              3. Discord (all team channels)

Usage:
    from genesis.alerts.alert_failover import AlertFailover

    failover = AlertFailover()

    # Call periodically (every 1-2 minutes during market hours)
    failover.check_and_escalate()

    # Or get report
    summary = failover.get_status_report()

Author: GENESIS 🌱
Date: 2026-05-16
"""

import logging
import os
import threading
import time
from typing import Dict, List, Optional

import requests

from genesis.alerts.alert_ack_tracker import (
    get_pending_alerts,
    increment_resend_count,
    mark_escalated,
    get_alert_summary,
)
from genesis.alerts.alert_dispatcher import send_alert, get_message_id_prefix

logger = logging.getLogger("genesis.alerts.failover")

# ── Configuration ────────────────────────────────────────────────────────────

BUS_URL = os.getenv("NEXUS_BUS_URL", "http://192.168.1.141:9999")
DISCORD_BRIDGE_URL = os.getenv("DISCORD_BRIDGE_URL", "http://localhost:8010")
DISCORD_BRIDGE_SECRET = os.getenv("DISCORD_BRIDGE_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c")
AHMED_CHAT_ID = os.getenv("AHMED_CHAT_ID", "8573754783")

RESEND_TIMEOUT_MIN = 5
ESCALATE_TIMEOUT_MIN = 10

_lock = threading.Lock()
_last_check_time: float = 0.0


# ── Escalation channels ──────────────────────────────────────────────────────

def _send_escalation_telegram(alert_title: str, message_id: str) -> bool:
    """
    Send escalation message via Telegram (second+ attempt).

    Returns:
        True if sent, False on failure.
    """
    text = (
        f"🚨🚨 <b>ALERT ESCALATION</b>\n"
        f"<b>{alert_title}</b>\n\n"
        f"<i>Previous attempt did not receive ACK.</i>\n"
        f"<i>This is a second/emergency message.</i>\n"
        f"<i>ID: {message_id}</i>\n\n"
        f"<b>Please acknowledge immediately:</b>\n"
        f"Reply to this message or call the on-call."
    )

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": AHMED_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=5,
        )
        resp.raise_for_status()
        logger.info("failover: Escalation Telegram sent for message_id=%s", message_id)
        return True
    except Exception as e:
        logger.warning("failover: Escalation Telegram failed: %s", e)
        return False


def _send_escalation_bus(alert_title: str, message_id: str) -> bool:
    """
    Send escalation to message bus (SOVEREIGN).

    Returns:
        True if sent, False on failure.
    """
    message = (
        f"[ESCALATION] Alert not acknowledged after 10 minutes\n"
        f"Title: {alert_title}\n"
        f"Message ID: {message_id}\n"
        f"Action: Page on-call immediately. All channels activated."
    )

    try:
        resp = requests.post(
            f"{BUS_URL}/send",
            json={
                "from": "genesis-alerts-escalation",
                "to": "sovereign",
                "message": message,
            },
            timeout=3,
        )
        resp.raise_for_status()
        logger.info("failover: Escalation bus sent for message_id=%s", message_id)
        return True
    except Exception as e:
        logger.warning("failover: Escalation bus failed: %s", e)
        return False


def _send_escalation_discord(alert_title: str, message_id: str) -> bool:
    """
    Send escalation to Discord #escalations channel.

    Returns:
        True if sent, False on failure.
    """
    if not DISCORD_BRIDGE_URL or not DISCORD_BRIDGE_SECRET:
        logger.debug("failover: Discord bridge not configured")
        return False

    try:
        resp = requests.post(
            f"{DISCORD_BRIDGE_URL}/post",
            headers={"X-Bridge-Auth": DISCORD_BRIDGE_SECRET},
            json={
                "channel": "#escalations",
                "agent": "genesis-alerts",
                "title": "🚨 ALERT ESCALATION — NO ACK FOR 10 MIN",
                "description": f"Alert: **{alert_title}**\n\nMessage ID: `{message_id}`\n\n"
                              f"Ahmed has not acknowledged this alert in 10 minutes. "
                              f"Escalating to all channels.",
                "color": "red",
                "fields": [
                    {"name": "Status", "value": "ESCALATED", "inline": True},
                    {"name": "Time Since Send", "value": "10+ minutes", "inline": True},
                ],
                "timestamp": True,
            },
            timeout=4,
        )
        resp.raise_for_status()
        logger.info("failover: Escalation Discord sent for message_id=%s", message_id)
        return True
    except Exception as e:
        logger.warning("failover: Escalation Discord failed: %s", e)
        return False


# ── Check and escalate logic ─────────────────────────────────────────────────

def _check_and_resend() -> int:
    """
    Check for alerts pending resend (older than 5 min, not ACKed).
    Resend them via Telegram.

    Returns:
        Number of alerts resent.
    """
    resend_alerts = get_pending_alerts(older_than_min=RESEND_TIMEOUT_MIN, critical=False)
    resent = 0

    for alert in resend_alerts:
        msg_id = alert["message_id"]
        title = alert["title"]

        # Resend via Telegram
        resend_msg_id = send_alert(
            title=f"[RESEND] {title}",
            body="Previous message was not acknowledged. Resending.",
            agent="genesis",
            tier="ESCALATE",
        )

        if resend_msg_id:
            increment_resend_count(msg_id)
            resent += 1
            logger.info(
                "failover: Resent alert message_id=%s resend_count=%d",
                msg_id, alert["resend_count"] + 1,
            )

    return resent


def _check_and_escalate() -> int:
    """
    Check for alerts pending escalation (older than 10 min, not ACKed).
    Escalate them to all channels.

    Returns:
        Number of alerts escalated.
    """
    escalate_alerts = get_pending_alerts(older_than_min=ESCALATE_TIMEOUT_MIN, critical=True)
    escalated = 0

    for alert in escalate_alerts:
        msg_id = alert["message_id"]
        title = alert["title"]

        logger.critical(
            "failover: ESCALATING ALERT — no ACK after 10 min — message_id=%s title=%s",
            msg_id, title,
        )

        # Send to all channels
        channels_reached = 0

        if _send_escalation_telegram(title, msg_id):
            channels_reached += 1

        if _send_escalation_bus(title, msg_id):
            channels_reached += 1

        if _send_escalation_discord(title, msg_id):
            channels_reached += 1

        if channels_reached > 0:
            mark_escalated(msg_id)
            escalated += 1
            logger.warning(
                "failover: Escalation complete — message_id=%s reached_channels=%d",
                msg_id, channels_reached,
            )

    return escalated


# ── Public API ───────────────────────────────────────────────────────────────

class AlertFailover:
    """
    Manages alert retry/escalation lifecycle.
    Safe for concurrent access (uses internal locks).
    """

    def __init__(self):
        """Initialize failover handler."""
        self._last_resend_check = 0.0
        self._last_escalate_check = 0.0
        self._check_interval_s = 30  # Don't check more than once per 30 sec

    def check_and_escalate(self) -> Dict:
        """
        Run full check: resend overdue alerts, escalate critical alerts.

        Returns:
            Dict with keys: resent, escalated, pending_resend, pending_escalate
        """
        with _lock:
            now = time.time()

            # Rate limit: don't check more than every 30 sec
            if now - self._last_resend_check < self._check_interval_s:
                logger.debug("failover: check skipped (rate limited)")
                return {
                    "resent": 0,
                    "escalated": 0,
                    "pending_resend": 0,
                    "pending_escalate": 0,
                }

            self._last_resend_check = now

            resent = _check_and_resend()
            escalated = _check_and_escalate()

            summary = get_alert_summary()

            return {
                "resent": resent,
                "escalated": escalated,
                "pending_resend": summary["pending_resend"],
                "pending_escalate": summary["pending_escalate"],
            }

    def get_status_report(self) -> str:
        """
        Get human-readable status report of alert queue.

        Returns:
            Multiline string suitable for logging or sending to Ahmed.
        """
        summary = get_alert_summary()

        return (
            f"ALERT FAILOVER STATUS:\n"
            f"  Total tracked: {summary['total_tracked']}\n"
            f"  Acknowledged: {summary['acked']}\n"
            f"  Pending: {summary['pending']}\n"
            f"  Pending resend (>5min): {summary['pending_resend']}\n"
            f"  Pending escalate (>10min): {summary['pending_escalate']}\n"
        )


# ── Convenience functions ────────────────────────────────────────────────────

def check_and_escalate() -> Dict:
    """
    Module-level function to trigger failover check.
    Can be called from cron or monitoring loops.

    Returns:
        Dict with resent, escalated, pending counts.
    """
    failover = AlertFailover()
    return failover.check_and_escalate()


def get_status_report() -> str:
    """
    Module-level function to get failover status.

    Returns:
        Human-readable status string.
    """
    failover = AlertFailover()
    return failover.get_status_report()
