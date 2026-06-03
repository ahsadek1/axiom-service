"""
circuit_breaker.py — Alpaca API Circuit Breaker
================================================
Prevents hammering a down Alpaca API with repeated calls.

States:
  CLOSED   — Normal operation. All calls go through.
  OPEN     — API is down. Calls blocked. Recovery probe every N seconds.
  HALF_OPEN — Testing recovery. One probe call allowed.

Thresholds:
  5 failures in 60s  → OPEN
  Recovery probe every 30s
  3 consecutive successes → CLOSED
"""
from __future__ import annotations
import logging
import threading
import time
from enum import Enum
from typing import Callable, Optional

log = logging.getLogger("nexus.alpaca.circuit_breaker")


class CBState(str, Enum):
    CLOSED    = "CLOSED"      # Normal — calls go through
    OPEN      = "OPEN"        # Down — calls blocked
    HALF_OPEN = "HALF_OPEN"   # Probing recovery


class CircuitBreakerOpen(Exception):
    """Raised when circuit breaker is OPEN — API is down."""
    pass


class CircuitBreaker:
    """
    Thread-safe circuit breaker for Alpaca API calls.

    Usage:
        cb = CircuitBreaker(system_id="THESIS")
        try:
            cb.call(lambda: requests.get(url))
        except CircuitBreakerOpen:
            # API is known to be down — skip this cycle
            pass
    """

    def __init__(
        self,
        system_id:         str,
        failure_threshold: int   = 5,
        failure_window_s:  int   = 60,
        recovery_probe_s:  int   = 30,
        success_threshold: int   = 3,
        notify_fn:         Optional[Callable[[str], None]] = None,
    ):
        self.system_id         = system_id
        self.failure_threshold = failure_threshold
        self.failure_window_s  = failure_window_s
        self.recovery_probe_s  = recovery_probe_s
        self.success_threshold = success_threshold
        self.notify_fn         = notify_fn

        self._state            = CBState.CLOSED
        self._failures:  list[float] = []   # timestamps
        self._successes  = 0
        self._opened_at: Optional[float] = None
        self._lock       = threading.Lock()

    @property
    def state(self) -> CBState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == CBState.OPEN

    def call(self, fn: Callable):
        """
        Execute fn through the circuit breaker.
        Raises CircuitBreakerOpen if API is known down.
        """
        with self._lock:
            if self._state == CBState.OPEN:
                # Check if recovery probe time has elapsed
                elapsed = time.time() - (self._opened_at or 0)
                if elapsed >= self.recovery_probe_s:
                    self._state = CBState.HALF_OPEN
                    log.info("[%s] CB: OPEN → HALF_OPEN (probing recovery)", self.system_id)
                else:
                    raise CircuitBreakerOpen(
                        f"{self.system_id} circuit breaker OPEN — "
                        f"retry in {self.recovery_probe_s - elapsed:.0f}s"
                    )

        try:
            result = fn()
            self._on_success()
            return result
        except CircuitBreakerOpen:
            raise
        except Exception as exc:
            self._on_failure(exc)
            raise

    def _on_success(self) -> None:
        with self._lock:
            now = time.time()
            if self._state == CBState.HALF_OPEN:
                self._successes += 1
                if self._successes >= self.success_threshold:
                    log.info("[%s] CB: HALF_OPEN → CLOSED after %d successes",
                            self.system_id, self._successes)
                    self._state    = CBState.CLOSED
                    self._failures = []
                    self._successes = 0
                    self._opened_at = None
                    self._notify("ALPACA GUARDIAN: Circuit breaker CLOSED — API recovered")
            elif self._state == CBState.CLOSED:
                self._successes += 1
                # Clean old failures outside window
                self._failures = [f for f in self._failures
                                 if now - f < self.failure_window_s]

    def _on_failure(self, exc: Exception) -> None:
        with self._lock:
            now = time.time()
            self._successes = 0
            self._failures.append(now)

            # Clean failures outside window
            self._failures = [f for f in self._failures
                             if now - f < self.failure_window_s]

            if self._state == CBState.HALF_OPEN:
                # Probe failed — back to OPEN
                self._state     = CBState.OPEN
                self._opened_at = now
                log.warning("[%s] CB: HALF_OPEN → OPEN (probe failed: %s)",
                           self.system_id, exc)
                return

            if (self._state == CBState.CLOSED and
                    len(self._failures) >= self.failure_threshold):
                self._state     = CBState.OPEN
                self._opened_at = now
                log.error(
                    "[%s] CB: CLOSED → OPEN (%d failures in %ds)",
                    self.system_id, len(self._failures), self.failure_window_s
                )
                self._notify(
                    f"ALPACA GUARDIAN: Circuit breaker OPEN for {self.system_id}\n"
                    f"{len(self._failures)} failures in {self.failure_window_s}s\n"
                    f"Last error: {type(exc).__name__}: {str(exc)[:100]}\n"
                    f"Trading PAUSED. Recovery probe every {self.recovery_probe_s}s."
                )

    def _notify(self, msg: str) -> None:
        if self.notify_fn:
            try:
                self.notify_fn(msg)
            except Exception:
                pass

    def force_open(self, reason: str = "") -> None:
        """Manually open the circuit breaker."""
        with self._lock:
            self._state     = CBState.OPEN
            self._opened_at = time.time()
            log.warning("[%s] CB: Force opened. Reason: %s", self.system_id, reason)

    def force_close(self) -> None:
        """Manually close the circuit breaker (use after manual fix)."""
        with self._lock:
            self._state     = CBState.CLOSED
            self._failures  = []
            self._successes = 0
            self._opened_at = None
            log.info("[%s] CB: Force closed.", self.system_id)

    def status(self) -> dict:
        with self._lock:
            return {
                "system_id":    self.system_id,
                "state":        self._state.value,
                "failures":     len(self._failures),
                "threshold":    self.failure_threshold,
                "opened_at":    self._opened_at,
                "successes":    self._successes,
            }
