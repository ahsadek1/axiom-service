"""
telegram.py — Cipher Telegram Alerts + SOVEREIGN Bus Notifications

Sends critical alerts to Ahmed (Telegram) and SOVEREIGN (Nexus bus) when
Cipher encounters persistent failures.
Never raises — both channels are best-effort, never block the trading pipeline.
"""

import logging
import os

import requests

logger = logging.getLogger("cipher.telegram")

TELEGRAM_TIMEOUT_S = 5
BUS_TIMEOUT_S = 5
NEXUS_BUS_URL = os.environ.get("NEXUS_BUS_URL", "http://192.168.1.141:9999")
_AGENT_NAME = "cipher"


def _send(bot_token: str, chat_id: str, text: str) -> None:
    """
    Send a Telegram message. Silently swallows all errors.

    Args:
        bot_token: Telegram bot token
        chat_id: Target chat ID
        text: Message text
    """
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=TELEGRAM_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning("Telegram alert failed (non-critical): %s", e)


def _bus_notify(message: str) -> None:
    """
    Send an alert to SOVEREIGN via the Nexus message bus. Fire-and-forget.
    Never raises — bus is best-effort, never blocks the trading pipeline.

    Args:
        message: Plain-text alert body for SOVEREIGN.
    """
    try:
        resp = requests.post(
            f"{NEXUS_BUS_URL}/send",
            json={"from": _AGENT_NAME, "to": "sovereign", "message": message},
            timeout=BUS_TIMEOUT_S,
        )
        if resp.status_code not in (200, 201):
            logger.warning("Bus notify returned HTTP %d", resp.status_code)
    except Exception as e:
        logger.warning("Bus notify failed (non-critical): %s", e)


def alert_brain_down(
    bot_token: str, chat_id: str, consecutive_failures: int
) -> None:
    """
    Alert Ahmed (Telegram) and SOVEREIGN (bus) that Claude has been down
    for multiple consecutive windows.

    Args:
        bot_token: Telegram bot token
        chat_id: Ahmed's chat ID
        consecutive_failures: Number of consecutive windows where AI failed
    """
    text = (
        f"🔴 <b>Cipher Brain Down</b>\n"
        f"Claude API has failed for {consecutive_failures} consecutive windows.\n"
        f"Cipher is not analyzing tickers. Check Anthropic API status."
    )
    bus_message = (
        f"[CIPHER] BRAIN DOWN — {consecutive_failures} consecutive failures. "
        f"Claude API unreachable. Cipher not analyzing tickers."
    )
    _send(bot_token, chat_id, text)
    _bus_notify(bus_message)
    logger.critical("Cipher brain down alert sent — %d consecutive failures", consecutive_failures)


def alert_submission_failed(
    bot_token: str, chat_id: str, ticker: str, buffer: str
) -> None:
    """
    Alert Ahmed (Telegram) and SOVEREIGN (bus) that a buffer submission
    permanently failed after retry.

    Args:
        bot_token: Telegram bot token
        chat_id: Ahmed's chat ID
        ticker: Ticker that failed
        buffer: 'Alpha' or 'Prime'
    """
    text = (
        f"⚠️ <b>Cipher Submission Failed</b>\n"
        f"Failed to submit {ticker} to {buffer} Buffer after retry.\n"
        f"Check buffer service health."
    )
    bus_message = (
        f"[CIPHER] SUBMISSION FAILED — {ticker} → {buffer} Buffer after retry. "
        f"Check buffer service health."
    )
    _send(bot_token, chat_id, text)
    _bus_notify(bus_message)
