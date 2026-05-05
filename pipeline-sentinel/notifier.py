"""
notifier.py — Telegram alert delivery for Pipeline Sentinel.

Features:
- Per-failure-class deduplication: same class within 300 seconds → silently skipped
- 3x retry with exponential backoff (1s → 2s → 4s)
- Never raises — monitoring must never crash the service
"""

import logging
import os
import time
from typing import Dict

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("sentinel.notifier")

_DEDUP_WINDOW_S = 300   # 5 minutes between same-class alerts
_MAX_RETRIES    = 3
_BACKOFF_BASE_S = 1


class TelegramNotifier:
    """Sends Telegram alerts to Ahmed with deduplication and retry logic."""

    def __init__(self) -> None:
        """Load Telegram credentials from environment."""
        self._bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id:   str = os.environ.get("TELEGRAM_AHMED_CHAT_ID", "")
        self._last_sent: Dict[str, float] = {}   # failure_class → last send unix ts

        if not self._bot_token or not self._chat_id:
            logger.warning(
                "TELEGRAM_BOT_TOKEN or TELEGRAM_AHMED_CHAT_ID not set — alerts will be logged only"
            )

    def send_alert(self, message: str, failure_class: str) -> None:
        """
        Send a Telegram alert for a failure class.

        Silently skips if the same failure_class was alerted within the dedup window.
        Retries up to 3 times with exponential backoff on failure.
        Never raises.

        Args:
            message:       Human-readable alert text (supports Markdown).
            failure_class: Failure class name — used for deduplication key.
        """
        now = time.time()
        last = self._last_sent.get(failure_class, 0.0)

        if now - last < _DEDUP_WINDOW_S:
            logger.debug(
                "Alert suppressed for %s — last sent %.0fs ago (dedup window=%ds)",
                failure_class,
                now - last,
                _DEDUP_WINDOW_S,
            )
            return

        if not self._bot_token or not self._chat_id:
            logger.warning("ALERT [%s]: %s (Telegram not configured)", failure_class, message)
            self._last_sent[failure_class] = now
            return

        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        # Escape special chars that break Markdown mode (underscores, brackets, etc.)
        import re
        safe_message = re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', message)
        payload = {
            "chat_id":    self._chat_id,
            "text":       safe_message,
            "parse_mode": "MarkdownV2",
        }

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.post(url, json=payload, timeout=10)
                if resp.status_code == 200:
                    logger.info("Alert sent for failure_class=%s", failure_class)
                    self._last_sent[failure_class] = now
                    return
                logger.warning(
                    "Telegram returned %d on attempt %d for %s",
                    resp.status_code, attempt, failure_class,
                )
            except Exception as exc:
                logger.warning(
                    "Telegram request failed (attempt %d/%d) for %s: %s",
                    attempt, _MAX_RETRIES, failure_class, exc,
                )

            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_BASE_S * (2 ** (attempt - 1)))

        logger.error(
            "Failed to send Telegram alert for %s after %d attempts",
            failure_class, _MAX_RETRIES,
        )
