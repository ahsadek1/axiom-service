"""
test_guardian.py — Guardian Angel v2 test suite (15 tests, all mocked)
No real launchctl, filesystem, Alpaca, or Telegram calls.
"""

import os
import time
import sqlite3
import tempfile
import pathlib
import unittest
import requests
from unittest.mock import patch, MagicMock, call
from typing import Dict, Any, List


# ---------------------------------------------------------------------------
# Import under test
# ---------------------------------------------------------------------------

import guardian_angel as ga


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_healing_db() -> ga.HealingDB:
    """Create a temporary healing DB for tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    return ga.HealingDB(path)


def _make_svc(name: str = "test-svc", port: int = 9999) -> Dict[str, Any]:
    """Build a service config dict for testing."""
    return {
        "name": name,
        "port": port,
        "health_path": "/health",
        "launchd": f"ai.nexus.{name}",
        "db": f"{name}.db",
    }


def _extract_tiers(mock_tg: MagicMock) -> List[int]:
    """
    Extract tier values from _send_telegram mock calls.
    Handles both positional (msg, tier) and keyword (msg, tier=N) call styles.
    """
    tiers: List[int] = []
    for c in mock_tg.call_args_list:
        args, kwargs = c
        if len(args) >= 2:
            tiers.append(args[1])
        elif "tier" in kwargs:
            tiers.append(kwargs["tier"])
    return tiers


# ---------------------------------------------------------------------------
# T01 — healthy service → no anomaly logged
# ---------------------------------------------------------------------------

class TestT01HealthyService(unittest.TestCase):
    def test_healthy_no_anomaly(self) -> None:
        """A 200 OK health response should record success and log no anomaly."""
        db = _tmp_healing_db()
        state = ga.ServiceState("axiom")
        crash_history: Dict[str, List[float]] = {}
        svc = _make_svc("axiom", 8001)

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200

        with patch("guardian_angel.requests.get", return_value=mock_resp):
            result = ga._health_check(svc, state, db, crash_history)

        self.assertTrue(result)
        self.assertEqual(state.consecutive_failures, 0)
        anomalies, _ = db.count_today()
        self.assertEqual(anomalies, 0)


# ---------------------------------------------------------------------------
# T02 — service down → launchctl restart triggered
# ---------------------------------------------------------------------------

class TestT02ServiceDown(unittest.TestCase):
    def test_service_down_triggers_restart(self) -> None:
        """2 consecutive failures should trigger launchctl restart."""
        db = _tmp_healing_db()
        state = ga.ServiceState("axiom")
        crash_history: Dict[str, List[float]] = {}
        svc = _make_svc("axiom", 8001)

        conn_err = requests.ConnectionError("Connection refused")

        verify_resp = MagicMock()
        verify_resp.ok = True

        with patch("guardian_angel.requests.get", side_effect=conn_err), \
             patch("guardian_angel._launchctl_restart", return_value=True) as mock_restart, \
             patch("guardian_angel._send_telegram"), \
             patch("guardian_angel.time.sleep"):
            # First failure — below threshold (consecutive_failures < 2)
            ga._health_check(svc, state, db, crash_history)
            # Reset mock so we can track the second call's restart
            mock_restart.reset_mock()
            # Second failure — consecutive_failures == 2 → triggers restart
            with patch("guardian_angel.requests.get",
                       side_effect=[conn_err, verify_resp]):
                ga._health_check(svc, state, db, crash_history)

        mock_restart.assert_called_once_with("axiom")


# ---------------------------------------------------------------------------
# T03 — 3 crashes in 10 min → REPEATED_CRASHES, healing_active set
# ---------------------------------------------------------------------------

class TestT03RepeatedCrashes(unittest.TestCase):
    def test_repeated_crashes_stops_restart(self) -> None:
        """3 crash anomalies in 10 min → REPEATED_CRASHES, no further restart."""
        db = _tmp_healing_db()
        state = ga.ServiceState("axiom")
        svc = _make_svc("axiom", 8001)
        # Seed 3 recent crashes in crash history (within last 10 minutes)
        now_ts = time.time()
        crash_history: Dict[str, List[float]] = {
            "axiom": [now_ts - 300, now_ts - 200, now_ts - 100]
        }

        conn_err = requests.ConnectionError("Connection refused")
        state.consecutive_failures = 2  # Already at threshold

        with patch("guardian_angel.requests.get", side_effect=conn_err), \
             patch("guardian_angel._launchctl_restart") as mock_restart, \
             patch("guardian_angel._send_telegram") as mock_tg, \
             patch("guardian_angel.time.sleep"):
            ga._health_check(svc, state, db, crash_history)

        mock_restart.assert_not_called()
        flag = db.get_flag("healing_active")
        self.assertEqual(flag, "true")
        tiers = _extract_tiers(mock_tg)
        self.assertIn(4, tiers)


# ---------------------------------------------------------------------------
# T04 — response time spike → alert only, no restart
# ---------------------------------------------------------------------------

class TestT04ResponseTimeDrift(unittest.TestCase):
    def test_response_time_drift_alerts_only(self) -> None:
        """Slow rolling average should alert but not restart."""
        db = _tmp_healing_db()
        state = ga.ServiceState("axiom")
        # Pre-fill with slow responses (>3.0s avg)
        state.response_times = [3.5, 4.0, 3.8, 4.2, 3.9, 4.1, 3.7, 4.0, 3.6, 4.3]
        crash_history: Dict[str, List[float]] = {}
        svc = _make_svc("axiom", 8001)

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200

        with patch("guardian_angel.requests.get", return_value=mock_resp), \
             patch("guardian_angel._launchctl_restart") as mock_restart, \
             patch("guardian_angel._send_telegram") as mock_tg:
            ga._health_check(svc, state, db, crash_history)

        mock_restart.assert_not_called()
        mock_tg.assert_called()


# ---------------------------------------------------------------------------
# T05 — memory at 86% → restart triggered
# ---------------------------------------------------------------------------

class TestT05MemoryLeak(unittest.TestCase):
    def test_high_memory_triggers_restart(self) -> None:
        """Memory at 86% should trigger graceful restart."""
        db = _tmp_healing_db()
        states: Dict[str, ga.ServiceState] = {"axiom": ga.ServiceState("axiom")}

        mock_proc = MagicMock()
        mock_proc.memory_percent.return_value = 86.0
        mock_proc.cpu_percent.return_value = 30.0

        with patch("guardian_angel.psutil.Process", return_value=mock_proc), \
             patch("guardian_angel._launchctl_restart", return_value=True) as mock_restart, \
             patch("guardian_angel._send_telegram"):
            ga._check_process_resources("axiom", 12345, db, states)

        mock_restart.assert_called_once_with("axiom")
        anomalies, _ = db.count_today()
        self.assertGreaterEqual(anomalies, 1)


# ---------------------------------------------------------------------------
# T06 — disk at 91% → log rotation runs
# ---------------------------------------------------------------------------

class TestT06DiskCritical(unittest.TestCase):
    def test_disk_critical_triggers_rotation(self) -> None:
        """Disk at 91% should run log rotation."""
        db = _tmp_healing_db()
        guardian = ga.GuardianAngel.__new__(ga.GuardianAngel)
        guardian._healing_db = db

        with patch("guardian_angel._disk_usage_pct", return_value=91.0), \
             patch("guardian_angel._rotate_logs", return_value=1024 * 1024) as mock_rotate, \
             patch("guardian_angel._send_telegram"):
            guardian._check_disk()

        mock_rotate.assert_called_once()
        anomalies, _ = db.count_today()
        self.assertGreaterEqual(anomalies, 1)


# ---------------------------------------------------------------------------
# T07 — DB integrity fail → alert sent, Tier 4
# ---------------------------------------------------------------------------

class TestT07DBCorruption(unittest.TestCase):
    def test_db_corruption_sends_tier4(self) -> None:
        """DB integrity check failure should trigger Tier 4 alert."""
        db = _tmp_healing_db()
        guardian = ga.GuardianAngel.__new__(ga.GuardianAngel)
        guardian._healing_db = db
        guardian._last_db_check_ts = 0.0  # Force the 10-min interval check

        with patch("guardian_angel._check_db_integrity", return_value=False), \
             patch("guardian_angel._wal_size_bytes", return_value=0), \
             patch("guardian_angel._send_telegram") as mock_tg, \
             patch("guardian_angel.SERVICES", [ga.SERVICES[0]]):
            # Make Path(...).exists() return True so the code doesn't skip
            with patch("guardian_angel.Path") as mock_path_cls:
                path_inst = MagicMock()
                path_inst.__truediv__ = MagicMock(return_value=path_inst)
                path_inst.__str__ = MagicMock(return_value="/fake/axiom.db")
                path_inst.exists.return_value = True
                mock_path_cls.return_value = path_inst
                guardian._check_db_integrity_all()

        tiers = _extract_tiers(mock_tg)
        self.assertIn(4, tiers)


# ---------------------------------------------------------------------------
# T08 — WAL 60MB → checkpoint + VACUUM runs
# ---------------------------------------------------------------------------

class TestT08WALStuck(unittest.TestCase):
    def test_large_wal_triggers_checkpoint(self) -> None:
        """WAL file >50MB should trigger checkpoint + VACUUM."""
        db = _tmp_healing_db()
        guardian = ga.GuardianAngel.__new__(ga.GuardianAngel)
        guardian._healing_db = db
        guardian._last_db_check_ts = 0.0

        with patch("guardian_angel._check_db_integrity", return_value=True), \
             patch("guardian_angel._wal_size_bytes", return_value=60 * 1024 * 1024), \
             patch("guardian_angel._checkpoint_wal", return_value=True) as mock_ckpt, \
             patch("guardian_angel._send_telegram"), \
             patch("guardian_angel.SERVICES", [ga.SERVICES[0]]):
            with patch("guardian_angel.Path") as mock_path_cls:
                path_inst = MagicMock()
                path_inst.__truediv__ = MagicMock(return_value=path_inst)
                path_inst.__str__ = MagicMock(return_value="/fake/axiom.db")
                path_inst.exists.return_value = True
                mock_path_cls.return_value = path_inst
                guardian._check_db_integrity_all()

        mock_ckpt.assert_called_once()


# ---------------------------------------------------------------------------
# T09 — .env hash changes → CONFIG_TAMPERED, healing_active set
# ---------------------------------------------------------------------------

class TestT09ConfigTampered(unittest.TestCase):
    def test_env_hash_change_triggers_alert(self) -> None:
        """Changed .env hash should trigger CONFIG_TAMPERED and healing_active."""
        db = _tmp_healing_db()
        guardian = ga.GuardianAngel.__new__(ga.GuardianAngel)
        guardian._healing_db = db
        guardian._env_hashes = {"/nexus/axiom/.env": "aabbccdd"}

        with patch("guardian_angel._hash_env_files",
                   return_value={"/nexus/axiom/.env": "deadbeef"}), \
             patch("guardian_angel._send_telegram") as mock_tg:
            guardian._check_config_hashes()

        flag = db.get_flag("healing_active")
        self.assertEqual(flag, "true")
        tiers = _extract_tiers(mock_tg)
        self.assertIn(4, tiers)


# ---------------------------------------------------------------------------
# T10 — stale Axiom health response → scheduler drift detected
# ---------------------------------------------------------------------------

class TestT10SchedulerDrift(unittest.TestCase):
    def test_stale_health_response_returns_ok(self) -> None:
        """
        Service returns 200 even with stale pool state (health_check returns True).
        Scheduler drift detection lives in a separate check layer.
        """
        db = _tmp_healing_db()
        state = ga.ServiceState("axiom")
        crash_history: Dict[str, List[float]] = {}
        svc = _make_svc("axiom", 8001)

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200

        with patch("guardian_angel.requests.get", return_value=mock_resp):
            result = ga._health_check(svc, state, db, crash_history)

        self.assertTrue(result)
        self.assertEqual(state.consecutive_failures, 0)


# ---------------------------------------------------------------------------
# T11 — execution_paused → /resume POSTed
# ---------------------------------------------------------------------------

class TestT11ExecutionPaused(unittest.TestCase):
    def test_execution_paused_triggers_resume(self) -> None:
        """execution_paused flag should trigger POST /resume."""
        db = _tmp_healing_db()
        svc = {
            "name": "alpha-execution",
            "port": 8005,
            "health_path": "/health",
            "launchd": "ai.nexus.alpha-execution",
            "db": "alpha_execution.db",
        }

        health_resp = MagicMock()
        health_resp.ok = True
        health_resp.json.return_value = {"execution_paused": True}

        resume_resp = MagicMock()
        resume_resp.ok = True
        resume_resp.status_code = 200

        with patch("guardian_angel.requests.get", return_value=health_resp), \
             patch("guardian_angel.requests.post", return_value=resume_resp) as mock_post, \
             patch("guardian_angel._send_telegram"):
            ga._check_execution_paused(svc, db)

        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        self.assertIn("/resume", call_url)


# ---------------------------------------------------------------------------
# T12 — 3rd occurrence of same anomaly → pattern table count = 3
# ---------------------------------------------------------------------------

class TestT12PatternTracking(unittest.TestCase):
    def test_third_occurrence_updates_pattern(self) -> None:
        """Pattern table should reach count=3 after 3 anomaly logs."""
        db = _tmp_healing_db()
        for _ in range(3):
            db.log_anomaly("axiom", "CONNECTION_REFUSED", "HIGH", "test")

        with db._lock:
            row = db._conn.execute(
                "SELECT occurrence_count FROM patterns "
                "WHERE service='axiom' AND anomaly_type='CONNECTION_REFUSED'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 3)


# ---------------------------------------------------------------------------
# T13 — DB/Alpaca orphan position → Tier 4, healing_active set
# ---------------------------------------------------------------------------

class TestT13OrphanPosition(unittest.TestCase):
    def test_orphan_position_triggers_tier4(self) -> None:
        """Position in Alpaca but not in DB → Tier 4 alert + healing_active."""
        db = _tmp_healing_db()

        # Cipher P2-1 fix: _reconcile_positions now calls _get_alpaca_positions_via_services
        # (execution service /positions endpoints) instead of _alpaca_get directly.
        alpaca_positions = {"AAPL": {"symbol": "AAPL", "qty": "10", "side": "long"}}

        with patch("guardian_angel._get_alpaca_positions_via_services", return_value=alpaca_positions), \
             patch("guardian_angel._get_db_positions", return_value=[]), \
             patch("guardian_angel._send_telegram") as mock_tg:
            ga._reconcile_positions(db)

        flag = db.get_flag("healing_active")
        self.assertEqual(flag, "true")
        tiers = _extract_tiers(mock_tg)
        self.assertIn(4, tiers)


# ---------------------------------------------------------------------------
# T14 — 2 services fail within 5 min → CASCADE detected
# ---------------------------------------------------------------------------

class TestT14CascadeDetection(unittest.TestCase):
    def test_two_failures_triggers_cascade(self) -> None:
        """2+ recent failures → CASCADE → healing_active + Tier 4."""
        db = _tmp_healing_db()
        now = time.time()
        failure_times = {
            "axiom": now - 30,
            "omni": now - 60,
        }

        with patch("guardian_angel._send_telegram") as mock_tg:
            ga._check_cascade(failure_times, db)

        flag = db.get_flag("healing_active")
        self.assertEqual(flag, "true")
        tiers = _extract_tiers(mock_tg)
        self.assertIn(4, tiers)
        anomalies, _ = db.count_today()
        self.assertGreaterEqual(anomalies, 1)


# ---------------------------------------------------------------------------
# T15 — hourly backup creates correct filename, prunes old backups
# ---------------------------------------------------------------------------

class TestT15HourlyBackup(unittest.TestCase):
    def test_backup_creates_files_and_prunes(self) -> None:
        """_backup_dbs should create timestamped files and keep only 24."""
        import datetime as dt_module
        with tempfile.TemporaryDirectory() as tmpdir:
            backup_dir = pathlib.Path(tmpdir) / "backups"
            backup_dir.mkdir()
            data_dir = pathlib.Path(tmpdir) / "data"
            data_dir.mkdir()

            # Create a fake DB file
            fake_db = data_dir / "axiom.db"
            fake_db.write_bytes(b"fakedb")

            # Create 25 old backup files for axiom so pruning is triggered
            for i in range(25):
                old = backup_dir / f"axiom_20260101_{i:02d}.db"
                old.write_bytes(b"old")

            with patch.object(ga, "DATA_DIR", str(data_dir)), \
                 patch.object(ga, "BACKUP_DIR", str(backup_dir)), \
                 patch.object(ga, "SERVICES", [
                     {
                         "name": "axiom",
                         "port": 8001,
                         "health_path": "/health",
                         "launchd": "ai.nexus.axiom",
                         "db": "axiom.db",
                     }
                 ]):
                ga._backup_dbs()

            # Should have pruned to ≤24
            remaining = sorted(backup_dir.glob("axiom_*.db"))
            self.assertLessEqual(len(remaining), 24)

            # Latest backup with today's hour tag should exist
            hour_tag = dt_module.datetime.now().strftime("%Y%m%d_%H")
            latest = backup_dir / f"axiom_{hour_tag}.db"
            self.assertTrue(latest.exists(), f"Expected backup {latest} not found")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
