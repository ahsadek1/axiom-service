"""
credential_loader.py — Dynamic Credential Reloading

Maintains a cached copy of all API credentials that auto-reloads from .env
every 5 minutes or on explicit reload. Prevents silent API failures when
credentials rotate.

FIX-CREDENTIAL-VAULT (2026-05-13): Added to eliminate scenario where:
1. Railway token rotates → TOOLS.md updated
2. Dependent script still has old token in memory
3. API call silently fails (no alert, no stderr)
4. Sub-agent hangs indefinitely
"""

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("omni.credential_loader")

_RELOAD_INTERVAL_S = 300  # Reload every 5 minutes


@dataclass
class CredentialSet:
    """Immutable snapshot of all API credentials."""

    anthropic_api_key: str
    openai_api_key: str
    gemini_api_key: str
    deepseek_api_key: str
    nexus_secret: str
    nexus_prime_secret: str
    omni_secret: str
    axiom_secret: str
    telegram_bot_token: str
    ahmed_chat_id: str


class CredentialCache:
    """Thread-safe credential cache with auto-reload."""

    def __init__(self):
        self._lock = threading.RLock()
        self._last_reload_time = 0.0
        self._creds: Optional[CredentialSet] = None
        self._reload_count = 0

    def get(self) -> Optional[CredentialSet]:
        """
        Get current credential set, reloading if TTL expired.

        Returns:
            CredentialSet or None if credentials cannot be loaded.
        """
        with self._lock:
            now = time.time()
            if (
                self._creds is None
                or (now - self._last_reload_time) > _RELOAD_INTERVAL_S
            ):
                self._reload()
            return self._creds

    def _reload(self) -> None:
        """Reload credentials from environment variables."""
        try:
            creds = CredentialSet(
                anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
                openai_api_key=os.getenv("OPENAI_API_KEY", ""),
                gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
                deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
                nexus_secret=os.getenv("NEXUS_WEBHOOK_SECRET", ""),
                nexus_prime_secret=os.getenv("NEXUS_PRIME_SECRET", ""),
                omni_secret=os.getenv("OMNI_SECRET", ""),
                axiom_secret=os.getenv("AXIOM_SECRET", ""),
                telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
                ahmed_chat_id=os.getenv("AHMED_CHAT_ID", ""),
            )

            # Validate — ensure no empty critical credentials
            critical = [
                ("ANTHROPIC_API_KEY", creds.anthropic_api_key),
                ("OPENAI_API_KEY", creds.openai_api_key),
                ("GEMINI_API_KEY", creds.gemini_api_key),
                ("DEEPSEEK_API_KEY", creds.deepseek_api_key),
            ]
            missing = [name for name, value in critical if not value]
            if missing:
                logger.error(
                    "Credential reload failed — missing critical keys: %s",
                    missing,
                )
                return

            self._creds = creds
            self._last_reload_time = time.time()
            self._reload_count += 1
            logger.info(
                "Credential cache reloaded (reload #%d) | "
                "anthropic_len=%d openai_len=%d gemini_len=%d deepseek_len=%d",
                self._reload_count,
                len(creds.anthropic_api_key),
                len(creds.openai_api_key),
                len(creds.gemini_api_key),
                len(creds.deepseek_api_key),
            )
        except Exception as e:
            logger.error("Credential reload error: %s", e)

    def reload_now(self) -> None:
        """Explicitly reload credentials immediately."""
        with self._lock:
            self._last_reload_time = 0  # Force reload on next get()
            self._reload()
            logger.info("Credential cache explicit reload requested")


# Global singleton
_cache = CredentialCache()


def get_credentials() -> Optional[CredentialSet]:
    """
    Get current cached credential set, reloading if needed.

    Returns:
        CredentialSet or None if load failed.
    """
    return _cache.get()


def reload_credentials_now() -> None:
    """Explicitly reload all credentials from environment immediately."""
    _cache.reload_now()
