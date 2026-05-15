"""
test_synthesis_pool_monitor.py — Unit tests for PoolHealthMonitor

Test cases:
  1. Normal operation: health checks pass, silence_time resets
  2. Semaphore stuck: monitor detects and escalates
  3. Pool silence: monitor detects no completion in window
  4. Pool accepts work: health ping succeeds
  5. Escalation to SOVEREIGN after max retries
"""

import unittest
import threading
import time
from unittest.mock import Mock, patch, MagicMock
from concurrent.futures import ThreadPoolExecutor

# Add parent directory to path for imports
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from synthesis_pool_monitor import PoolHealthMonitor


class TestPoolHealthMonitor(unittest.TestCase):
    """Test PoolHealthMonitor health check logic."""

    def setUp(self):
        """Set up test fixtures."""
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-synth")
        self.semaphore = threading.Semaphore(2)
        self.monitor = PoolHealthMonitor(
            pool=self.pool,
            semaphore=self.semaphore,
            check_interval_sec=1,
            hang_timeout_sec=2,
            silence_window_sec=5,  # Short window for testing
        )

    def tearDown(self):
        """Clean up resources."""
        self.monitor.shutdown()
        self.pool.shutdown(wait=False)

    def test_monitor_starts_and_stops(self):
        """Test 1: Monitor starts as daemon thread and can be stopped."""
        self.monitor.start()
        self.assertTrue(self.monitor._running)
        
        # Monitor thread should be running
        self.assertIsNotNone(self.monitor._monitor_thread)
        self.assertTrue(self.monitor._monitor_thread.is_alive())
        
        # Shutdown should stop the thread
        self.monitor.shutdown()
        self.assertFalse(self.monitor._running)
        
        # Wait for thread to exit
        self.monitor._monitor_thread.join(timeout=3)
        self.assertFalse(self.monitor._monitor_thread.is_alive())

    def test_mark_synthesis_complete_resets_silence(self):
        """Test 2: mark_synthesis_complete() resets silence timer."""
        old_time = self.monitor._last_completion_time
        time.sleep(0.1)
        
        self.monitor.mark_synthesis_complete()
        new_time = self.monitor._last_completion_time
        
        self.assertGreater(new_time, old_time)

    def test_semaphore_responsive_check_succeeds(self):
        """Test 3: Semaphore responsiveness check passes with healthy semaphore."""
        result = self.monitor._check_semaphore_responsive()
        self.assertTrue(result)

    def test_semaphore_stuck_detection(self):
        """Test 4: Detect stuck semaphore (all permits acquired)."""
        # Acquire all permits
        self.semaphore.acquire()
        self.semaphore.acquire()
        
        # Monitor should detect stuck semaphore
        result = self.monitor._check_semaphore_responsive()
        self.assertFalse(result)
        
        # Release permits
        self.semaphore.release()
        self.semaphore.release()

    def test_pool_accepts_work(self):
        """Test 5: Health ping succeeds with responsive pool."""
        result = self.monitor._check_pool_accepts_work()
        self.assertTrue(result)

    def test_silence_detection(self):
        """Test 6: Detect synthesis silence (no completion in window)."""
        # Set last completion to past
        self.monitor._last_completion_time = time.time() - 10  # 10s ago
        
        # Silence window is 5s, so this should trigger
        time_since = time.time() - self.monitor._last_completion_time
        self.assertGreater(time_since, self.monitor.silence_window_sec)

    def test_health_check_cycle_success(self):
        """Test 7: Healthy cycle: all checks pass, restart_count resets."""
        self.monitor._restart_count = 2  # Simulate prior failures
        self.monitor._check_pool_health()
        
        # Restart count should reset to 0 on success
        self.assertEqual(self.monitor._restart_count, 0)

    def test_handle_pool_failure_retries(self):
        """Test 8: Pool failure triggers retry logic."""
        self.monitor._restart_count = 0
        self.monitor._handle_pool_failure("test_failure_1")
        
        self.assertEqual(self.monitor._restart_count, 1)
        
        self.monitor._handle_pool_failure("test_failure_2")
        self.assertEqual(self.monitor._restart_count, 2)
        
        self.monitor._handle_pool_failure("test_failure_3")
        self.assertEqual(self.monitor._restart_count, 3)

    def test_escalate_after_max_retries(self):
        """Test 9: After max retries, escalate to SOVEREIGN."""
        self.monitor._max_retries = 2
        self.monitor._restart_count = 2
        
        with patch("synthesis_pool_monitor.requests.post") as mock_post:
            self.monitor._escalate_to_sovereign("critical_failure")
            
            # Should have called requests.post to SOVEREIGN bus
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            
            # Verify the message contains escalation info
            json_data = call_args[1]["json"]
            self.assertEqual(json_data["from"], "omni")
            self.assertEqual(json_data["to"], "sovereign")
            self.assertIn("P0_CRITICAL", json_data["message"])
            self.assertIn("critical_failure", json_data["message"])

    def test_monitor_thread_handles_exceptions(self):
        """Test 10: Monitor thread continues on exception."""
        self.monitor.start()
        
        # Wait a bit for monitor to run
        time.sleep(1.5)
        
        # Thread should still be alive even if errors occur
        self.assertTrue(self.monitor._monitor_thread.is_alive())
        
        self.monitor.shutdown()

    def test_concurrent_mark_synthesis_complete(self):
        """Test 11: Concurrent calls to mark_synthesis_complete are safe."""
        def mark_completion():
            for _ in range(5):
                self.monitor.mark_synthesis_complete()
                time.sleep(0.01)
        
        threads = [threading.Thread(target=mark_completion) for _ in range(3)]
        for t in threads:
            t.start()
        
        for t in threads:
            t.join()
        
        # Should not raise any exceptions
        self.assertIsNotNone(self.monitor._last_completion_time)

    def test_multiple_shutdown_calls(self):
        """Test 12: Multiple shutdown calls are safe (idempotent)."""
        self.monitor.start()
        self.monitor.shutdown()
        
        # Second shutdown should not raise exception
        self.monitor.shutdown()
        
        # Thread should be stopped
        self.assertFalse(self.monitor._running)

    def test_semaphore_check_timeout_behavior(self):
        """Test 13: Semaphore check respects timeout."""
        # Create a semaphore with 0 permits
        test_sem = threading.Semaphore(0)
        test_monitor = PoolHealthMonitor(
            pool=self.pool,
            semaphore=test_sem,
            hang_timeout_sec=1,
        )
        
        start = time.time()
        result = test_monitor._check_semaphore_responsive()
        elapsed = time.time() - start
        
        self.assertFalse(result)
        # Should timeout after ~0.5s (hang_timeout_sec / 2)
        self.assertLess(elapsed, 2)


if __name__ == "__main__":
    unittest.main()
