"""
Tests for genesis/alerts/alert_dispatcher.py
=============================================

Tests cover:
- Primary Telegram send path
- Fallback to bus on Telegram failure
- Timeout handling
- Network failure handling
- Message ID generation and uniqueness
- Empty title validation

Author: GENESIS 🌱
Date: 2026-05-16
"""

import pytest
from unittest.mock import patch, MagicMock
import requests

from genesis.alerts.alert_dispatcher import (
    send_alert,
    _send_telegram,
    _send_bus,
    _generate_message_id,
    get_message_id_prefix,
)


class TestMessageIDGeneration:
    """Test message ID generation and uniqueness."""

    def test_generate_message_id_format(self):
        """UT-1: Message IDs have correct format."""
        msg_id = _generate_message_id()
        assert msg_id.startswith("genesis-")
        parts = msg_id.split("-")
        assert len(parts) >= 3  # genesis-timestamp-counter
        assert parts[1].isdigit()  # timestamp is numeric

    def test_generate_message_id_uniqueness(self):
        """UT-2: Rapid calls generate unique IDs."""
        ids = [_generate_message_id() for _ in range(100)]
        assert len(set(ids)) == 100, "IDs must be unique"

    def test_message_id_prefix(self):
        """UT-3: Message ID prefix is consistent."""
        prefix = get_message_id_prefix()
        assert prefix == "genesis-"


class TestTelegramSend:
    """Test primary Telegram send path."""

    @patch("genesis.alerts.alert_dispatcher.requests.post")
    def test_telegram_send_success(self, mock_post):
        """UT-4: Successful Telegram send returns message ID."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        msg_id = _send_telegram(
            title="Test alert",
            body="Test body",
            agent="test-agent",
            tier="ESCALATE",
        )

        assert msg_id is not None
        assert msg_id.startswith("genesis-")
        mock_post.assert_called_once()

    @patch("genesis.alerts.alert_dispatcher.requests.post")
    def test_telegram_send_includes_message_id(self, mock_post):
        """UT-5: Telegram message includes message ID in text."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        _send_telegram(
            title="Alert",
            body="Body",
            agent="test",
            tier="ESCALATE",
        )

        call_args = mock_post.call_args
        sent_data = call_args[1]["json"]
        assert "ID: genesis-" in sent_data["text"]

    @patch("genesis.alerts.alert_dispatcher.requests.post")
    def test_telegram_timeout_returns_none(self, mock_post):
        """UT-6: Telegram timeout returns None for fallback."""
        mock_post.side_effect = requests.exceptions.Timeout("timeout")

        msg_id = _send_telegram(
            title="Alert",
            body="Body",
            agent="test",
            tier="ESCALATE",
        )

        assert msg_id is None

    @patch("genesis.alerts.alert_dispatcher.requests.post")
    def test_telegram_http_error_returns_none(self, mock_post):
        """UT-7: Telegram HTTP error returns None for fallback."""
        mock_post.side_effect = requests.exceptions.RequestException("error")

        msg_id = _send_telegram(
            title="Alert",
            body="Body",
            agent="test",
            tier="ESCALATE",
        )

        assert msg_id is None


class TestBusFallback:
    """Test fallback to message bus."""

    @patch("genesis.alerts.alert_dispatcher.requests.post")
    def test_bus_send_success(self, mock_post):
        """UT-8: Successful bus send returns message ID."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        msg_id = _send_bus(
            title="Alert",
            body="Body",
            agent="test",
            tier="ESCALATE",
        )

        assert msg_id is not None
        assert msg_id.startswith("genesis-")

    @patch("genesis.alerts.alert_dispatcher.requests.post")
    def test_bus_send_format(self, mock_post):
        """UT-9: Bus message has correct format."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        _send_bus(
            title="Test alert",
            body="Test body",
            agent="test-agent",
            tier="WARN",
        )

        call_args = mock_post.call_args
        sent_data = call_args[1]["json"]
        assert sent_data["from"] == "genesis-alerts"
        assert sent_data["to"] == "sovereign"
        assert "[ALERT-FALLBACK]" in sent_data["message"]

    @patch("genesis.alerts.alert_dispatcher.requests.post")
    def test_bus_timeout_returns_none(self, mock_post):
        """UT-10: Bus timeout returns None."""
        mock_post.side_effect = requests.exceptions.Timeout("timeout")

        msg_id = _send_bus(
            title="Alert",
            body="Body",
            agent="test",
            tier="ESCALATE",
        )

        assert msg_id is None


class TestSendAlertIntegration:
    """Integration tests for send_alert public API."""

    @patch("genesis.alerts.alert_dispatcher._send_telegram")
    @patch("genesis.alerts.alert_dispatcher._send_bus")
    def test_send_alert_tries_telegram_first(self, mock_bus, mock_tg):
        """UT-11: send_alert tries Telegram first."""
        mock_tg.return_value = "genesis-123-1"
        mock_bus.return_value = None

        msg_id = send_alert(
            title="Alert",
            body="Body",
            agent="test",
            tier="ESCALATE",
        )

        assert msg_id == "genesis-123-1"
        mock_tg.assert_called_once()
        mock_bus.assert_not_called()

    @patch("genesis.alerts.alert_dispatcher._send_telegram")
    @patch("genesis.alerts.alert_dispatcher._send_bus")
    def test_send_alert_falls_back_to_bus_on_telegram_failure(self, mock_bus, mock_tg):
        """UT-12: send_alert falls back to bus if Telegram fails."""
        mock_tg.return_value = None
        mock_bus.return_value = "genesis-123-2"

        msg_id = send_alert(
            title="Alert",
            body="Body",
            agent="test",
            tier="ESCALATE",
        )

        assert msg_id == "genesis-123-2"
        mock_tg.assert_called_once()
        mock_bus.assert_called_once()

    @patch("genesis.alerts.alert_dispatcher._send_telegram")
    @patch("genesis.alerts.alert_dispatcher._send_bus")
    def test_send_alert_returns_none_if_both_fail(self, mock_bus, mock_tg):
        """UT-13: send_alert returns None if both channels fail."""
        mock_tg.return_value = None
        mock_bus.return_value = None

        msg_id = send_alert(
            title="Alert",
            body="Body",
            agent="test",
            tier="ESCALATE",
        )

        assert msg_id is None

    def test_send_alert_rejects_empty_title(self):
        """UT-14: send_alert rejects empty title."""
        with pytest.raises(ValueError):
            send_alert(
                title="",
                body="Body",
                agent="test",
                tier="ESCALATE",
            )

    @patch("genesis.alerts.alert_dispatcher._send_telegram")
    def test_send_alert_includes_all_parameters(self, mock_tg):
        """UT-15: send_alert passes all parameters correctly."""
        mock_tg.return_value = "genesis-123-1"

        send_alert(
            title="Custom title",
            body="Custom body",
            agent="custom-agent",
            tier="WARN",
        )

        call_args = mock_tg.call_args
        assert call_args.kwargs["title"] == "Custom title"
        assert call_args.kwargs["body"] == "Custom body"
        assert call_args.kwargs["agent"] == "custom-agent"
        assert call_args.kwargs["tier"] == "WARN"

    @patch("genesis.alerts.alert_dispatcher._send_telegram")
    def test_send_alert_default_agent(self, mock_tg):
        """UT-16: send_alert defaults agent to 'genesis'."""
        mock_tg.return_value = "genesis-123-1"

        send_alert(
            title="Alert",
            body="Body",
        )

        call_args = mock_tg.call_args
        assert call_args.kwargs["agent"] == "genesis"

    @patch("genesis.alerts.alert_dispatcher._send_telegram")
    def test_send_alert_default_tier(self, mock_tg):
        """UT-17: send_alert defaults tier to 'ESCALATE'."""
        mock_tg.return_value = "genesis-123-1"

        send_alert(
            title="Alert",
            body="Body",
        )

        call_args = mock_tg.call_args
        assert call_args.kwargs["tier"] == "ESCALATE"


class TestTelegramHTMLFormatting:
    """Test Telegram HTML formatting edge cases."""

    @patch("genesis.alerts.alert_dispatcher.requests.post")
    def test_telegram_message_uses_html_parsing(self, mock_post):
        """UT-18: Telegram message sets HTML parse mode."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        _send_telegram(
            title="Alert",
            body="Body",
            agent="test",
            tier="ESCALATE",
        )

        call_args = mock_post.call_args
        sent_data = call_args[1]["json"]
        assert sent_data["parse_mode"] == "HTML"

    @patch("genesis.alerts.alert_dispatcher.requests.post")
    def test_telegram_message_contains_bold_title(self, mock_post):
        """UT-19: Telegram message bolds the title."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        _send_telegram(
            title="Critical Alert",
            body="Body",
            agent="test",
            tier="ESCALATE",
        )

        call_args = mock_post.call_args
        sent_data = call_args[1]["json"]
        assert "<b>Critical Alert</b>" in sent_data["text"]

    @patch("genesis.alerts.alert_dispatcher.requests.post")
    def test_telegram_message_contains_alert_emoji(self, mock_post):
        """UT-20: Telegram message includes alert emoji."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        _send_telegram(
            title="Alert",
            body="Body",
            agent="test",
            tier="ESCALATE",
        )

        call_args = mock_post.call_args
        sent_data = call_args[1]["json"]
        assert "🚨" in sent_data["text"]
