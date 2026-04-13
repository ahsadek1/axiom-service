"""
telegram.py — Axiom Telegram Notification Utility

Sends messages to Ahmed's Telegram chat.
Fail-safe: never raises — logs errors and continues.
All Nexus notifications route through this module.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("axiom.telegram")


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "HTML",
) -> bool:
    """
    Send a Telegram message to Ahmed.

    Args:
        bot_token:  Telegram bot token.
        chat_id:    Target chat ID (Ahmed's ID: 8573754783).
        text:       Message text (HTML or plain).
        parse_mode: 'HTML' or 'Markdown' (default: HTML).

    Returns:
        True if message sent successfully, False on any error.
    """
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        logger.warning("Telegram send failed (status %d): %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        logger.error("Telegram send exception: %s", e)
        return False


def send_service_down_alert(
    bot_token: str,
    chat_id: str,
    service_name: str,
    consecutive_failures: int,
    last_success: Optional[str] = None,
) -> None:
    """
    Send SERVICE DOWN alert to Ahmed.

    Args:
        bot_token:            Telegram bot token.
        chat_id:              Ahmed's chat ID.
        service_name:         Name of the service that is down.
        consecutive_failures: Number of consecutive failures.
        last_success:         ISO timestamp of last successful contact, or None.
    """
    last_ok = f"\nLast contact: {last_success}" if last_success else ""
    text = (
        f"🚨 <b>SERVICE DOWN — {service_name}</b>\n"
        f"Consecutive failures: {consecutive_failures}\n"
        f"Impact: Concordance may require fewer agents (P2 max){last_ok}\n"
        f"Auto-retry in progress."
    )
    send_message(bot_token, chat_id, text)


def send_pool_alert(
    bot_token: str,
    chat_id: str,
    pool_size: int,
    reason: str,
) -> None:
    """
    Send pool size alert when Tier 2 produces fewer than minimum candidates.

    Args:
        bot_token:  Telegram bot token.
        chat_id:    Ahmed's chat ID.
        pool_size:  Current pool size.
        reason:     Reason for low pool size.
    """
    text = (
        f"⚠️ <b>AXIOM POOL ALERT</b>\n"
        f"Pool size: {pool_size} (minimum: 10)\n"
        f"Reason: {reason}\n"
        f"Using previous pool. Agents continue scanning."
    )
    send_message(bot_token, chat_id, text)


def send_circuit_breaker_alert(
    bot_token: str,
    chat_id: str,
    level: str,
    trigger: str,
    action: str,
) -> None:
    """
    Send circuit breaker activation alert.

    Args:
        bot_token: Telegram bot token.
        chat_id:   Ahmed's chat ID.
        level:     'AMBER', 'RED', or 'STOP'.
        trigger:   What triggered the circuit breaker.
        action:    What action was taken.
    """
    emoji = {"AMBER": "🟡", "RED": "🔴", "STOP": "🛑"}.get(level, "⚠️")
    text = (
        f"{emoji} <b>{level} ALERT</b>\n"
        f"Trigger: {trigger}\n"
        f"Action: {action}"
    )
    send_message(bot_token, chat_id, text)
