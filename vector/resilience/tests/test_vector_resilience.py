"""
test_vector_resilience.py — VECTOR Phase 2 Resilience Tests
Spec: vector-resilience-v1.md v1.1
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../.."))

import pytest
import shared.resilience.triad_db as triad_db
from shared.resilience.triad_db import init_triad_db

from vector.resilience.vector_resilience import (
    TOMBSTONE_PATH, VECTOR_STATE_DIR,
    wrap_diagnostic_main, check_tombstone_for_guardian_angel,
    verify_service_recovery, RestartVerification,
    intervention_lock, InterventionInProgress,
    check_log_staleness,
    check_watchdog_liveness,
    validate_and_process_directives,
    audit_action,
    CONSUMED_IDS_PATH,
)
import vector.resilience.vector_resilience as vr


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "vector-test.db"
    triad_db.DB_PATH = db_path
    init_triad_db()
    # Also redirect vector state to tmp
    vr.VECTOR_STATE_DIR = tmp_path / "vector_state"
    vr.LOCK_DIR = vr.VECTOR_STATE_DIR / "locks"
    vr.TOMBSTONE_PATH = vr.VECTOR_STATE_DIR / "diagnostic_crashed"
    vr.CONSUMED_IDS_PATH = vr.VECTOR_STATE_DIR / "consumed_ids.json"
    vr.VECTOR_STATE_DIR.mkdir(parents=True, exist_ok=True)
    vr.LOCK_DIR.mkdir(parents=True, exist_ok=True)
    yield


# ---------------------------------------------------------------------------
# V1: Diagnostic crash guard
# ---------------------------------------------------------------------------

class TestDiagnosticCrashGuard:

    def test_tombstone_written_on_crash(self):
        """Crash → tombstone written to persistent path."""
        def crashing_main():
            raise RuntimeError("disk full")

        with patch("vector.resilience.vector_resilience._emergency_telegram_alert"):
            with pytest.raises(RuntimeError):
                wrap_diagnostic_main(crashing_main)

        assert vr.TOMBSTONE_PATH.exists()
        data = json.loads(vr.TOMBSTONE_PATH.read_text())
        assert "disk full" in data["error"]

    def test_tombstone_cleared_on_clean_run(self):
        """Clean run → tombstone removed."""
        vr.TOMBSTONE_PATH.write_text('{"crashed_at": "old", "error": "old"}')

        wrap_diagnostic_main(lambda: None)

        assert not vr.TOMBSTONE_PATH.exists()

    def test_guardian_angel_detects_tombstone(self):
        """GA check returns tombstone data when present and fresh."""
        vr.TOMBSTONE_PATH.write_text(json.dumps({
            "crashed_at": "2026-05-02T23:00:00Z", "error": "test crash"
        }))
        result = check_tombstone_for_guardian_angel()
        assert result is not None
        assert "error" in result

    def test_guardian_angel_returns_none_when_no_tombstone(self):
        result = check_tombstone_for_guardian_angel()
        assert result is None


# ---------------------------------------------------------------------------
# V2: Restart verification contract
# ---------------------------------------------------------------------------

class TestRestartVerification:

    def test_healthy_service_returns_is_healthy_true(self):
        """HTTP 200 + status:healthy → is_healthy=True."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "healthy"}
        mock_resp.elapsed.total_seconds.return_value = 0.05
        with patch("vector.resilience.vector_resilience.requests.get", return_value=mock_resp):
            result = verify_service_recovery("omni", 8004)
        assert result.is_healthy is True
        assert result.body_status == "healthy"

    def test_degraded_service_returns_is_healthy_false(self):
        """HTTP 200 + status:degraded → is_healthy=False, not counted as recovered."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "degraded"}
        mock_resp.elapsed.total_seconds.return_value = 0.1
        with patch("vector.resilience.vector_resilience.requests.get", return_value=mock_resp):
            result = verify_service_recovery("omni", 8004)
        assert result.is_healthy is False
        assert "degraded" in result.failure_reason

    def test_missing_status_key_treated_as_degraded(self):
        """HTTP 200 but no status key → suspicious, is_healthy=False."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"version": "1.0"}  # no status key
        mock_resp.elapsed.total_seconds.return_value = 0.1
        with patch("vector.resilience.vector_resilience.requests.get", return_value=mock_resp):
            result = verify_service_recovery("omni", 8004)
        assert result.is_healthy is False
        assert result.body_status == "MISSING"


# ---------------------------------------------------------------------------
# V3: Log staleness + intervention lock
# ---------------------------------------------------------------------------

class TestLogStalenessAndLock:

    def test_fresh_log_returns_none(self, tmp_path):
        log_file = tmp_path / "service.log"
        log_file.write_text("2026-05-02 INFO starting up\n")
        result = check_log_staleness(str(log_file), "test-service", max_age_minutes=20)
        assert result is None

    def test_stale_log_returns_finding(self, tmp_path):
        log_file = tmp_path / "service.log"
        log_file.write_text("old content")
        # Make it appear old
        old_time = time.time() - 25 * 60  # 25 minutes ago
        os.utime(str(log_file), (old_time, old_time))
        result = check_log_staleness(str(log_file), "test-service", max_age_minutes=20)
        assert result is not None
        assert "LOG_STALE" in result

    def test_intervention_lock_prevents_double_restart(self):
        """Second call within timeout raises InterventionInProgress."""
        with intervention_lock("omni", timeout=60):
            with pytest.raises(InterventionInProgress):
                with intervention_lock("omni", timeout=60):
                    pass

    def test_intervention_lock_releases_on_exit(self):
        """Lock is released after context manager exits."""
        with intervention_lock("axiom", timeout=60):
            pass
        # Should not raise
        with intervention_lock("axiom", timeout=60):
            pass


# ---------------------------------------------------------------------------
# V4: Credential watchdog liveness
# ---------------------------------------------------------------------------

class TestWatchdogLiveness:

    def test_no_alert_on_first_ever_run(self):
        """No prior watchdog run → no alert (null guard)."""
        with patch("shared.resilience.alert_router.route_alert") as mock_alert:
            check_watchdog_liveness()
        mock_alert.assert_not_called()

    def test_alert_fires_when_overdue(self):
        """Watchdog last run >8 days ago → P1 alert."""
        from shared.resilience.triad_db import log_intervention, _get_conn
        import shared.resilience.triad_db as tdb

        # Write a watchdog heartbeat with old timestamp
        old_ts = "2026-04-20T00:00:00+00:00"
        conn = tdb._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO intervention_log
               (agent, action_type, target, trigger, outcome, timestamp)
               VALUES ('vector','watchdog_heartbeat','credential_watchdog','scheduled_run','recovered',?)""",
            (old_ts,)
        )
        conn.commit()
        conn.close()

        import shared.resilience.alert_router as ar
        with patch.object(ar, "_send_sovereign", return_value=True), \
             patch.object(ar, "_send_telegram", return_value=True):
            check_watchdog_liveness()

        conn = triad_db._get_conn()
        rows = conn.execute(
            "SELECT * FROM alert_dedup WHERE dedup_key LIKE '%watchdog%'"
        ).fetchall()
        conn.close()
        assert len(rows) >= 1


# ---------------------------------------------------------------------------
# V5/V6: Bus directive validation + dedup + poison guard
# ---------------------------------------------------------------------------

class TestBusDirectives:

    def test_unknown_directive_type_quarantined(self):
        """Unknown directive type → quarantined, not executed."""
        processed = []
        directives = [{"id": "msg-001", "type": "hack_the_planet", "target": "omni"}]
        counts = validate_and_process_directives(directives, lambda d: processed.append(d))
        assert counts["quarantined"] == 1
        assert counts["processed"] == 0

    def test_same_message_id_skipped_on_second_delivery(self):
        """Same msg_id delivered twice → second silently skipped."""
        processed = []
        directives = [{"id": "msg-dup", "type": "ping"}]
        validate_and_process_directives(directives, lambda d: processed.append(d))
        validate_and_process_directives(directives, lambda d: processed.append(d))
        assert len(processed) == 1  # only processed once

    def test_poison_message_quarantined_loop_continues(self):
        """Poison message raises → quarantined, subsequent messages still processed."""
        results = []
        def process(d):
            if d.get("type") == "escalate":
                raise ValueError("poison!")
            results.append(d)

        directives = [
            {"id": "msg-poison", "type": "escalate"},
            {"id": "msg-good",   "type": "ping"},
        ]
        counts = validate_and_process_directives(directives, process)
        assert counts["failed"] == 1
        assert counts["processed"] == 1
        assert len(results) == 1


# ---------------------------------------------------------------------------
# V7: @audit_action decorator
# ---------------------------------------------------------------------------

class TestAuditActionDecorator:

    def test_decorator_logs_to_resilience_db(self):
        """@audit_action writes structured record to RESILIENCE-DB on every call."""
        from shared.resilience.triad_db import _get_conn

        @audit_action(action_type="service_restart")
        def mock_restart(service: str, port: int):
            return True

        with patch("vector.resilience.vector_resilience._probe_health_for_audit",
                   return_value={"status": "healthy"}):
            mock_restart(service="omni", port=8004)

        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM intervention_log WHERE agent='vector' AND action_type='service_restart'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["outcome"] == "recovered"
        assert rows[0]["target"] == "omni"

    def test_decorator_logs_failure_on_exception(self):
        """@audit_action logs outcome=failed when decorated function raises."""
        from shared.resilience.triad_db import _get_conn

        @audit_action(action_type="service_restart")
        def mock_failing_restart(service: str, port: int):
            raise RuntimeError("launchctl failed")

        with patch("vector.resilience.vector_resilience._probe_health_for_audit",
                   return_value=None), \
             pytest.raises(RuntimeError):
            mock_failing_restart(service="axiom", port=8001)

        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM intervention_log WHERE agent='vector' AND target='axiom'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["outcome"] == "failed"
        assert "launchctl" in rows[0]["error"]
