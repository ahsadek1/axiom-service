"""
state.py — Thread-safe, staleness-aware cached value.
Spec: NEXUS_RESILIENCE_BASE v1.1 (Cipher, 2026-05-02)
Status: Zero conflicts — wraps existing in-memory caches.

Rule: Any value read before acting must be wrapped in FreshValue.
Stale = unavailable. Fail safe, not fail open.

Usage:
    vix = FreshValue(None, max_age=timedelta(minutes=5), label="vix")
    vix.update(14.3)

    # Safe path — returns None if stale
    v = vix.get()
    if v is None:
        skip("vix_stale")

    # Hard path — raises StaleStateError if stale (use in execution gates)
    v = vix.require()
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Generic, Optional, TypeVar

T = TypeVar("T")


class StaleStateError(Exception):
    """
    Raised by FreshValue.require() when the cached value is older than max_age.

    Attributes:
        label           — the FreshValue label (e.g. "vix", "regime")
        age_seconds     — how old the value is
        max_age_seconds — the configured staleness limit
    """

    def __init__(self, label: str, age_seconds: int, max_age_seconds: int):
        self.label = label
        self.age_seconds = age_seconds
        self.max_age_seconds = max_age_seconds
        super().__init__(
            f"{label} is stale ({age_seconds}s old, max {max_age_seconds}s)"
        )


class FreshValue(Generic[T]):
    """
    Thread-safe wrapper for a single cached value with a freshness TTL.

    - update() is atomic — no reader can see a partial state.
    - get() returns None when stale — safe for scan-skip logic.
    - require() raises StaleStateError when stale — use in execution gates.
    - age_seconds property for health endpoint reporting.

    Args:
        value    — initial value (may be None for "no data yet")
        max_age  — timedelta after which the value is considered stale
        label    — human label used in StaleStateError messages and logs
    """

    def __init__(self, value: T, max_age: timedelta, label: str) -> None:
        self._value: T = value
        self._updated_at: datetime = datetime.utcnow()
        self._max_age: timedelta = max_age
        self._label: str = label
        self._lock = threading.Lock()

    def update(self, new_value: T) -> None:
        """Atomically replace value and reset the freshness clock."""
        with self._lock:
            self._value = new_value
            self._updated_at = datetime.utcnow()

    def get(self) -> Optional[T]:
        """
        Return the value if fresh, None if stale.
        Never raises. Use for scan-skip decisions.
        """
        with self._lock:
            if (datetime.utcnow() - self._updated_at) > self._max_age:
                return None
            return self._value

    def require(self) -> T:
        """
        Return the value if fresh, raise StaleStateError if stale.
        Use in execution gates where stale data must hard-stop the flow.
        """
        with self._lock:
            age = datetime.utcnow() - self._updated_at
            if age > self._max_age:
                raise StaleStateError(
                    self._label,
                    int(age.total_seconds()),
                    int(self._max_age.total_seconds()),
                )
            return self._value

    @property
    def age_seconds(self) -> int:
        """Seconds since last update. Useful for health endpoint reporting."""
        with self._lock:
            return int((datetime.utcnow() - self._updated_at).total_seconds())

    @property
    def is_fresh(self) -> bool:
        """True if the value is within its TTL."""
        with self._lock:
            return (datetime.utcnow() - self._updated_at) <= self._max_age

    @property
    def label(self) -> str:
        return self._label
