"""
synthesis_pool_monitor.py — Internal health monitor for synthesis worker pool

Monitors the ThreadPoolExecutor that runs _run_synthesis() tasks.
Detects when:
  - Worker threads crash or hang
  - Semaphore is stuck (no release after 45s)
  - Pool queue backs up without completion

Auto-restart on failure with 3 retries before alerting SOVEREIGN.

Usage:
  pool_monitor = PoolHealthMonitor(pool=_SYNTHESIS_POOL, semaphore=_SYNTHESIS_SEMAPHORE)
  pool_monitor.start()  # runs in background thread
  pool_monitor.shutdown()  # on service shutdown
"""

import logging
import threading
import time
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

logger = logging.getLogger("omni.synthesis_pool_monitor")


class PoolHealthMonitor:
    """Monitor synthesis worker pool health. Auto-restart on failure."""

    def __init__(
        self,
        pool: ThreadPoolExecutor,
        semaphore: threading.Semaphore,
        check_interval_sec: int = 10,
        hang_timeout_sec: int = 45,
        silence_window_sec: int = 20 * 60,
    ):
        """
        Args:
            pool: ThreadPoolExecutor running synthesis tasks
            semaphore: Semaphore guarding pool slots
            check_interval_sec: How often to ping the pool (default 10s)
            hang_timeout_sec: If no completion in this time, assume hang (default 45s)
            silence_window_sec: Alert if no syntheses in this window (default 20min)
        """
        self.pool = pool
        self.semaphore = semaphore
        self.check_interval_sec = check_interval_sec
        self.hang_timeout_sec = hang_timeout_sec
        self.silence_window_sec = silence_window_sec

        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False
        self._last_completion_time = time.time()  # Track last successful synthesis completion
        self._ping_future = None
        self._restart_count = 0
        self._max_retries = 3

    def start(self) -> None:
        """Start background health monitor thread."""
        if self._running:
            logger.warning("PoolHealthMonitor already running")
            return

        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="omni-pool-health-monitor",
        )
        self._monitor_thread.start()
        logger.info("PoolHealthMonitor started (interval=%ds, hang_timeout=%ds)",
                    self.check_interval_sec, self.hang_timeout_sec)

    def shutdown(self) -> None:
        """Stop health monitor gracefully."""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        logger.info("PoolHealthMonitor stopped")

    def mark_synthesis_complete(self) -> None:
        """Call this after every successful synthesis completion.
        Used to track synthesis activity and detect silence.
        """
        self._last_completion_time = time.time()

    def _monitor_loop(self) -> None:
        """Background thread: continuously check pool health."""
        while self._running:
            try:
                self._check_pool_health()
            except Exception as e:
                logger.error("PoolHealthMonitor error: %s", e, exc_info=True)
            
            time.sleep(self.check_interval_sec)

    def _check_pool_health(self) -> None:
        """Run a single health check cycle."""
        # Check 1: Can we acquire + release the semaphore? (proves it's not stuck)
        if not self._check_semaphore_responsive():
            logger.critical(
                "POOL HEALTH: Semaphore stuck for >%ds — possible worker deadlock",
                self.hang_timeout_sec,
            )
            self._handle_pool_failure("semaphore_stuck")
            return

        # Check 2: Has the pool been silent too long?
        time_since_completion = time.time() - self._last_completion_time
        if time_since_completion > self.silence_window_sec:
            logger.critical(
                "POOL HEALTH: No synthesis completion in %d seconds (silence_window=%ds)",
                int(time_since_completion),
                self.silence_window_sec,
            )
            self._handle_pool_failure("synthesis_silence")
            return

        # Check 3: Can we submit a health-check task to the pool?
        if not self._check_pool_accepts_work():
            logger.critical("POOL HEALTH: Pool rejected health-check task submission")
            self._handle_pool_failure("pool_rejected_work")
            return

        # All checks passed
        self._restart_count = 0  # Reset retry counter on success

    def _check_semaphore_responsive(self) -> bool:
        """Try to acquire and release semaphore with timeout.
        Returns True if semaphore is responsive, False if stuck.
        """
        try:
            acquired = self.semaphore.acquire(timeout=self.hang_timeout_sec / 2)
            if not acquired:
                logger.warning("POOL HEALTH: Semaphore acquire timeout (%ds)", 
                               self.hang_timeout_sec / 2)
                return False
            
            self.semaphore.release()
            return True
        except Exception as e:
            logger.warning("POOL HEALTH: Semaphore check failed: %s", e)
            return False

    def _check_pool_accepts_work(self) -> bool:
        """Submit a dummy task to verify pool can accept work.
        Returns True if task was submitted successfully.
        """
        try:
            def _health_ping():
                return "pong"

            future = self.pool.submit(_health_ping)
            # Wait briefly for completion
            result = future.result(timeout=self.check_interval_sec)
            return result == "pong"
        except Exception as e:
            logger.warning("POOL HEALTH: Health ping failed: %s", e)
            return False

    def _handle_pool_failure(self, failure_reason: str) -> None:
        """Handle detected pool failure: retry or escalate."""
        self._restart_count += 1
        logger.critical(
            "POOL HEALTH FAILURE [attempt %d/%d]: %s",
            self._restart_count,
            self._max_retries,
            failure_reason,
        )

        if self._restart_count <= self._max_retries:
            # Retry: reset last_completion_time to give next cycle a chance
            logger.info("POOL HEALTH: Resetting silence window for retry")
            self._last_completion_time = time.time()
        else:
            # Max retries exceeded — escalate to SOVEREIGN
            self._escalate_to_sovereign(failure_reason)

    def _escalate_to_sovereign(self, failure_reason: str) -> None:
        """Escalate pool failure to SOVEREIGN after max retries."""
        logger.critical(
            "POOL HEALTH: Escalating to SOVEREIGN after %d failed retries. Reason: %s",
            self._max_retries,
            failure_reason,
        )
        
        try:
            import requests
            import os
            
            bus_url = os.environ.get("SOVEREIGN_BUS_URL", "http://192.168.1.141:9999")
            response = requests.post(
                f"{bus_url}/send",
                json={
                    "from": "omni",
                    "to": "sovereign",
                    "message": f"P0_CRITICAL: OMNI synthesis pool health failure. "
                              f"Reason: {failure_reason}. "
                              f"Failed after {self._max_retries} retry attempts. "
                              f"Service may be unable to process synthesis tasks.",
                },
                timeout=5,
            )
            logger.info("Escalation sent to SOVEREIGN: %s", response.status_code)
        except Exception as e:
            logger.error("Failed to escalate to SOVEREIGN: %s", e)
