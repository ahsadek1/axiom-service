"""
Tests for genesis/alerts/alert_ack_tracker.py
==============================================

Tests cover:
- Alert tracking (insert/read)
- ACK marking and detection
- Pending alert retrieval (resend + escalate)
- Resend counter increments
- Escalation marking
- Summary statistics
- DB initialization and thread safety
- Cleanup of old records

Author: GENESIS 🌱
Date: 2026-05-16
"""

import os
import pytest
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from genesis.alerts.alert_ack_tracker import (
    track_alert,
    mark_acked,
    is_acked,
    get_pending_alerts,
    increment_resend_count,
    mark_escalated,
    get_alert_summary,
    cleanup_old_records,
    _ensure_db,
    _connect,
    _now_utc,
)


@pytest.fixture
def temp_db():
    """Create a temporary DB for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_ack.db")
        with patch("genesis.alerts.alert_ack_tracker.ACK_DB_PATH", db_path):
            _ensure_db()
            yield db_path


class TestAlertTracking:
    """Test alert tracking (insert/read)."""

    def test_track_alert_success(self, temp_db):
        """UT-1: track_alert successfully records an alert."""
        result = track_alert(
            message_id="genesis-100-1",
            title="Test alert",
            agent="test-agent",
        )
        assert result is True

    def test_track_alert_is_retrievable(self, temp_db):
        """UT-2: tracked alert can be queried."""
        track_alert(
            message_id="genesis-100-1",
            title="Test alert",
            agent="test-agent",
        )

        # Should be in pending list (not ACKed)
        pending = get_pending_alerts(older_than_min=0)
        msg_ids = [a["message_id"] for a in pending]
        assert "genesis-100-1" in msg_ids

    def test_track_alert_duplicate_is_ignored(self, temp_db):
        """UT-3: Inserting duplicate message_id is ignored (INSERT OR IGNORE)."""
        track_alert(
            message_id="genesis-100-1",
            title="First title",
            agent="agent1",
        )
        track_alert(
            message_id="genesis-100-1",
            title="Second title",
            agent="agent2",
        )

        # Should only have one entry
        summary = get_alert_summary()
        assert summary["total_tracked"] == 1

    def test_track_alert_default_agent(self, temp_db):
        """UT-4: track_alert defaults agent to 'genesis'."""
        track_alert(
            message_id="genesis-100-1",
            title="Test alert",
        )

        pending = get_pending_alerts(older_than_min=0)
        assert pending[0]["agent"] == "genesis"

    def test_track_alert_stores_title(self, temp_db):
        """UT-5: tracked alert preserves full title."""
        title = "Critical: Execution auth failed at 2026-05-16 09:30:45"
        track_alert(
            message_id="genesis-100-1",
            title=title,
            agent="test",
        )

        pending = get_pending_alerts(older_than_min=0)
        assert pending[0]["title"] == title

    def test_track_alert_stores_timestamp(self, temp_db):
        """UT-6: tracked alert has sent_at timestamp."""
        before = datetime.now(timezone.utc)
        track_alert(
            message_id="genesis-100-1",
            title="Test",
            agent="test",
        )
        after = datetime.now(timezone.utc)

        pending = get_pending_alerts(older_than_min=0)
        sent_at = datetime.fromisoformat(pending[0]["sent_at"])
        assert before <= sent_at <= after


class TestACKMarking:
    """Test ACK marking and detection."""

    def test_mark_acked_success(self, temp_db):
        """UT-7: mark_acked successfully marks an alert as acknowledged."""
        track_alert("genesis-100-1", "Test", "test")
        result = mark_acked("genesis-100-1")
        assert result is True

    def test_is_acked_after_marking(self, temp_db):
        """UT-8: is_acked returns True after mark_acked."""
        track_alert("genesis-100-1", "Test", "test")
        mark_acked("genesis-100-1")
        assert is_acked("genesis-100-1") is True

    def test_is_acked_before_marking(self, temp_db):
        """UT-9: is_acked returns False before mark_acked."""
        track_alert("genesis-100-1", "Test", "test")
        assert is_acked("genesis-100-1") is False

    def test_is_acked_nonexistent_message(self, temp_db):
        """UT-10: is_acked returns False for nonexistent message."""
        assert is_acked("genesis-999-999") is False

    def test_mark_acked_nonexistent_message(self, temp_db):
        """UT-11: mark_acked returns False for nonexistent message."""
        result = mark_acked("genesis-999-999")
        assert result is False

    def test_mark_acked_idempotent(self, temp_db):
        """UT-12: mark_acked is idempotent (can't double-ACK)."""
        track_alert("genesis-100-1", "Test", "test")
        mark_acked("genesis-100-1")
        # Second call should return False (already ACKed)
        result = mark_acked("genesis-100-1")
        assert result is False

    def test_mark_acked_sets_timestamp(self, temp_db):
        """UT-13: mark_acked records the ACK time."""
        track_alert("genesis-100-1", "Test", "test")
        before = datetime.now(timezone.utc)
        mark_acked("genesis-100-1")
        after = datetime.now(timezone.utc)

        conn = _connect()
        row = conn.execute(
            "SELECT ack_received_at FROM alert_ack_state WHERE message_id = ?",
            ("genesis-100-1",),
        ).fetchone()
        conn.close()

        ack_time = datetime.fromisoformat(row["ack_received_at"])
        assert before <= ack_time <= after


class TestPendingAlertRetrieval:
    """Test get_pending_alerts filtering."""

    def test_get_pending_alerts_empty(self, temp_db):
        """UT-14: get_pending_alerts returns empty list when no pending."""
        track_alert("genesis-100-1", "Test", "test")
        mark_acked("genesis-100-1")

        pending = get_pending_alerts(older_than_min=0)
        assert pending == []

    def test_get_pending_alerts_includes_unacked(self, temp_db):
        """UT-15: get_pending_alerts includes unacknowledged alerts."""
        track_alert("genesis-100-1", "Test", "test")
        pending = get_pending_alerts(older_than_min=0)
        assert len(pending) == 1
        assert pending[0]["message_id"] == "genesis-100-1"

    def test_get_pending_alerts_excludes_acked(self, temp_db):
        """UT-16: get_pending_alerts excludes acknowledged alerts."""
        track_alert("genesis-100-1", "Unacked", "test")
        track_alert("genesis-100-2", "Acked", "test")
        mark_acked("genesis-100-2")

        pending = get_pending_alerts(older_than_min=0)
        msg_ids = [a["message_id"] for a in pending]
        assert "genesis-100-1" in msg_ids
        assert "genesis-100-2" not in msg_ids

    def test_get_pending_alerts_time_filter_older_than_5min(self, temp_db):
        """UT-17: get_pending_alerts filters by age (>5 min)."""
        # Create alert right now
        track_alert("genesis-new-1", "New alert", "test")

        # Manually insert an old alert
        conn = _connect()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-old-1", "Old alert", "test", old_time),
        )
        conn.commit()
        conn.close()

        # With older_than_min=5, should only return the 6-min-old alert
        pending = get_pending_alerts(older_than_min=5)
        msg_ids = [a["message_id"] for a in pending]
        assert "genesis-old-1" in msg_ids
        assert "genesis-new-1" not in msg_ids

    def test_get_pending_alerts_critical_flag(self, temp_db):
        """UT-18: critical=True only returns >10 min old alerts."""
        # Create old alert (11 min)
        conn = _connect()
        very_old_time = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-very-old-1", "Very old", "test", very_old_time),
        )

        # Create semi-old alert (6 min)
        semi_old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-semi-old-1", "Semi old", "test", semi_old_time),
        )
        conn.commit()
        conn.close()

        # critical=True should only return the 11-min alert
        pending = get_pending_alerts(critical=True)
        msg_ids = [a["message_id"] for a in pending]
        assert "genesis-very-old-1" in msg_ids
        assert "genesis-semi-old-1" not in msg_ids

    def test_get_pending_alerts_excludes_escalated(self, temp_db):
        """UT-19: get_pending_alerts excludes escalated alerts."""
        conn = _connect()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-escalated-1", "Escalated", "test", old_time),
        )
        conn.commit()

        # Mark as escalated
        mark_escalated("genesis-escalated-1")
        conn.close()

        # Should not appear in pending (critical=True)
        pending = get_pending_alerts(critical=True)
        msg_ids = [a["message_id"] for a in pending]
        assert "genesis-escalated-1" not in msg_ids

    def test_get_pending_alerts_ordered_by_time(self, temp_db):
        """UT-20: get_pending_alerts returns alerts ordered by sent_at (oldest first)."""
        conn = _connect()
        time1 = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        time2 = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-2", "Second", "test", time2),
        )
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-1", "First", "test", time1),
        )
        conn.commit()
        conn.close()

        pending = get_pending_alerts(older_than_min=0)
        # First alert is oldest and should be first
        assert pending[0]["message_id"] == "genesis-1"
        assert pending[1]["message_id"] == "genesis-2"


class TestResendCounter:
    """Test resend counter increments."""

    def test_increment_resend_count_success(self, temp_db):
        """UT-21: increment_resend_count increases counter."""
        track_alert("genesis-100-1", "Test", "test")
        result = increment_resend_count("genesis-100-1")
        assert result is True

    def test_increment_resend_count_increments(self, temp_db):
        """UT-22: increment_resend_count actually increments the value."""
        track_alert("genesis-100-1", "Test", "test")

        for i in range(1, 4):
            increment_resend_count("genesis-100-1")
            conn = _connect()
            row = conn.execute(
                "SELECT resend_count FROM alert_ack_state WHERE message_id = ?",
                ("genesis-100-1",),
            ).fetchone()
            conn.close()
            assert row["resend_count"] == i

    def test_increment_resend_count_sets_timestamp(self, temp_db):
        """UT-23: increment_resend_count updates last_resend_at."""
        track_alert("genesis-100-1", "Test", "test")
        before = datetime.now(timezone.utc)
        increment_resend_count("genesis-100-1")
        after = datetime.now(timezone.utc)

        conn = _connect()
        row = conn.execute(
            "SELECT last_resend_at FROM alert_ack_state WHERE message_id = ?",
            ("genesis-100-1",),
        ).fetchone()
        conn.close()

        resend_time = datetime.fromisoformat(row["last_resend_at"])
        assert before <= resend_time <= after

    def test_increment_resend_count_nonexistent(self, temp_db):
        """UT-24: increment_resend_count on nonexistent message still succeeds (no-op)."""
        # Note: Current implementation doesn't check rowcount, just executes the UPDATE
        result = increment_resend_count("genesis-999-999")
        # Returns True even if no rows were updated
        assert result is True


class TestEscalationMarking:
    """Test escalation marking."""

    def test_mark_escalated_success(self, temp_db):
        """UT-25: mark_escalated successfully marks alert."""
        track_alert("genesis-100-1", "Test", "test")
        result = mark_escalated("genesis-100-1")
        assert result is True

    def test_mark_escalated_sets_flag(self, temp_db):
        """UT-26: mark_escalated sets escalated=1."""
        track_alert("genesis-100-1", "Test", "test")
        mark_escalated("genesis-100-1")

        conn = _connect()
        row = conn.execute(
            "SELECT escalated FROM alert_ack_state WHERE message_id = ?",
            ("genesis-100-1",),
        ).fetchone()
        conn.close()

        assert row["escalated"] == 1

    def test_mark_escalated_idempotent(self, temp_db):
        """UT-27: mark_escalated is idempotent."""
        track_alert("genesis-100-1", "Test", "test")
        mark_escalated("genesis-100-1")
        result = mark_escalated("genesis-100-1")
        # Should succeed even on second call
        assert result is True

    def test_mark_escalated_nonexistent(self, temp_db):
        """UT-28: mark_escalated on nonexistent message still returns True."""
        # NOTE: Current implementation doesn't check rowcount, returns True always
        result = mark_escalated("genesis-999-999")
        assert result is True  # Will insert nothing but won't error


class TestAlertSummary:
    """Test summary statistics."""

    def test_get_alert_summary_empty(self, temp_db):
        """UT-29: get_alert_summary on empty DB returns zeros."""
        summary = get_alert_summary()
        assert summary["total_tracked"] == 0
        assert summary["acked"] == 0
        assert summary["pending"] == 0
        assert summary["pending_resend"] == 0
        assert summary["pending_escalate"] == 0

    def test_get_alert_summary_total_tracked(self, temp_db):
        """UT-30: summary correctly counts total_tracked."""
        track_alert("genesis-1", "Alert 1", "test")
        track_alert("genesis-2", "Alert 2", "test")
        summary = get_alert_summary()
        assert summary["total_tracked"] == 2

    def test_get_alert_summary_acked_count(self, temp_db):
        """UT-31: summary correctly counts acked alerts."""
        track_alert("genesis-1", "Alert 1", "test")
        track_alert("genesis-2", "Alert 2", "test")
        mark_acked("genesis-1")
        summary = get_alert_summary()
        assert summary["acked"] == 1
        assert summary["pending"] == 1

    def test_get_alert_summary_pending_resend(self, temp_db):
        """UT-32: summary counts pending_resend (>5 min old, not ACKed)."""
        conn = _connect()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-old-1", "Old", "test", old_time),
        )
        conn.commit()
        conn.close()

        summary = get_alert_summary()
        assert summary["pending_resend"] == 1

    def test_get_alert_summary_pending_escalate(self, temp_db):
        """UT-33: summary counts pending_escalate (>10 min old, not ACKed, not escalated)."""
        conn = _connect()
        very_old_time = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-very-old-1", "Very old", "test", very_old_time),
        )
        conn.commit()
        conn.close()

        summary = get_alert_summary()
        assert summary["pending_escalate"] == 1


class TestCleanup:
    """Test cleanup of old records."""

    def test_cleanup_old_records_removes_old(self, temp_db):
        """UT-34: cleanup_old_records removes records >N hours old."""
        conn = _connect()
        very_old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-old-1", "Old", "test", very_old_time),
        )
        conn.commit()
        conn.close()

        deleted = cleanup_old_records(older_than_hours=24)
        assert deleted == 1

    def test_cleanup_old_records_keeps_recent(self, temp_db):
        """UT-35: cleanup_old_records keeps recent records."""
        track_alert("genesis-recent-1", "Recent", "test")

        deleted = cleanup_old_records(older_than_hours=24)
        assert deleted == 0

        summary = get_alert_summary()
        assert summary["total_tracked"] == 1

    def test_cleanup_old_records_returns_count(self, temp_db):
        """UT-36: cleanup_old_records returns number deleted."""
        conn = _connect()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        for i in range(5):
            conn.execute(
                "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
                (f"genesis-old-{i}", "Old", "test", old_time),
            )
        conn.commit()
        conn.close()

        deleted = cleanup_old_records(older_than_hours=24)
        assert deleted == 5


class TestDBInitialization:
    """Test DB initialization."""

    def test_ensure_db_creates_table(self, temp_db):
        """UT-37: _ensure_db creates the required table."""
        conn = _connect()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alert_ack_state'"
        )
        result = cursor.fetchone()
        conn.close()
        assert result is not None

    def test_ensure_db_creates_index(self, temp_db):
        """UT-38: _ensure_db creates the sent_at index."""
        conn = _connect()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_alert_ack_sent_at'"
        )
        result = cursor.fetchone()
        conn.close()
        assert result is not None

    def test_ensure_db_idempotent(self, temp_db):
        """UT-39: _ensure_db can be called multiple times safely."""
        _ensure_db()
        _ensure_db()
        _ensure_db()
        # Should not raise

        summary = get_alert_summary()
        assert summary is not None


class TestThreadSafety:
    """Test thread safety of DB operations."""

    def test_concurrent_track_alerts(self, temp_db):
        """UT-40: Concurrent track_alert calls are safe."""
        results = []

        def track_many():
            for i in range(10):
                result = track_alert(f"genesis-{threading.current_thread().ident}-{i}", f"Alert {i}", "test")
                results.append(result)

        threads = [threading.Thread(target=track_many) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(results)
        summary = get_alert_summary()
        assert summary["total_tracked"] == 30

    def test_concurrent_mark_acked(self, temp_db):
        """UT-41: Concurrent mark_acked calls are safe."""
        for i in range(10):
            track_alert(f"genesis-{i}", f"Alert {i}", "test")

        results = []

        def ack_many():
            for i in range(10):
                result = mark_acked(f"genesis-{i}")
                results.append(result)

        threads = [threading.Thread(target=ack_many) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        summary = get_alert_summary()
        assert summary["acked"] == 10

    def test_concurrent_increment_resend(self, temp_db):
        """UT-42: Concurrent increment_resend_count calls are safe."""
        track_alert("genesis-shared-1", "Shared", "test")

        def increment_many():
            for _ in range(5):
                increment_resend_count("genesis-shared-1")

        threads = [threading.Thread(target=increment_many) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        conn = _connect()
        row = conn.execute(
            "SELECT resend_count FROM alert_ack_state WHERE message_id = ?",
            ("genesis-shared-1",),
        ).fetchone()
        conn.close()

        assert row["resend_count"] == 15
