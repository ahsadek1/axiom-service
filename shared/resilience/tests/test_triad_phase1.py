"""
test_triad_phase1.py — Phase 1 Infrastructure Tests
Spec: resilience-phase1-infrastructure.md v1.0
Tests: 10 required, all must pass before Phase 2 starts.
"""

import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

import pytest
from unittest.mock import patch, MagicMock

# Point DB at a temp file for all tests
_TMP_DB = tempfile.mktemp(suffix="-triad-test.db")
os.environ.setdefault("TRIAD_DB_PATH_OVERRIDE", _TMP_DB)

import shared.resilience.triad_db as triad_db
triad_db.DB_PATH = __import__("pathlib").Path(_TMP_DB)

from shared.resilience.triad_db import (
    init_triad_db,
    log_intervention,
    log_near_miss,
    log_invariant_violation,
    log_deployment,
    mark_deployment_healthy,
    mark_deployment_rolled_back,
    quarantine_directive,
    get_circuit_state,
    set_circuit_state,
    check_and_record_alert,
    _get_conn,
)
from shared.resilience.alert_router import route_alert
import shared.resilience.alert_router as alert_router_mod


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """Give every test its own clean DB."""
    db_path = tmp_path / "triad-test.db"
    triad_db.DB_PATH = db_path
    alert_router_mod.log_intervention  # ensure import
    init_triad_db()
    yield
    try:
        db_path.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Test 1: Concurrent writes from 2 agents — no corruption, both rows written
# ---------------------------------------------------------------------------

class TestConcurrentWrites:
    def test_concurrent_intervention_log_writes(self):
        """
        Two agents write to intervention_log simultaneously.
        BEGIN IMMEDIATE must serialize writes — both rows present, no corruption.
        """
        errors = []

        def write_agent(agent_name):
            try:
                log_intervention(
                    agent=agent_name,
                    action_type="service_restart",
                    target="omni",
                    trigger="health_check_failed",
                    outcome="recovered",
                    elapsed_sec=2.5,
                )
            except Exception as e:
                errors.append(str(e))

        t1 = threading.Thread(target=write_agent, args=("genesis",))
        t2 = threading.Thread(target=write_agent, args=("vector",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert not errors, f"Concurrent write errors: {errors}"

        conn = _get_conn()
        rows = conn.execute("SELECT agent FROM intervention_log ORDER BY id").fetchall()
        conn.close()
        agents = {r["agent"] for r in rows}
        assert "genesis" in agents
        assert "vector" in agents
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Test 2: P0 alert → Ahmed Telegram fires
# ---------------------------------------------------------------------------

class TestAlertRouterP0:
    def test_p0_routes_to_ahmed_and_sovereign(self):
        """P0 alert must call Telegram + SOVEREIGN bus."""
        with patch.object(alert_router_mod, "_send_telegram", return_value=True) as mock_tg, \
             patch.object(alert_router_mod, "_send_sovereign", return_value=True) as mock_sv:
            result = route_alert(
                agent="genesis",
                issue_type="service_down",
                target="omni",
                severity="P0",
                detail="OMNI health returned 503",
            )
        assert result is True
        mock_tg.assert_called_once()
        mock_sv.assert_called_once()
        # Message must contain P0 marker
        msg = mock_tg.call_args[0][0]
        assert "P0" in msg
        assert "omni" in msg


# ---------------------------------------------------------------------------
# Test 3: Same P0 from 3 agents within 1 min → one alert, not three
# ---------------------------------------------------------------------------

class TestAlertDedup:
    def test_same_p0_from_three_agents_sends_once(self):
        """Dedup: same issue_type+target+severity from 3 agents → 1 Telegram."""
        sent = []
        with patch.object(alert_router_mod, "_send_telegram",
                          side_effect=lambda m: sent.append(m) or True), \
             patch.object(alert_router_mod, "_send_sovereign", return_value=True):
            for agent in ("genesis", "vector", "cipher"):
                route_alert(
                    agent=agent,
                    issue_type="service_down",
                    target="alpha-execution",
                    severity="P0",
                    detail="service unreachable",
                )
        assert len(sent) == 1, f"Expected 1 Telegram, got {len(sent)}"


# ---------------------------------------------------------------------------
# Test 4: P1 → SOVEREIGN only, no Ahmed
# ---------------------------------------------------------------------------

class TestAlertRouterP1:
    def test_p1_routes_to_sovereign_only(self):
        """P1 must reach SOVEREIGN but NOT Ahmed."""
        with patch.object(alert_router_mod, "_send_telegram", return_value=True) as mock_tg, \
             patch.object(alert_router_mod, "_send_sovereign", return_value=True) as mock_sv:
            result = route_alert(
                agent="vector",
                issue_type="stale_deploy",
                target="axiom",
                severity="P1",
                detail="stale for 12 minutes",
            )
        assert result is True
        mock_tg.assert_not_called()
        mock_sv.assert_called_once()


# ---------------------------------------------------------------------------
# Test 5: P2 → queued for morning brief, no immediate alert
# ---------------------------------------------------------------------------

class TestAlertRouterP2:
    def test_p2_queues_to_morning_brief(self, tmp_path):
        """P2 must write to morning brief queue, not Telegram or SOVEREIGN."""
        brief_path = str(tmp_path / "morning_brief.jsonl")
        alert_router_mod._MORNING_BRIEF_PATH = brief_path

        with patch.object(alert_router_mod, "_send_telegram", return_value=True) as mock_tg, \
             patch.object(alert_router_mod, "_send_sovereign", return_value=True) as mock_sv:
            result = route_alert(
                agent="cipher",
                issue_type="near_miss",
                target="oracle",
                severity="P2",
                detail="oracle recovered from timeout 3 times this week",
            )
        assert result is True
        mock_tg.assert_not_called()
        mock_sv.assert_not_called()
        # Check queue file written
        assert os.path.exists(brief_path)
        with open(brief_path) as f:
            entry = json.loads(f.readline())
        assert entry["target"] == "oracle"
        assert entry["severity"] if "severity" in entry else True


# ---------------------------------------------------------------------------
# Test 6: RESILIENCE-DB unreachable → log_intervention logs error, never raises
# ---------------------------------------------------------------------------

class TestDBFailureSafety:
    def test_log_intervention_never_raises_on_db_failure(self, tmp_path):
        """DB failure must log error but never raise to caller."""
        triad_db.DB_PATH = tmp_path / "nonexistent_dir" / "resilience.db"
        # Must not raise
        log_intervention(
            agent="genesis",
            action_type="service_restart",
            target="omni",
            trigger="test",
            outcome="recovered",
        )
        # If we get here, it didn't raise — test passes

    def test_get_circuit_state_fails_open_on_db_error(self, tmp_path):
        """get_circuit_state returns CLOSED (fail-open) when DB is unreachable."""
        triad_db.DB_PATH = tmp_path / "bad_dir" / "resilience.db"
        state = get_circuit_state("any_service")
        assert state == "CLOSED"


# ---------------------------------------------------------------------------
# Test 7: ALERT ROUTER fails → caller's action continues unblocked
# ---------------------------------------------------------------------------

class TestAlertRouterNeverRaises:
    def test_route_alert_never_raises(self):
        """route_alert must return False (not raise) on internal failure."""
        with patch.object(alert_router_mod, "check_and_record_alert",
                          side_effect=RuntimeError("DB exploded")):
            result = route_alert(
                agent="genesis",
                issue_type="service_down",
                target="omni",
                severity="P0",
                detail="test",
            )
        # Must return False, not raise
        assert result is False


# ---------------------------------------------------------------------------
# Test 8: quarantine_directive → row written, not re-processed
# ---------------------------------------------------------------------------

class TestQuarantine:
    def test_quarantine_directive_writes_row(self):
        """Quarantined directive must be logged and not re-processable."""
        quarantine_directive(
            agent="vector",
            raw_directive='{"type": "unknown_action", "target": "???"}',
            quarantine_reason="unknown directive type",
        )
        conn = _get_conn()
        rows = conn.execute("SELECT * FROM quarantine_log").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["agent"] == "vector"
        assert "unknown directive type" in rows[0]["quarantine_reason"]


# ---------------------------------------------------------------------------
# Test 9: get_circuit_state with no registry row → returns "CLOSED" (fail-open)
# ---------------------------------------------------------------------------

class TestCircuitRegistry:
    def test_get_circuit_state_missing_returns_closed(self):
        """Service not in registry → CLOSED (fail-open default)."""
        state = get_circuit_state("nonexistent_service")
        assert state == "CLOSED"

    def test_set_and_get_circuit_state(self):
        """set then get circuit state round-trip."""
        set_circuit_state("omni", "OPEN", reason="too many failures")
        state = get_circuit_state("omni")
        assert state == "OPEN"

        set_circuit_state("omni", "CLOSED")
        state = get_circuit_state("omni")
        assert state == "CLOSED"


# ---------------------------------------------------------------------------
# Test 10: Deployment history + rollback tracking
# ---------------------------------------------------------------------------

class TestDeploymentHistory:
    def test_deployment_log_and_health_verify(self):
        """Log deployment, mark healthy, verify state."""
        dep_id = log_deployment(
            service="alpha-execution",
            pre_deploy_sha="abc123",
            post_deploy_sha="def456",
        )
        assert dep_id > 0

        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM deployment_history WHERE id=?", (dep_id,)
        ).fetchone()
        conn.close()
        assert row["health_verified"] == 0
        assert row["rolled_back"] == 0

        mark_deployment_healthy(dep_id)
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM deployment_history WHERE id=?", (dep_id,)
        ).fetchone()
        conn.close()
        assert row["health_verified"] == 1

    def test_deployment_rollback(self):
        """Log deployment, mark rolled back."""
        dep_id = log_deployment("omni", "sha1", "sha2")
        mark_deployment_rolled_back(dep_id, "went unhealthy within 5min")

        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM deployment_history WHERE id=?", (dep_id,)
        ).fetchone()
        conn.close()
        assert row["rolled_back"] == 1
        assert "unhealthy" in row["rollback_reason"]
