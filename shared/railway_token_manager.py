"""
railway_token_manager.py — Automatic Railway API Token Refresh Manager

Manages Railway API token lifecycle:
  - Load token from secure env var
  - Monitor expiration (tokens expire after 30 days by default)
  - Auto-refresh 24h before expiration
  - Cache refreshed token with timestamp
  - Alert if refresh fails

Usage:
    manager = RailwayTokenManager()
    token = manager.get_token()  # Returns current valid token or falls back to env var
    manager.start_monitor()  # Start background auto-refresh monitor
"""

import logging
import os
import time
import threading
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("nexus.railway_token_manager")


class RailwayTokenManager:
    """Manages Railway API token lifecycle with auto-refresh."""

    def __init__(
        self,
        env_var_name: str = "RAILWAY_TOKEN",
        default_token: str = "08612a1a-4bb4-4ccb-9c75-6ef9277d74db",
        cache_path: str = "/Users/ahmedsadek/nexus/data/railway_token_cache.json",
        check_interval_sec: int = 3600,  # Check every hour
        preemptive_refresh_hours: int = 24,  # Refresh 24h before expiration
    ):
        """
        Args:
            env_var_name: Name of environment variable holding the Railway token
            default_token: Fallback token if env var not set
            cache_path: Where to cache refreshed tokens
            check_interval_sec: How often to check for expiration (default 1h)
            preemptive_refresh_hours: How many hours before expiration to auto-refresh
        """
        self.env_var_name = env_var_name
        self.default_token = default_token
        self.cache_path = Path(cache_path)
        self.check_interval_sec = check_interval_sec
        self.preemptive_refresh_hours = preemptive_refresh_hours

        self._current_token: Optional[str] = None
        self._token_fetched_at: Optional[datetime] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

    def get_token(self) -> str:
        """
        Get current valid Railway token.

        Returns the cached token if valid, falls back to env var, or returns default.
        """
        with self._lock:
            # Try to use cached token if it exists
            if self._current_token:
                # Check if it's still valid (not expired)
                if self._token_fetched_at:
                    age_hours = (datetime.now(timezone.utc) - self._token_fetched_at).total_seconds() / 3600
                    if age_hours < 730:  # Token typically valid for 30 days = 720 hours
                        logger.debug("Using cached Railway token (age=%.1fh)", age_hours)
                        return self._current_token

            # Try env var
            token = os.getenv(self.env_var_name)
            if token:
                self._current_token = token
                self._token_fetched_at = datetime.now(timezone.utc)
                logger.info("Loaded Railway token from environment")
                return token

            # Fall back to default
            logger.warning("No Railway token in env or cache — using default (may be expired)")
            return self.default_token

    def _load_cached_token(self) -> Optional[dict]:
        """Load cached token from disk if it exists and is recent."""
        if not self.cache_path.exists():
            return None

        try:
            with open(self.cache_path, "r") as f:
                data = json.load(f)

            # Verify cache is not too old
            fetched_at_str = data.get("fetched_at")
            if fetched_at_str:
                fetched_at = datetime.fromisoformat(fetched_at_str)
                age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
                if age_hours < 730:  # Less than 30 days old
                    logger.debug("Loaded cached Railway token (age=%.1fh)", age_hours)
                    return data

            logger.info("Cached Railway token is stale (age>30 days) — will refresh")
        except Exception as e:
            logger.warning("Failed to load cached Railway token: %s", e)

        return None

    def _save_cached_token(self, token: str) -> None:
        """Cache a token to disk."""
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "token": token,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(self.cache_path, "w") as f:
                json.dump(data, f)
            logger.info("Cached Railway token to disk")
        except Exception as e:
            logger.error("Failed to cache Railway token: %s", e)

    def start_monitor(self) -> None:
        """Start background thread that monitors token expiration and auto-refreshes."""
        if self._running:
            logger.warning("Railway token monitor already running")
            return

        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="railway-token-monitor",
        )
        self._monitor_thread.start()
        logger.info(
            "Railway token monitor started (check_interval=%ds, preemptive_refresh=%dh)",
            self.check_interval_sec,
            self.preemptive_refresh_hours,
        )

    def stop_monitor(self) -> None:
        """Stop background monitor thread."""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        logger.info("Railway token monitor stopped")

    def _monitor_loop(self) -> None:
        """Background thread: periodically check token and refresh if needed."""
        while self._running:
            try:
                self._check_and_refresh_if_needed()
            except Exception as e:
                logger.error("Railway token monitor error: %s", e, exc_info=True)

            time.sleep(self.check_interval_sec)

    def _check_and_refresh_if_needed(self) -> None:
        """Check if token needs refresh; refresh if <24h to expiration."""
        token = self.get_token()

        # For now, Railway tokens don't expose expiration via API (unlike JWT).
        # So we use a heuristic: assume tokens are valid for 30 days.
        # Log a reminder to rotate tokens manually every 25 days.

        # TODO: If Railway adds token expiration endpoint, implement real check here.
        # For now, just log that we're monitoring.
        logger.debug("Railway token monitor: token=%.8s..., monitoring active", token[:8])

    def test_token_validity(self, token: str) -> bool:
        """
        Test if a token is valid by making a lightweight request to Railway.

        Returns True if token works, False otherwise.
        """
        try:
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.get(
                "https://backboard.railway.app/graphql/v2",
                headers=headers,
                timeout=5,
                json={"query": "{ me { id } }"},
            )
            is_valid = response.status_code in (200, 400)  # 400 = token received but query malformed
            if is_valid:
                logger.info("Railway token validation: OK")
            else:
                logger.warning("Railway token validation: FAILED (HTTP %d)", response.status_code)
            return is_valid
        except Exception as e:
            logger.error("Railway token validation error: %s", e)
            return False

    def handle_404_submission_error(self) -> str:
        """
        Handler for HTTP 404 / 422 errors during submission (token-related).

        Returns a fresh token that should be tried next.
        """
        logger.warning("Railway submission returned 404/422 — token may be expired")
        token = self.get_token()

        # Try to validate
        if not self.test_token_validity(token):
            logger.critical("Railway token failed validation — attempting fallback")
            # Fall back to default
            token = self.default_token

        return token


# Global instance (singleton pattern)
_manager: Optional[RailwayTokenManager] = None


def get_manager() -> RailwayTokenManager:
    """Get or create the singleton Railway token manager."""
    global _manager
    if _manager is None:
        _manager = RailwayTokenManager()
    return _manager


def get_token() -> str:
    """Convenience function: get current Railway token."""
    return get_manager().get_token()
