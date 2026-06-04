"""
worker_monitor.py — OMNI Synthesis Pool Worker Health Monitor

Detects when synthesis worker threads die or become unresponsive.
Triggers automatic service restart when capacity drops below 2/3.

GENESIS FIX 2026-06-04: Replaces manual escalation with automated healing.
"""

import logging
import os
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Thread, Lock

logger = logging.getLogger("omni.worker_monitor")


class WorkerHealthMonitor:
    """Monitor ThreadPoolExecutor worker health and trigger restart if degraded."""
    
    def __init__(self, executor: ThreadPoolExecutor, max_workers: int, restart_callback=None):
        """
        Initialize worker monitor.
        
        Args:
            executor: ThreadPoolExecutor to monitor
            max_workers: Expected max workers (e.g., 3)
            restart_callback: Optional callable to invoke on restart (default: os.execv)
        """
        self.executor = executor
        self.max_workers = max_workers
        self.restart_callback = restart_callback or self._default_restart
        self.min_healthy_workers = max(1, (max_workers * 2) // 3)  # 2/3 threshold
        
        self._last_health_check = time.time()
        self._consecutive_degraded = 0
        self._lock = Lock()
        
        self._monitor_thread = None
        self._running = False
    
    def start(self):
        """Start background monitoring thread."""
        if self._running:
            return
        
        self._running = True
        self._monitor_thread = Thread(
            target=self._monitor_loop,
            daemon=True,
            name="omni-worker-monitor"
        )
        self._monitor_thread.start()
        logger.info(
            "WorkerHealthMonitor started | max_workers=%d | min_healthy=%d",
            self.max_workers, self.min_healthy_workers
        )
    
    def stop(self):
        """Stop background monitoring."""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
    
    def _monitor_loop(self):
        """Background loop checking worker health every 10 seconds."""
        while self._running:
            try:
                self._check_and_heal()
            except Exception as e:
                logger.error("WorkerHealthMonitor check failed: %s", e, exc_info=True)
            
            time.sleep(10)  # Check every 10 seconds
    
    def _check_and_heal(self):
        """Check worker health; restart if degraded."""
        with self._lock:
            # Count alive workers
            alive = len([f for f in self.executor._threads if f.is_alive()])
            
            # Log for diagnostics
            logger.debug(
                "Worker health check | alive=%d / max=%d | min_healthy=%d",
                alive, self.max_workers, self.min_healthy_workers
            )
            
            if alive < self.min_healthy_workers:
                self._consecutive_degraded += 1
                logger.warning(
                    "Synthesis pool DEGRADED (%d alive, need %d) | "
                    "consecutive_checks=%d",
                    alive, self.min_healthy_workers, self._consecutive_degraded
                )
                
                # Trigger restart after 2 consecutive degraded checks (20s window)
                if self._consecutive_degraded >= 2:
                    logger.critical(
                        "Worker pool UNRECOVERED for 20s — triggering restart | "
                        "alive=%d < min_healthy=%d",
                        alive, self.min_healthy_workers
                    )
                    self.restart_callback()
                    # restart_callback should exit process; if it returns, reset counter
                    self._consecutive_degraded = 0
            else:
                # Pool healthy — reset degradation counter
                if self._consecutive_degraded > 0:
                    logger.info(
                        "Worker pool recovered | alive=%d >= min_healthy=%d",
                        alive, self.min_healthy_workers
                    )
                self._consecutive_degraded = 0
            
            self._last_health_check = time.time()
    
    def _default_restart(self):
        """Default restart callback: restart the Python process via os.execv."""
        logger.critical("Restarting OMNI service via os.execv")
        
        # Notify SOVEREIGN before restart
        try:
            import requests
            requests.post(
                f"{os.environ.get('SOVEREIGN_BUS_URL', 'http://192.168.1.141:9999')}/send",
                json={
                    "from": "omni",
                    "to": "sovereign",
                    "message": "OMNI worker pool restart triggered by monitor | alive workers < 2/3",
                },
                timeout=2
            )
        except Exception as e:
            logger.warning("Failed to notify SOVEREIGN: %s", e)
        
        # Wait for notification to be sent
        time.sleep(0.5)
        
        # Restart process
        import sys
        os.execv(sys.executable, [sys.executable] + sys.argv)
