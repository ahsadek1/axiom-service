"""
test_g5_snapshot_clearance.py — G5 pre-market snapshot and clearance check tests.

Tests T1–T8 from the AUTO_EXECUTE spec.
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add scripts dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pre_market_snapshot
import clearance_check


def _make_snapshot(auto_execute: bool, sign: bool = True) -> dict:
    """Build a well-formed snapshot dict."""
    snap = {
        "captured_at": "2026-04-19T06:00:00+00:00",
        "nexus_auto_execute": auto_execute,
        "alpha_auto_execute": auto_execute,
        "prime_auto_execute": auto_execute,
        "alpha_mode": "live" if auto_execute else "dry_run",
        "prime_mode": "live" if auto_execute else "dry_run",
        "alpha_reachable": True,
        "prime_reachable": True,
    }
    if sign:
        snap_bytes = json.dumps(snap, sort_keys=True).encode()
        snap["sha256"] = hashlib.sha256(snap_bytes).hexdigest()
    return snap


class TestClearanceCheck(unittest.TestCase):
    def _write_snapshot(self, snap: dict, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(snap, f)

    def test_t1_snapshot_missing_blocks(self):
        """T1: No snapshot file → BLOCKED (exit 1)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = os.path.join(tmpdir, "no_snapshot.json")
            with patch.object(clearance_check, "SNAPSHOT_PATH", missing_path), \
                 patch.object(clearance_check, "_alert_ahmed"):
                result = clearance_check.main()
            self.assertEqual(result, 1)

    def test_t2_cleared_when_states_match(self):
        """T2: Snapshot=false, live=false → CLEARED."""
        snap = _make_snapshot(False)
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = os.path.join(tmpdir, "snapshot.json")
            self._write_snapshot(snap, snap_path)
            mock_health = {"auto_execute": False, "mode": "dry_run"}
            with patch.object(clearance_check, "SNAPSHOT_PATH", snap_path), \
                 patch.object(clearance_check, "_query_health", return_value=mock_health), \
                 patch.object(clearance_check, "_alert_ahmed"):
                result = clearance_check.main()
            self.assertEqual(result, 0)

    def test_t3_mismatch_blocks_false_to_true(self):
        """T3: Snapshot=false but live=true → BLOCKED."""
        snap = _make_snapshot(False)
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = os.path.join(tmpdir, "snapshot.json")
            self._write_snapshot(snap, snap_path)
            mock_health = {"auto_execute": True, "mode": "live"}
            with patch.object(clearance_check, "SNAPSHOT_PATH", snap_path), \
                 patch.object(clearance_check, "_query_health", return_value=mock_health), \
                 patch.object(clearance_check, "_alert_ahmed") as mock_alert:
                result = clearance_check.main()
            self.assertEqual(result, 1)
            mock_alert.assert_called_once()
            alert_text = mock_alert.call_args[0][0]
            self.assertIn("MISMATCH", alert_text)

    def test_t4_mismatch_blocks_true_to_false(self):
        """T4: Snapshot=true but live=false → BLOCKED."""
        snap = _make_snapshot(True)
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = os.path.join(tmpdir, "snapshot.json")
            self._write_snapshot(snap, snap_path)
            mock_health = {"auto_execute": False, "mode": "dry_run"}
            with patch.object(clearance_check, "SNAPSHOT_PATH", snap_path), \
                 patch.object(clearance_check, "_query_health", return_value=mock_health), \
                 patch.object(clearance_check, "_alert_ahmed") as mock_alert:
                result = clearance_check.main()
            self.assertEqual(result, 1)
            mock_alert.assert_called_once()

    def test_t5_tampered_snapshot_blocked(self):
        """T5: SHA256 mismatch → BLOCKED."""
        snap = _make_snapshot(False)
        snap["nexus_auto_execute"] = True  # tamper after signing
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = os.path.join(tmpdir, "snapshot.json")
            self._write_snapshot(snap, snap_path)
            with patch.object(clearance_check, "SNAPSHOT_PATH", snap_path), \
                 patch.object(clearance_check, "_alert_ahmed") as mock_alert:
                result = clearance_check.main()
            self.assertEqual(result, 1)
            mock_alert.assert_called_once()
            self.assertIn("SHA256", mock_alert.call_args[0][0])

    def test_t6_unreachable_service_is_degraded(self):
        """T6: One service unreachable → exit 2 (degraded, not hard block)."""
        snap = _make_snapshot(False)
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = os.path.join(tmpdir, "snapshot.json")
            self._write_snapshot(snap, snap_path)

            def _health_side_effect(url, *args, **kwargs):
                if "8006" in url:
                    return {}  # prime unreachable
                return {"auto_execute": False, "mode": "dry_run"}

            with patch.object(clearance_check, "SNAPSHOT_PATH", snap_path), \
                 patch.object(clearance_check, "_query_health", side_effect=_health_side_effect), \
                 patch.object(clearance_check, "_alert_ahmed"):
                result = clearance_check.main()
            self.assertEqual(result, 2)


class TestSnapshotScript(unittest.TestCase):
    def test_t7_snapshot_writes_file(self):
        """T7: Snapshot script writes valid JSON with sha256 field."""
        mock_health = {"auto_execute": False, "mode": "dry_run"}
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = os.path.join(tmpdir, "snapshot.json")
            with patch.object(pre_market_snapshot, "SNAPSHOT_PATH", snap_path), \
                 patch.object(pre_market_snapshot, "_query_health", return_value=mock_health), \
                 patch.object(pre_market_snapshot, "_alert_ahmed"):
                result = pre_market_snapshot.main()
            self.assertEqual(result, 0)
            with open(snap_path) as f:
                snap = json.load(f)
            self.assertIn("sha256", snap)
            self.assertIn("nexus_auto_execute", snap)
            self.assertFalse(snap["nexus_auto_execute"])

    def test_t8_unreachable_returns_1_and_alerts(self):
        """T8: If alpha unreachable at 6 AM → exit 1, alert sent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = os.path.join(tmpdir, "snapshot.json")
            with patch.object(pre_market_snapshot, "SNAPSHOT_PATH", snap_path), \
                 patch.object(pre_market_snapshot, "_query_health", return_value={}), \
                 patch.object(pre_market_snapshot, "_alert_ahmed") as mock_alert:
                result = pre_market_snapshot.main()
            self.assertEqual(result, 1)
            mock_alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
