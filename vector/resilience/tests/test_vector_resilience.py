"""
test_vector_resilience.py — VECTOR 30% Resilience Layer Tests
Spec: vector-resilience-v1.md v1.1
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))


# ─────────────────────────────────────────────────────────────────────────────
# V1 — crash_guard.py
# ─────────────────────────────────────────────────────────────────────────────

class TestCrashGuard:
    def test_tombstone_written_to_persistent_path_on_crash(self, tmp_path):
        """Spec TC1a: crash → tombstone written to persistent (non-/tmp) path."""
        tombstone = tmp_path / "diagnostic_crashed"

        with patch("vector.resilience.crash_guard.TOMBSTONE_PATH", tombstone), \
             patch("vector.resilience.crash_guard.VECTOR_STATE_DIR", tmp_path), \
             patch("vector.resilience.crash_guard.send_emergency_alert"), \
             patch("sys.exit"):
            from vector.resilience.crash_guard import run_with_crash_guard

            def crashing_fn():
                raise RuntimeError("test crash")

            run_with_crash_guard(crashing_fn)

        assert tombstone.exists()
        data = json.loads(tombstone.read_text())
        assert "crashed_at" in data
        assert "test crash" in data["error"]

    def test_is_tombstone_active_returns_true(self, tmp_path):
        """Spec TC1b: is_tombstone_active() returns True when tombstone exists and is fresh."""
        tombstone = tmp_path / "diagnostic_crashed"
        tombstone.write_text(json.dumps({"crashed_at": "2026-05-03T10:00:00Z", "error": "test"}))

        with patch("vector.resilience.crash_guard.TOMBSTONE_PATH", tombstone):
            from vector.resilience.crash_guard import is_tombstone_active
            assert is_tombstone_active() is True

    def test_tombstone_cleared_on_clean_run(self, tmp_path):
        """Spec TC1c: tombstone cleared on next clean run."""
        tombstone = tmp_path / "diagnostic_crashed"
        tombstone.write_text("existing tombstone")

        with patch("vector.resilience.crash_guard.TOMBSTONE_PATH", tombstone), \
             patch("vector.resilience.crash_guard.VECTOR_STATE_DIR", tmp_path):
            from vector.resilience.crash_guard import run_with_crash_guard, clear_tombstone

            run_with_crash_guard(lambda: None)

        assert not tombstone.exists()

    def test_is_tombstone_active_false_when_absent(self, tmp_path):
        """No tombstone file → is_tombstone_active() returns False."""
        tombstone = tmp_path / "does_not_exist"
        with patch("vector.resilience.crash_guard.TOMBSTONE_PATH", tombstone):
            from vector.resilience.crash_guard import is_tombstone_active
            assert is_tombstone_active() is False

    def test_emergency_alert_never_raises(self):
        """send_emergency_alert with bad token → no exception raised."""
        with patch("requests.post", side_effect=Exception("network down")), \
             patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "bad", "TELEGRAM_CHAT_ID": "123"}):
            from vector.resilience.crash_guard import send_emergency_alert
            # Must not raise
            send_emergency_alert("Test alert")


# ─────────────────────────────────────────────────────────────────────────────
# V2 — verify.py
# ─────────────────────────────────────────────────────────────────────────────

class TestVerifyServiceRecovery:
    def test_degraded_status_not_healthy(self):
        """Spec TC2: service returns status='degraded' → is_healthy=False."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "degraded"}

        with patch("requests.get", return_value=mock_resp):
            from vector.resilience.verify import verify_service_recovery
            result = verify_service_recovery("omni", 8004)

        assert result.is_healthy is False
        assert result.body_status == "degraded"
        assert result.failure_reason != ""

    def test_missing_status_key_treated_as_suspicious(self):
        """Spec TC3: HTTP 200 with no status key → body_status='MISSING', is_healthy=False."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"other_field": "value"}  # no 'status' key

        with patch("requests.get", return_value=mock_resp):
            from vector.resilience.verify import verify_service_recovery
            result = verify_service_recovery("oracle", 8007)

        assert result.body_status == "MISSING"
        assert result.is_healthy is False
        assert result.http_code == 200

    def test_healthy_service_passes(self):
        """Healthy service → is_healthy=True."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "healthy"}

        with patch("requests.get", return_value=mock_resp):
            from vector.resilience.verify import verify_service_recovery
            result = verify_service_recovery("axiom", 8001)

        assert result.is_healthy is True
        assert result.body_status == "healthy"
        assert result.failure_reason == ""

    def test_connection_refused_unreachable(self):
        """Connection refused → body_status='UNREACHABLE', is_healthy=False."""
        with patch("requests.get", side_effect=ConnectionError("refused")):
            from vector.resilience.verify import verify_service_recovery
            result = verify_service_recovery("axiom", 8001)

        assert result.is_healthy is False
        assert result.body_status == "UNREACHABLE"
        assert result.http_code == 0


# ─────────────────────────────────────────────────────────────────────────────
# V3 — staleness.py
# ─────────────────────────────────────────────────────────────────────────────

class TestLogStaleness:
    def test_stale_log_returns_warning(self, tmp_path):
        """Spec TC4: log last modified 25 min ago → returns warning string."""
        log_file = tmp_path / "service.log"
        log_file.write_text("old log entry")

        # Fake mtime to 25 minutes ago
        old_mtime = time.time() - (25 * 60)
        os.utime(str(log_file), (old_mtime, old_mtime))

        from vector.resilience.staleness import check_log_staleness
        result = check_log_staleness(str(log_file), threshold_minutes=20)

        assert result is not None
        assert "LOG_STALE" in result
        assert "25" in result or "24" in result  # approximately 25 min

    def test_fresh_log_returns_none(self, tmp_path):
        """Fresh log → returns None."""
        log_file = tmp_path / "service.log"
        log_file.write_text("fresh entry")
        # mtime is now (just written)

        from vector.resilience.staleness import check_log_staleness
        result = check_log_staleness(str(log_file), threshold_minutes=20)
        assert result is None

    def test_missing_file_returns_none(self, tmp_path):
        """Non-existent log file → returns None (not an error)."""
        from vector.resilience.staleness import check_log_staleness
        result = check_log_staleness(str(tmp_path / "nonexistent.log"))
        assert result is None

    def test_intervention_lock_prevents_concurrent(self, tmp_path):
        """Active lock raises InterventionInProgress within timeout."""
        from vector.resilience.staleness import intervention_lock, InterventionInProgress

        with patch("vector.resilience.staleness.LOCK_DIR", tmp_path / "locks"):
            # First lock should succeed
            with intervention_lock("axiom", timeout=60):
                # Second lock attempt (same service) should raise
                with pytest.raises(InterventionInProgress):
                    with intervention_lock("axiom", timeout=60):
                        pass


# ─────────────────────────────────────────────────────────────────────────────
# V4 — watchdog.py
# ─────────────────────────────────────────────────────────────────────────────

class TestCredentialWatchdog:
    def test_stale_watchdog_returns_alert(self):
        """Spec TC5a: watchdog last run >8 days → returns alert string."""
        from vector.resilience.watchdog import check_watchdog_liveness

        # Mock CHRONICLE to return a timestamp 10 days ago
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

        mock_chronicle = MagicMock()
        mock_chronicle.chronicle_read = MagicMock(return_value=old_ts)

        with patch.dict(sys.modules, {"chronicle_reader": mock_chronicle}):
            result = check_watchdog_liveness()

        assert result is not None
        assert "8" in result or "10" in result

    def test_none_last_run_returns_none(self):
        """Spec TC5b: last_run is None → returns None (first run expected, no alert)."""
        from vector.resilience.watchdog import check_watchdog_liveness

        mock_chronicle = MagicMock()
        mock_chronicle.chronicle_read = MagicMock(return_value=None)

        with patch.dict(sys.modules, {"chronicle_reader": mock_chronicle}):
            result = check_watchdog_liveness()

        assert result is None

    def test_chronicle_down_returns_none(self):
        """CHRONICLE unavailable → returns None (fail-open, heartbeat not blocked)."""
        from vector.resilience.watchdog import check_watchdog_liveness

        with patch.dict(sys.modules, {"chronicle_reader": None}):
            result = check_watchdog_liveness()

        assert result is None

    def test_days_since_null_returns_none(self):
        """days_since(None) → None (null guard — no throws on first run)."""
        from vector.resilience.watchdog import days_since
        assert days_since(None) is None

    def test_days_since_calculates_correctly(self):
        """days_since(recent ISO) returns float near 0."""
        from datetime import datetime, timezone
        from vector.resilience.watchdog import days_since
        now_iso = datetime.now(timezone.utc).isoformat()
        result = days_since(now_iso)
        assert result is not None
        assert result < 0.01  # less than 1 second


# ─────────────────────────────────────────────────────────────────────────────
# V5+V6 — directives.py
# ─────────────────────────────────────────────────────────────────────────────

class TestDirectives:
    def test_unknown_type_quarantined_not_reprocessed(self, tmp_path):
        """Spec TC6: unknown type → quarantined + consumed, not re-processed on second call."""
        from vector.resilience.directives import validate_and_process_directives

        process_fn = MagicMock()
        bad_directive = {"id": "msg-001", "type": "destroy_everything", "target": "axiom"}

        with patch("vector.resilience.directives.CONSUMED_IDS_PATH", tmp_path / "consumed.json"), \
             patch("vector.resilience.directives.chronicle_quarantine"):
            # First call: quarantined
            result1 = validate_and_process_directives([bad_directive], process_fn)
            # Second call: skipped (already consumed)
            result2 = validate_and_process_directives([bad_directive], process_fn)

        process_fn.assert_not_called()
        assert result1["quarantined"] == 1
        assert result2["skipped"] == 1

    def test_same_message_id_twice_second_skipped(self, tmp_path):
        """Spec TC7: same message ID twice → second delivery skipped."""
        from vector.resilience.directives import validate_and_process_directives

        process_fn = MagicMock()
        directive = {"id": "msg-duplicate", "type": "ping", "target": ""}

        with patch("vector.resilience.directives.CONSUMED_IDS_PATH", tmp_path / "consumed.json"):
            result1 = validate_and_process_directives([directive], process_fn)
            result2 = validate_and_process_directives([directive], process_fn)

        assert process_fn.call_count == 1  # processed only once
        assert result1["processed"] == 1
        assert result2["skipped"] == 1

    def test_valid_restart_service_processed(self, tmp_path):
        """Valid restart_service directive → process_fn called."""
        from vector.resilience.directives import validate_and_process_directives

        process_fn = MagicMock()
        directive = {"id": "msg-003", "type": "restart_service", "target": "oracle"}

        with patch("vector.resilience.directives.CONSUMED_IDS_PATH", tmp_path / "consumed.json"), \
             patch.dict(sys.modules, {"chronicle_reader": MagicMock()}):
            result = validate_and_process_directives([directive], process_fn)

        process_fn.assert_called_once_with(directive)
        assert result["processed"] == 1

    def test_unknown_restart_target_quarantined(self, tmp_path):
        """restart_service with unknown target → quarantined."""
        from vector.resilience.directives import validate_and_process_directives

        process_fn = MagicMock()
        directive = {"id": "msg-004", "type": "restart_service", "target": "unknown-service-xyz"}

        with patch("vector.resilience.directives.CONSUMED_IDS_PATH", tmp_path / "consumed.json"), \
             patch("vector.resilience.directives.chronicle_quarantine"):
            result = validate_and_process_directives([directive], process_fn)

        process_fn.assert_not_called()
        assert result["quarantined"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# V7 — audit.py
# ─────────────────────────────────────────────────────────────────────────────

class TestAuditAction:
    def test_success_audit_record_written(self):
        """Spec TC8a: decorated function succeeds → audit record written to CHRONICLE."""
        from vector.resilience.audit import audit_action

        written_records = []

        def mock_write(key, record):
            written_records.append(record)

        mock_chronicle = MagicMock()
        mock_chronicle.chronicle_write = mock_write

        @audit_action(action_type="service_restart")
        def restart_service(service: str) -> str:
            return "ok"

        with patch.dict(sys.modules, {"chronicle_reader": mock_chronicle}), \
             patch("vector.resilience.audit._probe_health", return_value="healthy"), \
             patch("vector.resilience.audit._write_action_audit", side_effect=lambda r: written_records.append(r)):
            result = restart_service("oracle")

        assert result == "ok"
        assert len(written_records) == 1
        record = written_records[0]
        assert record["action_type"] == "service_restart"
        assert record["outcome"] == "recovered"
        assert record["error"] is None

    def test_exception_audit_record_written_and_reraised(self):
        """Spec TC8b: decorated function raises → record written AND exception re-raised."""
        from vector.resilience.audit import audit_action

        written_records = []

        @audit_action(action_type="service_restart")
        def failing_restart(service: str) -> None:
            raise RuntimeError("restart failed")

        with patch("vector.resilience.audit._probe_health", return_value="unreachable"), \
             patch("vector.resilience.audit._write_action_audit", side_effect=lambda r: written_records.append(r)):
            with pytest.raises(RuntimeError, match="restart failed"):
                failing_restart("axiom")

        assert len(written_records) == 1
        assert written_records[0]["outcome"] == "failed"
        assert "restart failed" in written_records[0]["error"]
