"""
TelegramClient — Telegram Bot API client for direct Ahmed notifications.

Used only for escalations that bypass SOVEREIGN (e.g. thesis completely
broken, all frameworks NO-GO, critical system failure).

All methods return bool and never raise.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


class TelegramClient:
    """Async HTTP client for Telegram Bot API.

    Args:
        bot_token: Telegram bot API token.
        chat_id: Target chat ID (Ahmed's personal chat).
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        """Store bot credentials; no connection opened yet."""
        self._bot_token = bot_token
        self._chat_id = chat_id

    async def send_message(self, text: str) -> bool:
        """Send a plain-text message to Ahmed via Telegram.

        Args:
            text: Message text (HTML formatting supported).

        Returns:
            True if message was accepted by Telegram API, False otherwise.
        """
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    return True
                logger.warning(
                    "TelegramClient.send_message() HTTP %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except Exception as exc:
            logger.error(
                "TelegramClient.send_message() error: %s", exc, exc_info=True
            )
            return False
