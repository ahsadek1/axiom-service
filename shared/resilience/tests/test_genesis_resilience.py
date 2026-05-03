"""
test_genesis_resilience.py — GENESIS Phase 2 Resilience Tests
Spec: genesis-resilience-v1.md v1.2
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

import pytest

import shared.resilience.triad_db as triad_db
import shared.resilience.alert_router as alert_router_mod
from shared.resilience.triad_db import init_triad_db


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "genesis-test.db"
    triad_db.DB_PATH = db_path
    init_triad_db()
    yield


# ============================================================
# Pre-Deploy Gate Tests
# ============================================================

class TestPreDeployGate:

    def test_gate_passes_clean_code(self, tmp_path):
        """Gate passes when tests pass and no secrets/markers."""
        from shared.resilience.pre_deploy_gate import run_pre_deploy_gate

        # Write a clean file
        clean_file = tmp_path / "clean.py"
        clean_file.write_text("def add(a, b): return a + b\n")

        with patch("shared.resilience.pre_deploy_gate._run_tests",
                   return_value=(True, "5 passed")), \
             patch("shared.resilience.pre_deploy_gate._create_cipher_checklist",
                   return_value="/tmp/test_checklist.json"):
            result = run_pre_deploy_gate(
                service="test-service",
                service_dir=str(tmp_path),
                changed_files=[str(clean_file)],
            )

        assert result.passed is True
        assert len(result.blockers) == 0

    def test_gate_blocked_on_test_failure(self, tmp_path):
        """Gate blocks deploy when test suite fails."""
        from shared.resilience.pre_deploy_gate import run_pre_deploy_gate

        with patch("shared.resilience.pre_deploy_gate._run_tests",
                   return_value=(False, "3 failed")), \
             patch("shared.resilience.pre_deploy_gate._create_cipher_checklist",
                   return_value="/tmp/test_checklist.json"):
            result = run_pre_deploy_gate(
                service="alpha-execution",
                service_dir=str(tmp_path),
                changed_files=[],
            )

        assert result.passed is False
        assert any("failed" in b.lower() or "Tests" in b for b in result.blockers)

    def test_gate_blocked_on_hardcoded_secret(self, tmp_path):
        """Gate blocks deploy when hardcoded secret is found."""
        from shared.resilience.pre_deploy_gate import run_pre_deploy_gate

        dirty_file = tmp_path / "config.py"
        dirty_file.write_text('api_key = "sk-proj-abc123def456ghi789jkl012"\n')

        with patch("shared.resilience.pre_deploy_gate._run_tests",
                   return_value=(True, "5 passed")), \
             patch("shared.resilience.pre_deploy_gate._create_cipher_checklist",
                   return_value="/tmp/test_checklist.json"):
            result = run_pre_deploy_gate(
                service="test-service",
                service_dir=str(tmp_path),
                changed_files=[str(dirty_file)],
            )

        assert result.passed is False
        assert any("secret" in b.lower() for b in result.blockers)

    def test_debt_markers_are_warnings_not_blockers(self, tmp_path):
        """TODO/FIXME markers warn but do not block the gate."""
        from shared.resilience.pre_deploy_gate import run_pre_deploy_gate

        warn_file = tmp_path / "work.py"
        warn_file.write_text("# TODO: clean this up\ndef fn(): pass\n")

        with patch("shared.resilience.pre_deploy_gate._run_tests",
                   return_value=(True, "5 passed")), \
             patch("shared.resilience.pre_deploy_gate._create_cipher_checklist",
                   return_value="/tmp/test_checklist.json"):
            result = run_pre_deploy_gate(
                service="test-service",
                service_dir=str(tmp_path),
                changed_files=[str(warn_file)],
            )

        assert result.passed is True  # not a blocker
        assert len(result.warnings) > 0

    def test_execution_path_requires_cipher_approval(self, tmp_path):
        """Touching execution path files blocks gate until Cipher approves."""
        from shared.resilience.pre_deploy_gate import run_pre_deploy_gate, _EXECUTION_PATH_FILES

        # Mock a file that touches MAX_POSITIONS
        exec_file = tmp_path / "main.py"
        exec_file.write_text("MAX_POSITIONS = 5\n")

        checklist_path = str(tmp_path / "checklist.json")
        checklist_data = {"cipher_approved": False, "cipher_approved_at": None}
        Path(checklist_path).write_text(json.dumps(checklist_data))

        with patch("shared.resilience.pre_deploy_gate._run_tests",
                   return_value=(True, "5 passed")), \
             patch("shared.resilience.pre_deploy_gate._create_cipher_checklist",
                   return_value=checklist_path):
            result = run_pre_deploy_gate(
                service="alpha-execution",
                service_dir=str(tmp_path),
                changed_files=[str(exec_file)],
            )

        assert result.passed is False
        assert any("Cipher" in b for b in result.blockers)

    def test_gate_passes_after_cipher_approves(self, tmp_path):
        """Gate passes once Cipher sets cipher_approved: true."""
        from shared.resilience.pre_deploy_gate import run_pre_deploy_gate

        exec_file = tmp_path / "main.py"
        exec_file.write_text("MAX_POSITIONS = 3\n")

        checklist_path = str(tmp_path / "checklist.json")
        # Cipher already approved
        checklist_data = {
            "cipher_approved": True,
            "cipher_approved_at": "2026-05-02T23:00:00Z"
        }
        Path(checklist_path).write_text(json.dumps(checklist_data))

        with patch("shared.resilience.pre_deploy_gate._run_tests",
                   return_value=(True, "5 passed")), \
             patch("shared.resilience.pre_deploy_gate._create_cipher_checklist",
                   return_value=checklist_path):
            result = run_pre_deploy_gate(
                service="alpha-execution",
                service_dir=str(tmp_path),
                changed_files=[str(exec_file)],
            )

        assert result.passed is True


# ============================================================
# Stale Deploy Monitor Tests
# ============================================================

class TestStaleDeployMonitor:

    def test_no_action_when_not_stale(self):
        """No restart triggered when stale_deploy is False."""
        from shared.resilience.stale_deploy_monitor import check_stale_deploys, _stale_since
        _stale_since.clear()

        healthy_response = {"status": "healthy", "stale_deploy": False}
        with patch("shared.resilience.stale_deploy_monitor._probe_health",
                   return_value=healthy_response):
            actions = check_stale_deploys()

        assert actions == []

    def test_timer_starts_on_first_stale_detection(self):
        """First stale detection starts timer, no restart yet."""
        from shared.resilience.stale_deploy_monitor import check_stale_deploys, _stale_since
        _stale_since.clear()

        stale_response = {"status": "healthy", "stale_deploy": True, "code_hash": "old123"}
        with patch("shared.resilience.stale_deploy_monitor._probe_health",
                   return_value=stale_response):
            actions = check_stale_deploys()

        # No restart yet — just started timer
        assert actions == []
        assert len(_stale_since) > 0

    def test_restart_triggered_after_threshold(self, tmp_path):
        """Restart triggered when stale_deploy persists past threshold."""
        from shared.resilience.stale_deploy_monitor import (
            check_stale_deploys, _stale_since, STALE_THRESHOLD_SECONDS
        )
        _stale_since.clear()

        # Pre-seed with old timestamp (beyond threshold)
        _stale_since["axiom"] = time.time() - STALE_THRESHOLD_SECONDS - 60

        stale_response = {"status": "healthy", "stale_deploy": True, "code_hash": "old123"}
        healthy_response = {"status": "healthy", "stale_deploy": False, "code_hash": "new456"}

        with patch("shared.resilience.stale_deploy_monitor._probe_health",
                   return_value=stale_response), \
             patch("shared.resilience.stale_deploy_monitor._restart_service",
                   return_value=True), \
             patch("shared.resilience.stale_deploy_monitor._verify_recovery",
                   return_value=True), \
             patch("shared.resilience.stale_deploy_monitor._probe_health",
                   side_effect=[stale_response, healthy_response]):
            actions = check_stale_deploys()

        # At least one action for axiom
        axiom_actions = [a for a in actions if a.get("service") == "axiom"]
        assert len(axiom_actions) == 1
        assert axiom_actions[0]["outcome"] in ("recovered", "escalated")

    def test_escalation_when_still_stale_after_restart(self, tmp_path):
        """Escalation triggered when service still stale after restart."""
        from shared.resilience.stale_deploy_monitor import (
            check_stale_deploys, _stale_since, STALE_THRESHOLD_SECONDS
        )
        from shared.resilience import alert_router as ar
        _stale_since.clear()
        _stale_since["omni"] = time.time() - STALE_THRESHOLD_SECONDS - 60

        still_stale = {"status": "healthy", "stale_deploy": True, "code_hash": "old"}

        with patch("shared.resilience.stale_deploy_monitor._probe_health",
                   return_value=still_stale), \
             patch("shared.resilience.stale_deploy_monitor._restart_service",
                   return_value=True), \
             patch("shared.resilience.stale_deploy_monitor._verify_recovery",
                   return_value=False), \
             patch.object(ar, "_send_sovereign", return_value=True), \
             patch.object(ar, "_send_telegram", return_value=True):
            actions = check_stale_deploys()

        omni_actions = [a for a in actions if a.get("service") == "omni"]
        assert len(omni_actions) == 1
        assert omni_actions[0]["outcome"] == "escalated"
