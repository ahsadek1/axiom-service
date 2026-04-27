"""
tests/test_gap002_notification_router.py — GAP-002 Notification Router Tests
"""

import sys
import os
import unittest
from unittest.mock import patch, call, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from notification_router import notify, notify_info, notify_warn, notify_escalate, NotifyTier


class TestInfoTier(unittest.TestCase):
    @patch("notification_router._post_ahmed")
    @patch("notification_router._post_discord")
    @patch("notification_router._post_bus")
    def test_info_posts_bus_and_agent_channel(self, mock_bus, mock_discord, mock_ahmed):
        notify("alpha-execution", NotifyTier.INFO, "Trade placed", "AAPL bull spread", ticker="AAPL")

        mock_bus.assert_called_once()
        mock_discord.assert_called_once()
        # Discord goes to agent's home channel
        channel_arg = mock_discord.call_args[0][0]
        self.assertEqual(channel_arg, "#execution-monitoring")
        # Ahmed NOT paged
        mock_ahmed.assert_not_called()

    @patch("notification_router._post_ahmed")
    @patch("notification_router._post_discord")
    @patch("notification_router._post_bus")
    def test_info_never_pages_ahmed(self, mock_bus, mock_discord, mock_ahmed):
        notify_info("omni", "GO verdict", "TSLA P1 bullish")
        mock_ahmed.assert_not_called()

    @patch("notification_router._post_ahmed")
    @patch("notification_router._post_discord")
    @patch("notification_router._post_bus")
    def test_info_routes_omni_to_go_verdicts(self, mock_bus, mock_discord, mock_ahmed):
        notify_info("omni", "GO", "NVDA P2")
        channel_arg = mock_discord.call_args[0][0]
        self.assertEqual(channel_arg, "#go-verdicts")


class TestWarnTier(unittest.TestCase):
    @patch("notification_router._post_ahmed")
    @patch("notification_router._post_discord")
    @patch("notification_router._post_bus")
    def test_warn_posts_bus_and_escalations_channel(self, mock_bus, mock_discord, mock_ahmed):
        notify("alpha-execution", NotifyTier.WARN, "VIX brake", "VIX too high", ticker="SPY")

        mock_bus.assert_called_once()
        mock_discord.assert_called_once()
        channel_arg = mock_discord.call_args[0][0]
        self.assertEqual(channel_arg, "#escalations")
        mock_ahmed.assert_not_called()

    @patch("notification_router._post_ahmed")
    @patch("notification_router._post_discord")
    @patch("notification_router._post_bus")
    def test_warn_never_pages_ahmed(self, mock_bus, mock_discord, mock_ahmed):
        notify_warn("omni", "OMNI CONDITIONAL", "2/4 GO votes", ticker="AAPL")
        mock_ahmed.assert_not_called()


class TestEscalateTier(unittest.TestCase):
    @patch("notification_router._post_ahmed")
    @patch("notification_router._post_discord")
    @patch("notification_router._post_bus")
    def test_escalate_posts_bus_discord_and_ahmed(self, mock_bus, mock_discord, mock_ahmed):
        notify("alpha-execution", NotifyTier.ESCALATE,
               "PHANTOM POSITION", "Fill confirmed, zero legs", ticker="NVDA")

        mock_bus.assert_called_once()
        mock_discord.assert_called_once()
        mock_ahmed.assert_called_once()

    @patch("notification_router._post_ahmed")
    @patch("notification_router._post_discord")
    @patch("notification_router._post_bus")
    def test_escalate_discord_goes_to_escalations(self, mock_bus, mock_discord, mock_ahmed):
        notify_escalate("omni", "OMNI SILENCE", "No synthesis in 25 min")
        channel_arg = mock_discord.call_args[0][0]
        self.assertEqual(channel_arg, "#escalations")

    @patch("notification_router._post_ahmed")
    @patch("notification_router._post_discord")
    @patch("notification_router._post_bus")
    def test_escalate_ahmed_receives_agent_and_title(self, mock_bus, mock_discord, mock_ahmed):
        notify_escalate("alpha-execution", "AUTH FAILURE", "401 from Alpaca", ticker="TSLA")
        ahmed_args = mock_ahmed.call_args
        self.assertIn("AUTH FAILURE", ahmed_args[0][0])
        self.assertIn("alpha-execution", ahmed_args[0][2])


class TestNeverRaises(unittest.TestCase):
    def test_bus_failure_does_not_raise(self):
        with patch("notification_router._requests") as mock_req:
            mock_req.post.side_effect = Exception("bus unreachable")
            # Should not raise
            try:
                notify_info("alpha-execution", "test", "body")
            except Exception:
                self.fail("notify_info raised despite bus failure")

    def test_discord_failure_does_not_raise(self):
        with patch("notification_router._post_bus"):
            with patch("notification_router._requests") as mock_req:
                mock_req.post.side_effect = ConnectionError("discord unreachable")
                try:
                    notify_warn("omni", "test", "body")
                except Exception:
                    self.fail("notify_warn raised despite discord failure")


class TestColorMapping(unittest.TestCase):
    @patch("notification_router._post_ahmed")
    @patch("notification_router._post_discord")
    @patch("notification_router._post_bus")
    def test_info_uses_teal(self, mock_bus, mock_discord, mock_ahmed):
        notify_info("omni", "t", "b")
        color_arg = mock_discord.call_args[0][4]
        self.assertEqual(color_arg, "teal")

    @patch("notification_router._post_ahmed")
    @patch("notification_router._post_discord")
    @patch("notification_router._post_bus")
    def test_warn_uses_yellow(self, mock_bus, mock_discord, mock_ahmed):
        notify_warn("omni", "t", "b")
        color_arg = mock_discord.call_args[0][4]
        self.assertEqual(color_arg, "yellow")

    @patch("notification_router._post_ahmed")
    @patch("notification_router._post_discord")
    @patch("notification_router._post_bus")
    def test_escalate_uses_red(self, mock_bus, mock_discord, mock_ahmed):
        notify_escalate("omni", "t", "b")
        color_arg = mock_discord.call_args[0][4]
        self.assertEqual(color_arg, "red")


class TestUnknownAgent(unittest.TestCase):
    @patch("notification_router._post_ahmed")
    @patch("notification_router._post_discord")
    @patch("notification_router._post_bus")
    def test_unknown_agent_defaults_to_daily_health(self, mock_bus, mock_discord, mock_ahmed):
        notify_info("new-unknown-agent", "t", "b")
        channel_arg = mock_discord.call_args[0][0]
        self.assertEqual(channel_arg, "#daily-health-report")


if __name__ == "__main__":
    unittest.main()
