"""
ORACLE Intelligence Hub — Rate Limiter
Token bucket rate limiter per platform.
Thread-safe implementation using threading.Lock.
"""

import logging
import time
from threading import Lock
from typing import Dict

import config

logger = logging.getLogger(__name__)

# Requests per minute per platform (from config)
_PLATFORM_LIMITS: Dict[str, int] = {
    "polygon":           config.POLYGON_RATE_LIMIT,
    "orats":             config.ORATS_RATE_LIMIT,
    "market_chameleon":  config.MARKET_CHAMELEON_RATE_LIMIT,
    "unusual_whales":    config.UNUSUAL_WHALES_RATE_LIMIT,
    "spotgamma":         config.SPOTGAMMA_RATE_LIMIT,
    "trading_economics": config.TRADING_ECONOMICS_RATE_LIMIT,
    "fred":              120,
    "alpha_vantage":     75,
    "edgar":             config.EDGAR_RATE_LIMIT,
    "deepseek":          60,
    "gemini":            30,
    "ails":              300,
    "benzinga":          240,  # conservative — limit is 4000/min
}


class _TokenBucket:
    """Token bucket for a single platform."""

    def __init__(self, requests_per_minute: int) -> None:
        self._rate: float = requests_per_minute / 60.0  # tokens per second
        self._capacity: float = float(requests_per_minute)
        self._tokens: float = self._capacity
        self._last_refill: float = time.monotonic()
        self._lock: Lock = Lock()

    def _refill(self) -> None:
        """Add tokens based on elapsed time (call under lock)."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def acquire(self, timeout: float = 10.0) -> bool:
        """
        Acquire one token. Blocks until available or timeout.

        Args:
            timeout: Maximum seconds to wait for a token.

        Returns:
            True if acquired, False if timed out.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            time.sleep(0.05)
        logger.warning("Rate limit token not acquired within %.1fs", timeout)
        return False


# ── Global bucket registry ────────────────────────────────────────────────────
_buckets: Dict[str, _TokenBucket] = {
    platform: _TokenBucket(rpm)
    for platform, rpm in _PLATFORM_LIMITS.items()
}
# Cipher P2-7 fix: lock for safe on-the-fly bucket registration.
# CPython dict writes are GIL-atomic but two concurrent calls for the same unknown
# platform can create two separate buckets — the second overwrites the first,
# breaking rate-limit isolation for the brief window between check and write.
_registry_lock: Lock = Lock()


def acquire(platform: str, timeout: float = 10.0) -> bool:
    """
    Acquire a rate-limit token for the given platform.

    Args:
        platform: Platform identifier (e.g. "polygon", "fred").
        timeout:  Maximum wait time in seconds.

    Returns:
        True if token acquired, False if timed out.
    """
    bucket = _buckets.get(platform)
    if bucket is None:
        # Cipher P2-7 fix: double-checked locking prevents two concurrent calls
        # for the same unknown platform creating separate bucket instances.
        with _registry_lock:
            bucket = _buckets.get(platform)  # re-check inside lock
            if bucket is None:
                logger.warning(
                    "Unknown platform '%s' — applying conservative default rate limit (120 RPM)",
                    platform,
                )
                bucket = _TokenBucket(requests_per_minute=120)
                _buckets[platform] = bucket
    return bucket.acquire(timeout=timeout)
