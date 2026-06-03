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

# SENTINEL escalation routing fix (Ahmed, April 22 2026):
# Critical failures must also route to SOVEREIGN via message bus.
# Import here — if sentinel can't import its own module, we want a clear error.
try:
    from sovereign_escalator import escalate_to_sovereign as _escalate
except ImportError:
    _escalate = None  # type: ignore[assignment]
    logger.warning("sovereign_escalator not available — SOVEREIGN routing disabled")

# Failure classes that warrant SOVEREIGN escalation (not just Ahmed Telegram)
_SOVEREIGN_ESCALATION_CLASSES = {
    "PIPELINE_STALL",
    "AGENT_SILENCE",
    "COMPLETION_RATE_LOW",
    "NETWORK_DEGRADED",
    "SCORE_CRITICAL",
}

_DEDUP_WINDOW_S          = 1800   # 30 minutes between same-class alerts (was 5 min — caused alert floods)
_DEDUP_WINDOW_CRITICAL_S = 600    # 10 minutes for critical classes during market hours
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

        # Use tighter dedup window for critical classes during market hours
        import datetime
        import pytz
        _et = pytz.timezone("America/New_York")
        _now_et = datetime.datetime.now(_et)
        _is_market = (
            _now_et.weekday() < 5
            and (_now_et.hour * 60 + _now_et.minute) >= 9 * 60 + 30
            and (_now_et.hour * 60 + _now_et.minute) < 16 * 60
        )
        _critical_classes = {"PIPELINE_STALL", "AGENT_SILENCE", "SCORE_CRITICAL"}
        _window = _DEDUP_WINDOW_CRITICAL_S if (_is_market and failure_class in _critical_classes) else _DEDUP_WINDOW_S

        if now - last < _window:
            logger.debug(
                "Alert suppressed for %s — last sent %.0fs ago (dedup window=%ds)",
                failure_class,
                now - last,
                _window,
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
                    # SENTINEL escalation routing fix: also route to SOVEREIGN for critical classes
                    if _escalate and failure_class in _SOVEREIGN_ESCALATION_CLASSES:
                        _escalate(message=message, level="WARNING", dedup_key=failure_class)
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

        # SENTINEL escalation routing fix:
        # Also route critical failure classes to SOVEREIGN via NNS message bus.
        # This runs after Telegram delivery (not instead of it) so Ahmed and
        # SOVEREIGN both receive the alert.
        if _escalate and failure_class in _SOVEREIGN_ESCALATION_CLASSES:
            _escalate(
                message=message,
                level="CRITICAL" if "STALL" in failure_class or "SILENCE" in failure_class else "WARNING",
                dedup_key=failure_class,
            )
        elif _escalate is None:
            logger.debug("sovereign_escalator unavailable — SOVEREIGN routing skipped")

    def send_resolved(self, failure_class: str, detail: str = "") -> None:
        """
        Send a single 'RESOLVED' notification when a failure class clears.

        This is the partner to send_alert: Ahmed gets told when something is fixed,
        not just when it breaks. Uses its own dedup key so it never suppresses.

        Args:
            failure_class: The failure class that has resolved.
            detail:        Optional resolution detail (e.g. score, action taken).
        """
        # Only fire resolved if we actually sent an alert for this failure recently (last 8h)
        last_alert = self._last_sent.get(failure_class, 0.0)
        if time.time() - last_alert > 28800:  # 8 hours — no recent alert, skip
            return

        resolve_key = f"RESOLVED_{failure_class}"
        now = time.time()
        last = self._last_sent.get(resolve_key, 0.0)
        if now - last < 300:   # Don't spam resolved either
            return

        detail_str = f" — {detail}" if detail else ""
        message = f"✅ *RESOLVED: {failure_class}*{detail_str}"
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        import re
        safe_message = re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', message)
        payload = {"chat_id": self._chat_id, "text": safe_message, "parse_mode": "MarkdownV2"}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info("Resolved notification sent for %s", failure_class)
                self._last_sent[resolve_key] = now
        except Exception as exc:
            logger.warning("Failed to send resolved notification for %s: %s", failure_class, exc)
