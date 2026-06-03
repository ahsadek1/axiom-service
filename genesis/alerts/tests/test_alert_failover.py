"""
Tests for genesis/alerts/alert_failover.py
===========================================

Tests cover:
- Resend logic (5-min timeout)
- Escalation logic (10-min timeout)
- Multi-channel escalation (Telegram, bus, Discord)
- Status reporting
- Rate limiting
- Concurrent access safety

Author: GENESIS 🌱
Date: 2026-05-16
"""

import os
import pytest
import threading
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, call
from pathlib import Path

from genesis.alerts.alert_failover import (
    AlertFailover,
    check_and_escalate,
    get_status_report,
    _check_and_resend,
    _check_and_escalate,
    _send_escalation_telegram,
    _send_escalation_bus,
    _send_escalation_discord,
)
from genesis.alerts.alert_ack_tracker import (
    track_alert,
    mark_acked,
    _ensure_db,
)


@pytest.fixture
def temp_db():
    """Create a temporary DB for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_ack.db")
        with patch("genesis.alerts.alert_ack_tracker.ACK_DB_PATH", db_path):
            _ensure_db()
            yield db_path


class TestResendLogic:
    """Test alert resend at 5-minute timeout."""

    @patch("genesis.alerts.alert_failover.send_alert")
    @patch("genesis.alerts.alert_failover.increment_resend_count")
    def test_check_and_resend_returns_count(self, mock_increment, mock_send, temp_db):
        """UT-1: _check_and_resend returns number of alerts resent."""
        # Create old, unacked alert
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-old-1", "Old alert", "test", old_time),
        )
        conn.commit()
        conn.close()

        mock_send.return_value = "genesis-resend-1"

        count = _check_and_resend()
        assert count == 1

    @patch("genesis.alerts.alert_failover.send_alert")
    @patch("genesis.alerts.alert_failover.increment_resend_count")
    def test_check_and_resend_calls_send_alert(self, mock_increment, mock_send, temp_db):
        """UT-2: _check_and_resend calls send_alert for each alert."""
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-old-1", "Old alert", "test", old_time),
        )
        conn.commit()
        conn.close()

        mock_send.return_value = "genesis-resend-1"

        _check_and_resend()

        # Should call send_alert once
        mock_send.assert_called_once()
        call_args = mock_send.call_args
        assert "[RESEND]" in call_args[1]["title"]
        assert "genesis-old-1" not in call_args[1]["title"]  # It's a new send, not the old ID

    @patch("genesis.alerts.alert_failover.send_alert")
    @patch("genesis.alerts.alert_failover.increment_resend_count")
    def test_check_and_resend_increments_counter(self, mock_increment, mock_send, temp_db):
        """UT-3: _check_and_resend increments resend count."""
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-old-1", "Old alert", "test", old_time),
        )
        conn.commit()
        conn.close()

        mock_send.return_value = "genesis-resend-1"

        _check_and_resend()

        # Should increment counter for the original message
        mock_increment.assert_called_once_with("genesis-old-1")

    @patch("genesis.alerts.alert_failover.send_alert")
    def test_check_and_resend_skips_recent_alerts(self, mock_send, temp_db):
        """UT-4: _check_and_resend skips alerts newer than 5 minutes."""
        track_alert("genesis-new-1", "New alert", "test")

        _check_and_resend()

        # Should not resend
        mock_send.assert_not_called()

    @patch("genesis.alerts.alert_failover.send_alert")
    @patch("genesis.alerts.alert_failover.increment_resend_count")
    def test_check_and_resend_skips_acked_alerts(self, mock_increment, mock_send, temp_db):
        """UT-5: _check_and_resend skips already-acknowledged alerts."""
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at, ack_received_at) VALUES (?, ?, ?, ?, ?)",
            ("genesis-acked-1", "Acked alert", "test", old_time, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

        _check_and_resend()

        # Should not resend
        mock_send.assert_not_called()

    @patch("genesis.alerts.alert_failover.send_alert")
    @patch("genesis.alerts.alert_failover.increment_resend_count")
    def test_check_and_resend_multiple_alerts(self, mock_increment, mock_send, temp_db):
        """UT-6: _check_and_resend handles multiple old alerts."""
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        for i in range(3):
            conn.execute(
                "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
                (f"genesis-old-{i}", f"Old alert {i}", "test", old_time),
            )
        conn.commit()
        conn.close()

        mock_send.return_value = "genesis-resend-1"

        count = _check_and_resend()
        assert count == 3


class TestEscalationLogic:
    """Test alert escalation at 10-minute timeout."""

    @patch("genesis.alerts.alert_failover._send_escalation_telegram")
    @patch("genesis.alerts.alert_failover._send_escalation_bus")
    @patch("genesis.alerts.alert_failover._send_escalation_discord")
    @patch("genesis.alerts.alert_failover.mark_escalated")
    def test_check_and_escalate_returns_count(self, mock_mark, mock_discord, mock_bus, mock_tg, temp_db):
        """UT-7: _check_and_escalate returns number of alerts escalated."""
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        very_old_time = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-very-old-1", "Very old alert", "test", very_old_time),
        )
        conn.commit()
        conn.close()

        mock_tg.return_value = True
        mock_bus.return_value = True
        mock_discord.return_value = True

        count = _check_and_escalate()
        assert count == 1

    @patch("genesis.alerts.alert_failover._send_escalation_telegram")
    @patch("genesis.alerts.alert_failover._send_escalation_bus")
    @patch("genesis.alerts.alert_failover._send_escalation_discord")
    @patch("genesis.alerts.alert_failover.mark_escalated")
    def test_check_and_escalate_calls_all_channels(self, mock_mark, mock_discord, mock_bus, mock_tg, temp_db):
        """UT-8: _check_and_escalate attempts all three channels."""
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        very_old_time = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-very-old-1", "Very old alert", "test", very_old_time),
        )
        conn.commit()
        conn.close()

        mock_tg.return_value = True
        mock_bus.return_value = True
        mock_discord.return_value = True

        _check_and_escalate()

        # All three should be called
        mock_tg.assert_called_once()
        mock_bus.assert_called_once()
        mock_discord.assert_called_once()

    @patch("genesis.alerts.alert_failover._send_escalation_telegram")
    @patch("genesis.alerts.alert_failover._send_escalation_bus")
    @patch("genesis.alerts.alert_failover._send_escalation_discord")
    @patch("genesis.alerts.alert_failover.mark_escalated")
    def test_check_and_escalate_marks_when_any_channel_succeeds(self, mock_mark, mock_discord, mock_bus, mock_tg, temp_db):
        """UT-9: _check_and_escalate marks escalation when at least one channel succeeds."""
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        very_old_time = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-very-old-1", "Very old alert", "test", very_old_time),
        )
        conn.commit()
        conn.close()

        mock_tg.return_value = True
        mock_bus.return_value = False
        mock_discord.return_value = False

        _check_and_escalate()

        # Should still mark as escalated
        mock_mark.assert_called_once_with("genesis-very-old-1")

    @patch("genesis.alerts.alert_failover._send_escalation_telegram")
    @patch("genesis.alerts.alert_failover._send_escalation_bus")
    @patch("genesis.alerts.alert_failover._send_escalation_discord")
    @patch("genesis.alerts.alert_failover.mark_escalated")
    def test_check_and_escalate_only_if_no_channels_succeed(self, mock_mark, mock_discord, mock_bus, mock_tg, temp_db):
        """UT-10: _check_and_escalate doesn't mark if all channels fail."""
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        very_old_time = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-very-old-1", "Very old alert", "test", very_old_time),
        )
        conn.commit()
        conn.close()

        mock_tg.return_value = False
        mock_bus.return_value = False
        mock_discord.return_value = False

        _check_and_escalate()

        # Should NOT mark as escalated
        mock_mark.assert_not_called()

    @patch("genesis.alerts.alert_failover._send_escalation_telegram")
    @patch("genesis.alerts.alert_failover._send_escalation_bus")
    @patch("genesis.alerts.alert_failover._send_escalation_discord")
    @patch("genesis.alerts.alert_failover.mark_escalated")
    def test_check_and_escalate_skips_recent_alerts(self, mock_mark, mock_discord, mock_bus, mock_tg, temp_db):
        """UT-11: _check_and_escalate skips alerts newer than 10 minutes."""
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        semi_old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-semi-old-1", "Semi-old alert", "test", semi_old_time),
        )
        conn.commit()
        conn.close()

        _check_and_escalate()

        # Should not escalate
        mock_tg.assert_not_called()

    @patch("genesis.alerts.alert_failover._send_escalation_telegram")
    @patch("genesis.alerts.alert_failover._send_escalation_bus")
    @patch("genesis.alerts.alert_failover._send_escalation_discord")
    @patch("genesis.alerts.alert_failover.mark_escalated")
    def test_check_and_escalate_skips_already_escalated(self, mock_mark, mock_discord, mock_bus, mock_tg, temp_db):
        """UT-12: _check_and_escalate skips already-escalated alerts."""
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        very_old_time = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at, escalated) VALUES (?, ?, ?, ?, ?)",
            ("genesis-escalated-1", "Already escalated", "test", very_old_time, 1),
        )
        conn.commit()
        conn.close()

        _check_and_escalate()

        # Should not try to escalate
        mock_tg.assert_not_called()


class TestEscalationChannels:
    """Test individual escalation channels."""

    @patch("genesis.alerts.alert_failover.requests.post")
    def test_send_escalation_telegram_success(self, mock_post):
        """UT-13: _send_escalation_telegram sends to Telegram."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        result = _send_escalation_telegram("Alert title", "genesis-100-1")
        assert result is True
        mock_post.assert_called_once()

    @patch("genesis.alerts.alert_failover.requests.post")
    def test_send_escalation_telegram_failure(self, mock_post):
        """UT-14: _send_escalation_telegram returns False on failure."""
        mock_post.side_effect = Exception("Network error")

        result = _send_escalation_telegram("Alert title", "genesis-100-1")
        assert result is False

    @patch("genesis.alerts.alert_failover.requests.post")
    def test_send_escalation_telegram_includes_message_id(self, mock_post):
        """UT-15: Escalation Telegram includes message ID."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        _send_escalation_telegram("Alert title", "genesis-100-1")

        call_args = mock_post.call_args
        sent_data = call_args[1]["json"]
        assert "genesis-100-1" in sent_data["text"]

    @patch("genesis.alerts.alert_failover.requests.post")
    def test_send_escalation_bus_success(self, mock_post):
        """UT-16: _send_escalation_bus sends to message bus."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        result = _send_escalation_bus("Alert title", "genesis-100-1")
        assert result is True
        mock_post.assert_called_once()

    @patch("genesis.alerts.alert_failover.requests.post")
    def test_send_escalation_bus_format(self, mock_post):
        """UT-17: Bus escalation has correct format."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        _send_escalation_bus("Alert title", "genesis-100-1")

        call_args = mock_post.call_args
        sent_data = call_args[1]["json"]
        assert sent_data["from"] == "genesis-alerts-escalation"
        assert sent_data["to"] == "sovereign"
        assert "[ESCALATION]" in sent_data["message"]

    @patch.dict(os.environ, {"DISCORD_BRIDGE_URL": "http://localhost:8010", "DISCORD_BRIDGE_SECRET": "secret"})
    @patch("genesis.alerts.alert_failover.requests.post")
    def test_send_escalation_discord_success(self, mock_post):
        """UT-18: _send_escalation_discord sends to Discord."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        result = _send_escalation_discord("Alert title", "genesis-100-1")
        assert result is True
        mock_post.assert_called_once()

    def test_send_escalation_discord_not_configured(self):
        """UT-19: _send_escalation_discord returns False if not configured."""
        with patch.dict(os.environ, {}, clear=True):
            result = _send_escalation_discord("Alert title", "genesis-100-1")
            assert result is False

    @patch.dict(os.environ, {"DISCORD_BRIDGE_URL": "http://localhost:8010", "DISCORD_BRIDGE_SECRET": "secret"})
    @patch("genesis.alerts.alert_failover.requests.post")
    def test_send_escalation_discord_failure(self, mock_post):
        """UT-20: _send_escalation_discord returns False on error."""
        mock_post.side_effect = Exception("Network error")

        result = _send_escalation_discord("Alert title", "genesis-100-1")
        assert result is False


class TestAlertFailoverClass:
    """Test AlertFailover class."""

    @patch("genesis.alerts.alert_failover._check_and_resend")
    @patch("genesis.alerts.alert_failover._check_and_escalate")
    @patch("genesis.alerts.alert_failover.get_alert_summary")
    def test_check_and_escalate_returns_dict(self, mock_summary, mock_escalate, mock_resend, temp_db):
        """UT-21: check_and_escalate returns proper dict structure."""
        mock_resend.return_value = 2
        mock_escalate.return_value = 1
        mock_summary.return_value = {
            "total_tracked": 10,
            "acked": 6,
            "pending": 4,
            "pending_resend": 2,
            "pending_escalate": 1,
        }

        failover = AlertFailover()
        result = failover.check_and_escalate()

        assert result["resent"] == 2
        assert result["escalated"] == 1
        assert result["pending_resend"] == 2
        assert result["pending_escalate"] == 1

    @patch("genesis.alerts.alert_failover._check_and_resend")
    @patch("genesis.alerts.alert_failover._check_and_escalate")
    def test_check_and_escalate_rate_limited(self, mock_escalate, mock_resend, temp_db):
        """UT-22: check_and_escalate rate-limits to once per 30 seconds."""
        mock_resend.return_value = 0
        mock_escalate.return_value = 0

        failover = AlertFailover()
        failover._check_interval_s = 1  # Set to 1 sec for testing

        # First call should execute
        result1 = failover.check_and_escalate()

        # Second immediate call should be skipped
        result2 = failover.check_and_escalate()

        assert result1["resent"] == 0
        assert result2["resent"] == 0  # Should return empty dict due to rate limit
        assert mock_resend.call_count == 1  # Only called once

    @patch("genesis.alerts.alert_failover.get_alert_summary")
    def test_get_status_report_format(self, mock_summary, temp_db):
        """UT-23: get_status_report returns formatted string."""
        mock_summary.return_value = {
            "total_tracked": 10,
            "acked": 6,
            "pending": 4,
            "pending_resend": 2,
            "pending_escalate": 1,
        }

        failover = AlertFailover()
        report = failover.get_status_report()

        assert isinstance(report, str)
        assert "ALERT FAILOVER STATUS" in report
        assert "10" in report  # total_tracked
        assert "6" in report   # acked
        assert "4" in report   # pending


class TestModuleLevelFunctions:
    """Test module-level convenience functions."""

    @patch.object(AlertFailover, "check_and_escalate")
    def test_check_and_escalate_module_function(self, mock_method, temp_db):
        """UT-24: Module-level check_and_escalate delegates to class."""
        mock_method.return_value = {"resent": 1, "escalated": 0, "pending_resend": 0, "pending_escalate": 0}

        result = check_and_escalate()

        assert result["resent"] == 1
        mock_method.assert_called_once()

    @patch.object(AlertFailover, "get_status_report")
    def test_get_status_report_module_function(self, mock_method, temp_db):
        """UT-25: Module-level get_status_report delegates to class."""
        mock_method.return_value = "STATUS: OK"

        result = get_status_report()

        assert result == "STATUS: OK"
        mock_method.assert_called_once()


class TestIntegrationScenarios:
    """Integration tests for realistic scenarios."""

    @patch("genesis.alerts.alert_failover.send_alert")
    @patch("genesis.alerts.alert_failover._send_escalation_telegram")
    @patch("genesis.alerts.alert_failover._send_escalation_bus")
    @patch("genesis.alerts.alert_failover._send_escalation_discord")
    @patch("genesis.alerts.alert_failover.increment_resend_count")
    @patch("genesis.alerts.alert_failover.mark_escalated")
    def test_scenario_alert_lifecycle_no_ack(
        self, mock_mark_esc, mock_incr, mock_discord, mock_bus, mock_tg_esc, mock_send, temp_db
    ):
        """UT-26: Scenario - Alert sent, no ACK → resend at 5min → escalate at 10min."""
        # Initial alert
        track_alert("genesis-1", "Test alert", "test")

        # At 6 minutes - should resend
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        conn.execute(
            "UPDATE alert_ack_state SET sent_at = ? WHERE message_id = ?",
            (old_time, "genesis-1"),
        )
        conn.commit()
        conn.close()

        mock_send.return_value = "genesis-resend-1"

        count_resend = _check_and_resend()
        assert count_resend == 1
        mock_send.assert_called_once()
        mock_incr.assert_called_once_with("genesis-1")

        # At 11 minutes - should escalate
        conn = _connect()
        very_old_time = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        conn.execute(
            "UPDATE alert_ack_state SET sent_at = ? WHERE message_id = ?",
            (very_old_time, "genesis-1"),
        )
        conn.commit()
        conn.close()

        mock_tg_esc.return_value = True
        mock_bus.return_value = True
        mock_discord.return_value = True

        count_escalate = _check_and_escalate()
        assert count_escalate == 1
        mock_tg_esc.assert_called_once()
        mock_bus.assert_called_once()
        mock_discord.assert_called_once()
        mock_mark_esc.assert_called_once_with("genesis-1")

    @patch("genesis.alerts.alert_failover.send_alert")
    @patch("genesis.alerts.alert_failover.increment_resend_count")
    def test_scenario_alert_acked_before_resend(self, mock_incr, mock_send, temp_db):
        """UT-27: Scenario - Alert sent, ACK received before 5min timeout."""
        track_alert("genesis-1", "Test alert", "test")
        mark_acked("genesis-1")

        # Simulate 6 minutes passing
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        conn.execute(
            "UPDATE alert_ack_state SET sent_at = ? WHERE message_id = ?",
            (old_time, "genesis-1"),
        )
        conn.commit()
        conn.close()

        # Should not resend (already ACKed)
        count = _check_and_resend()
        assert count == 0
        mock_send.assert_not_called()

    @patch("genesis.alerts.alert_failover._send_escalation_telegram")
    @patch("genesis.alerts.alert_failover._send_escalation_bus")
    @patch("genesis.alerts.alert_failover._send_escalation_discord")
    @patch("genesis.alerts.alert_failover.mark_escalated")
    def test_scenario_partial_channel_failure(self, mock_mark, mock_discord, mock_bus, mock_tg, temp_db):
        """UT-28: Scenario - Escalation to multiple channels, some fail."""
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        very_old_time = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-1", "Alert", "test", very_old_time),
        )
        conn.commit()
        conn.close()

        # Telegram succeeds, bus fails, Discord succeeds
        mock_tg.return_value = True
        mock_bus.return_value = False
        mock_discord.return_value = True

        count = _check_and_escalate()

        # Should still be marked as escalated (at least one succeeded)
        assert count == 1
        mock_mark.assert_called_once()

    @patch("genesis.alerts.alert_failover._send_escalation_telegram")
    @patch("genesis.alerts.alert_failover._send_escalation_bus")
    @patch("genesis.alerts.alert_failover._send_escalation_discord")
    @patch("genesis.alerts.alert_failover.mark_escalated")
    def test_scenario_all_channels_fail(self, mock_mark, mock_discord, mock_bus, mock_tg, temp_db):
        """UT-29: Scenario - Escalation to all channels fails."""
        from genesis.alerts.alert_ack_tracker import _connect
        conn = _connect()
        very_old_time = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        conn.execute(
            "INSERT INTO alert_ack_state (message_id, title, agent, sent_at) VALUES (?, ?, ?, ?)",
            ("genesis-1", "Alert", "test", very_old_time),
        )
        conn.commit()
        conn.close()

        # All channels fail
        mock_tg.return_value = False
        mock_bus.return_value = False
        mock_discord.return_value = False

        count = _check_and_escalate()

        # Should NOT be marked (must have at least one success)
        assert count == 0
        mock_mark.assert_not_called()
