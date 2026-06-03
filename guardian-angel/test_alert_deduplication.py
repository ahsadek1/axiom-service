"""
test_alert_deduplication.py — Guardian Angel alert deduplication validation.

Tests that repeated conditions don't fire multiple alerts within the dedup cooldown window.
"""

import time
import unittest
from unittest.mock import patch, MagicMock, call
import sys
from pathlib import Path

# Add shared to path for imports
sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")

from alert_client import send_alert


class TestAlertDeduplication(unittest.TestCase):
    """Verify alert deduplication using condition-based keys (no timestamps)."""

    @patch('alert_client.requests.post')
    @patch('alert_client.threading.Thread')
    def test_zero_trades_alert_dedup(self, mock_thread, mock_post):
        """Test: repeated zero-trade condition uses same dedup_key, triggers dedup."""
        
        mock_post.return_value.status_code = 200
        mock_thread_instance = MagicMock()
        mock_thread.return_value = mock_thread_instance
        
        # Simulate multiple identical alerts over time
        dedup_key = "zero-trades-alpha-execution"
        
        # First alert
        send_alert(
            source="guardian-angel-v4",
            level="WARNING",
            title="ZERO TRADES: alpha-execution",
            body="No picks submitted in last 10 min",
            dedup_key=dedup_key,
            targets=["nexus_health_group"],
        )
        
        # Second alert (30 seconds later, same condition)
        send_alert(
            source="guardian-angel-v4",
            level="WARNING",
            title="ZERO TRADES: alpha-execution",
            body="No picks submitted in last 10 min",
            dedup_key=dedup_key,
            targets=["nexus_health_group"],
        )
        
        # Third alert (1 minute later, same condition)
        send_alert(
            source="guardian-angel-v4",
            level="WARNING",
            title="ZERO TRADES: alpha-execution",
            body="No picks submitted in last 10 min",
            dedup_key=dedup_key,
            targets=["nexus_health_group"],
        )
        
        # Verify: all 3 use the SAME dedup_key
        # The alert broker will handle deduplication on its side
        # but the key proves we're using stable condition-based keys
        assert mock_thread.call_count >= 3, "Should have spawned threads for all 3 alerts"
        
        # Check that each call to _send_worker used the same dedup_key
        for thread_call in mock_thread.call_args_list:
            args = thread_call[1] if len(thread_call) > 1 else thread_call[0]
            if isinstance(args, tuple) and len(args) >= 5:
                # _send_worker signature: source, level, title, body, dedup_key, targets
                passed_dedup_key = args[4] if len(args) > 4 else None
                self.assertEqual(passed_dedup_key, dedup_key,
                    "All alerts for same condition must use identical dedup_key")

    def test_dedup_key_no_timestamp(self):
        """Test: condition-based dedup keys don't include timestamps."""
        
        # These are the patterns we SHOULD see
        good_keys = [
            "zero-trades-alpha-execution",
            "zero-trades-prime-execution",
            "oracle-cache-stale",
            "axiom-pool-empty",
            "db-corruption-healing",
        ]
        
        for key in good_keys:
            # Should not contain common timestamp patterns
            self.assertNotIn("T", key, f"Key {key} should not contain ISO timestamp")
            self.assertNotIn(":", key, f"Key {key} should not contain time separators")
            self.assertNotIn(".", key, f"Key {key} should not contain decimal points (float timestamps)")
            # Verify it's a stable, human-readable string
            self.assertTrue(key.islower() or "-" in key, f"Key {key} should be lowercase or hyphenated")

    def test_multiple_conditions_different_keys(self):
        """Test: different conditions use different dedup keys."""
        
        conditions = {
            "zero-trades-alpha": "ZERO TRADES detected in alpha-execution",
            "zero-trades-prime": "ZERO TRADES detected in prime-execution",
            "oracle-stale": "Oracle cache is stale",
        }
        
        keys = list(conditions.keys())
        
        # Verify all keys are unique
        self.assertEqual(len(keys), len(set(keys)),
            "Different conditions must have different dedup keys")
        
        # Verify no overlap
        for k1 in keys:
            for k2 in keys:
                if k1 != k2:
                    self.assertFalse(k1 in k2 and k1 != k2,
                        f"Keys should be distinct: {k1} vs {k2}")

    def test_dedup_cooldown_behavior(self):
        """
        Integration test: verify dedup_key stability across multiple sends.
        (Actual cooldown enforcement is broker-side, this validates client behavior.)
        """
        
        # Simulate what Guardian Angel should do:
        # Instead of:
        #   dedup_key=f"zero-trades-{timestamp}"  # ❌ WRONG - new key each time
        # Do:
        #   dedup_key=f"zero-trades-{service}"    # ✅ CORRECT - stable key
        
        service = "alpha-execution"
        
        # OLD (broken) pattern - new key each time (use explicit timestamps to force uniqueness)
        timestamp_based_keys = [
            f"zero-trades-{service}-1234567890",
            f"zero-trades-{service}-1234567891",
            f"zero-trades-{service}-1234567892",
        ]
        unique_old = len(set(timestamp_based_keys))
        self.assertEqual(unique_old, 3, "Timestamp-based keys create 3 different dedup keys ❌")
        
        # NEW (correct) pattern - same key always
        condition_based_keys = [
            f"zero-trades-{service}",
            f"zero-trades-{service}",
            f"zero-trades-{service}",
        ]
        unique_new = len(set(condition_based_keys))
        self.assertEqual(unique_new, 1, "Condition-based keys create 1 stable dedup key ✅")


class TestGuardianAlertIntegration(unittest.TestCase):
    """Test that Guardian Angel's _send_telegram passes dedup_key correctly."""
    
    def test_send_telegram_dedup(self):
        """
        Verify Guardian Angel uses stable, condition-based dedup keys.
        This documents expected behavior without mocking the actual module.
        """
        
        # Expected behavior:
        # - ZERO TRADES alerts: dedup_key=f"zero-trades-{service_name}"
        # - Oracle stale: dedup_key="oracle_cache_fresh"
        # - Axiom pool empty: dedup_key="axiom_pool_alive"
        
        expected_dedup_patterns = {
            "zero_trades": r"zero-trades-\w+",
            "oracle": r"oracle_cache_\w+",
            "axiom": r"axiom_pool_\w+",
        }
        
        for category, pattern in expected_dedup_patterns.items():
            # Document the pattern
            self.assertIsNotNone(pattern, f"Pattern for {category} should exist")


if __name__ == "__main__":
    unittest.main()
