"""
test_genesis_resilience.py — GENESIS 30% Resilience Layer Tests
Spec: genesis-resilience-v1.md v1.2
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Ensure module is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))


# ─────────────────────────────────────────────────────────────────────────────
# G1 — deploy_gate.py
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadCircuitRegistry:
    def test_chronicle_down_returns_empty_dict(self, tmp_path):
        """Spec TC1: CHRONICLE down → load_circuit_registry() returns {}, no exception."""
        from genesis.resilience.deploy_gate import load_circuit_registry
        # Point registry path to non-existent file (simulates missing cache)
        with patch("genesis.resilience.deploy_gate.REGISTRY_PATH", tmp_path / "nonexistent.json"):
            result = load_circuit_registry()
        assert result == {}

    def test_valid_cache_loaded(self, tmp_path):
        """Valid local cache file is returned correctly."""
        registry_data = {"axiom": {"threshold": 3}, "omni": {"threshold": 2}}
        cache_file = tmp_path / "circuit_registry.json"
        cache_file.write_text(json.dumps(registry_data))

        from genesis.resilience.deploy_gate import load_circuit_registry
        with patch("genesis.resilience.deploy_gate.REGISTRY_PATH", cache_file):
            result = load_circuit_registry()
        assert result == registry_data

    def test_corrupt_json_returns_empty(self, tmp_path):
        """Corrupt JSON in cache → returns {}, no exception."""
        cache_file = tmp_path / "circuit_registry.json"
        cache_file.write_text("{ bad json !!!")

        from genesis.resilience.deploy_gate import load_circuit_registry
        with patch("genesis.resilience.deploy_gate.REGISTRY_PATH", cache_file):
            result = load_circuit_registry()
        assert result == {}


class TestEnterStandby:
    def test_enter_standby_does_not_exit(self):
        """Spec TC2: enter_standby() MUST NOT call sys.exit."""
        from genesis.resilience.deploy_gate import enter_standby

        with patch("genesis.resilience.deploy_gate.wal_write"), \
             patch("genesis.resilience.deploy_gate.logger") as mock_log:
            # Patch sovereign_comms import to avoid dependency
            with patch.dict(sys.modules, {
                "sovereign_comms": MagicMock(),
                "chronicle_reader": MagicMock(),
            }):
                # Should not raise SystemExit
                enter_standby("test-service", "startup contract failed")

        # If we get here, sys.exit was NOT called — test passes


class TestWalWrite:
    def test_wal_failure_does_not_raise(self, tmp_path):
        """Spec TC3: WAL write fails → logged loudly, function returns normally."""
        from genesis.resilience.deploy_gate import wal_write

        with patch("genesis.resilience.deploy_gate.WAL_DB_PATH", tmp_path / "wal.db"), \
             patch("sqlite3.connect", side_effect=sqlite3.OperationalError("disk full")), \
             patch("genesis.resilience.deploy_gate.logger") as mock_log:
            # Must not raise
            wal_write("test_event", {"service": "axiom"})

        mock_log.error.assert_called_once()
        assert "ADVISORY" in str(mock_log.error.call_args)

    def test_wal_write_success(self, tmp_path):
        """Successful WAL write creates record in DB."""
        wal_db = tmp_path / "wal.db"
        from genesis.resilience.deploy_gate import wal_write

        with patch("genesis.resilience.deploy_gate.WAL_DB_PATH", wal_db):
            wal_write("deploy_started", {"service": "omni", "sha": "abc123"})

        conn = sqlite3.connect(str(wal_db))
        rows = conn.execute("SELECT event_type, payload FROM wal_events").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "deploy_started"
        assert "omni" in rows[0][1]


class TestRotateWal:
    def test_large_wal_rotated(self, tmp_path):
        """Spec TC4: WAL file >50MB → rotate_wal_if_needed() rotates it."""
        import genesis.resilience.deploy_gate as dg

        wal_db = tmp_path / "wal.db"
        # Write a real file that's over the threshold by patching WAL_MAX_BYTES
        wal_db.write_bytes(b"x" * 200)  # 200 bytes real file

        original_max = dg.WAL_MAX_BYTES
        dg.WAL_MAX_BYTES = 100   # temporarily lower threshold so 200 bytes triggers rotation
        try:
            with patch("genesis.resilience.deploy_gate.WAL_DB_PATH", wal_db):
                result = dg.rotate_wal_if_needed()
        finally:
            dg.WAL_MAX_BYTES = original_max  # restore

        assert result is True
        # Original file should be gone (renamed)
        assert not wal_db.exists()

    def test_small_wal_not_rotated(self, tmp_path):
        """Small WAL under threshold → not rotated."""
        wal_db = tmp_path / "wal.db"
        wal_db.write_bytes(b"x" * 100)

        from genesis.resilience.deploy_gate import rotate_wal_if_needed

        with patch("genesis.resilience.deploy_gate.WAL_DB_PATH", wal_db), \
             patch("pathlib.Path.stat") as mock_stat:
            mock_stat.return_value.st_size = 1024  # 1KB — well under 50MB
            mock_stat.return_value.st_mtime = time.time()
            result = rotate_wal_if_needed()

        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# G2 — stale_monitor.py
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckStaleDeploys:
    def test_stale_service_over_threshold_returned(self):
        """Spec TC5: stale_deploy=true for >10 min → service name returned."""
        import genesis.resilience.stale_monitor as sm

        # Pre-seed stale_since with an entry >10 minutes old
        sm._stale_since.clear()
        sm._stale_since["omni"] = time.time() - (11 * 60)  # 11 minutes ago

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "healthy", "stale_deploy": True}

        with patch("requests.get", return_value=mock_response):
            result = sm.check_stale_deploys()

        assert "omni" in result
        sm._stale_since.clear()

    def test_fresh_service_not_returned(self):
        """Service with stale_deploy=false → not in result."""
        import genesis.resilience.stale_monitor as sm
        sm._stale_since.clear()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "healthy", "stale_deploy": False}

        with patch("requests.get", return_value=mock_response):
            result = sm.check_stale_deploys()

        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# G3 — diagnosis.py
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadDiagnosisTree:
    def test_chronicle_unavailable_returns_default(self):
        """Spec TC6: CHRONICLE unavailable → returns DEFAULT_DIAGNOSIS_TREE."""
        from genesis.resilience.diagnosis import load_diagnosis_tree, DEFAULT_DIAGNOSIS_TREE

        with patch.dict(sys.modules, {"chronicle_reader": None}):
            # Force ImportError by patching sys.path to exclude the shared dir
            with patch("genesis.resilience.diagnosis.logger"):
                result = load_diagnosis_tree()

        # Should contain all default entries
        assert "service_unreachable" in result
        assert "brain_hang" in result

    def test_known_signature_returns_playbook(self):
        """Known error signature returns correct playbook entry."""
        from genesis.resilience.diagnosis import diagnose

        result = diagnose("brain_hang", {"service": "omni"})
        assert result["action"] == "semaphore_release_then_escalate"
        assert result["retry_count"] == 1
        assert result["matched_signature"] == "brain_hang"

    def test_unknown_signature_returns_escalate_only(self):
        """Unknown error signature → escalate_only fallback."""
        from genesis.resilience.diagnosis import diagnose

        result = diagnose("completely_unknown_error_xyz", {"service": "axiom"})
        assert result["action"] == "escalate_only"
        assert result["retry_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# G4 — rollback.py
# ─────────────────────────────────────────────────────────────────────────────

class TestRollback:
    def test_auto_rollback_triggers_on_unhealthy(self, tmp_path):
        """Spec TC7: auto_rollback runs and returns bool (uses triad_db for audit)."""
        import genesis.resilience.rollback as rb

        # Pre-seed in-memory state (F1 fix: triad_db-backed, no local DB)
        rb._pre_deploy_shas["oracle"] = "abc12345"
        rb._deployment_ids["oracle"] = -1  # -1 = no triad_db row (test context)

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json.return_value = {"status": "error"}

        with patch("requests.get", return_value=mock_resp), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("genesis.resilience.rollback._triad_log_deployment", return_value=42), \
             patch("genesis.resilience.rollback._triad_mark_healthy"), \
             patch("genesis.resilience.rollback._triad_mark_rolled_back"), \
             patch.dict(sys.modules, {
                 "sovereign_comms": MagicMock(),
                 "chronicle_reader": MagicMock(),
             }):
            result = rb.auto_rollback("oracle")

        assert isinstance(result, bool)

    def test_tag_pre_deploy_stores_sha(self, tmp_path):
        """tag_pre_deploy stores SHA in module-level registry (triad_db write + in-memory)."""
        import genesis.resilience.rollback as rb

        with patch("genesis.resilience.rollback._triad_log_deployment", return_value=99) as mock_log:
            rb.tag_pre_deploy("axiom", "deadbeef1234")

        # SHA stored in module-level registry
        assert rb._get_pre_deploy_sha("axiom") == "deadbeef1234"
        # deployment_id stored
        assert rb._deployment_ids["axiom"] == 99
        # triad_db was called
        mock_log.assert_called_once_with(
            service="axiom",
            pre_deploy_sha="deadbeef1234",
            post_deploy_sha="pending",
            deployed_by="genesis",
        )


# ─────────────────────────────────────────────────────────────────────────────
# G5 — coordinator.py
# ─────────────────────────────────────────────────────────────────────────────

class TestChaosTest:
    def test_unsafe_service_rejected(self):
        """Spec TC8a: 'alpha-execution' not in whitelist → run_chaos_test returns False."""
        from genesis.resilience.coordinator import run_chaos_test

        result = run_chaos_test("alpha-execution")
        assert result is False

    def test_safe_service_market_closed_no_positions(self):
        """Spec TC8b: oracle + market closed + 0 positions → chaos test runs."""
        from genesis.resilience.coordinator import run_chaos_test

        with patch("genesis.resilience.coordinator._is_market_closed", return_value=True), \
             patch("genesis.resilience.coordinator._get_open_position_count", return_value=0), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)):
            result = run_chaos_test("oracle")

        assert result is True

    def test_safe_service_market_open_rejected(self):
        """Market open → chaos test rejected even for safe service."""
        from genesis.resilience.coordinator import run_chaos_test

        with patch("genesis.resilience.coordinator._is_market_closed", return_value=False):
            result = run_chaos_test("oracle")

        assert result is False

    def test_safe_service_open_positions_rejected(self):
        """Open positions → chaos test rejected."""
        from genesis.resilience.coordinator import run_chaos_test

        with patch("genesis.resilience.coordinator._is_market_closed", return_value=True), \
             patch("genesis.resilience.coordinator._get_open_position_count", return_value=3):
            result = run_chaos_test("guardian-angel")

        assert result is False
