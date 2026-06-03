"""
test_agent_dispatcher.py — Tests for AgentDispatcher routing and response collection.
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_dispatcher import AgentDispatcher


class TestAgentDispatcher(unittest.TestCase):

    def setUp(self) -> None:
        self._dispatcher = AgentDispatcher()

    def test_dispatch_timeout_returns_error_string(self) -> None:
        """TC: bus sends but no response within timeout → returns 'No response' string."""
        def mock_post(*args, **kwargs) -> MagicMock:
            r = MagicMock()
            r.status_code = 202
            return r

        def mock_get(*args, **kwargs) -> MagicMock:
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"messages": []}
            return r

        # Use a very short timeout for test speed
        self._dispatcher.__class__  # just reference
        import agent_dispatcher
        original = agent_dispatcher.COMMAND_TIMEOUT_S
        agent_dispatcher.COMMAND_TIMEOUT_S = 1

        try:
            with patch("agent_dispatcher.requests.post", side_effect=mock_post), \
                 patch("agent_dispatcher.requests.get", side_effect=mock_get):
                result = self._dispatcher.dispatch("sovereign", "health_sweep", "status")
        finally:
            agent_dispatcher.COMMAND_TIMEOUT_S = original

        self.assertIn("No response", result)
        self.assertIn("sovereign", result.lower())

    def test_genesis_self_report_no_bus_call(self) -> None:
        """TC: genesis dispatch reads pipeline directly, no bus POST."""
        mock_dashboard = [
            {"component_name": "discord-bridge", "build_state": "DEPLOYED",
             "state_display": "✅ Deployed"}
        ]
        with patch("agent_dispatcher.requests.post") as mock_post, \
             patch("agent_dispatcher.requests.get") as mock_get:

            # Patch build_pipeline import inside the method
            mock_module = MagicMock()
            mock_module.get_dashboard.return_value = mock_dashboard

            import sys
            sys.modules["build_pipeline"] = mock_module

            result = self._dispatcher.dispatch("genesis", "build_status", "build queue")

            # Bus should NOT be called
            mock_post.assert_not_called()
            mock_get.assert_not_called()

        self.assertIn("discord-bridge", result)
        # Cleanup mock module
        if "build_pipeline" in sys.modules:
            del sys.modules["build_pipeline"]

    def test_dispatch_via_bus_returns_response(self) -> None:
        """TC: bus dispatch returns matched response text."""
        message_id_holder = {}

        def mock_post(url, json, timeout):
            message_id_holder["id"] = json.get("id", "")
            r = MagicMock()
            r.status_code = 202
            return r

        def mock_get(url, timeout):
            mid = message_id_holder.get("id", "")
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {
                "messages": [{
                    "id": "reply-1",
                    "message": json.dumps({
                        "reply_to": mid,
                        "response": "All 9 services nominal.",
                    })
                }]
            }
            return r

        with patch("agent_dispatcher.requests.post", side_effect=mock_post), \
             patch("agent_dispatcher.requests.get", side_effect=mock_get), \
             patch("agent_dispatcher.requests.delete"):
            result = self._dispatcher.dispatch("sovereign", "health_sweep", "status")

        self.assertEqual(result, "All 9 services nominal.")

    def test_dispatch_execution_connection_error(self) -> None:
        """TC: execution service unreachable → returns helpful error string."""
        import requests as req
        with patch("agent_dispatcher.requests.get",
                   side_effect=req.ConnectionError("refused")):
            result = self._dispatcher.dispatch(
                "alpha-execution", "trades_summary", "trades"
            )
        self.assertIn("not reachable", result)

    def test_dispatch_bus_unreachable_returns_error(self) -> None:
        """TC: bus unreachable → returns error string, no raise."""
        import requests as req
        with patch("agent_dispatcher.requests.post",
                   side_effect=req.ConnectionError("refused")):
            result = self._dispatcher.dispatch("sovereign", "health_sweep", "status")
        self.assertIn("unreachable", result.lower())

    def test_get_emoji_known_agent(self) -> None:
        self.assertEqual(self._dispatcher.get_emoji("sovereign"), "👑")
        self.assertEqual(self._dispatcher.get_emoji("omni"), "🧠")
        self.assertEqual(self._dispatcher.get_emoji("genesis"), "🌱")

    def test_get_emoji_unknown_agent(self) -> None:
        self.assertEqual(self._dispatcher.get_emoji("unknown_agent"), "🤖")

    def test_get_color_known_agent(self) -> None:
        self.assertEqual(self._dispatcher.get_color("sovereign"), "blue")
        self.assertEqual(self._dispatcher.get_color("omni"), "purple")
        self.assertEqual(self._dispatcher.get_color("genesis"), "green")

    def test_dispatch_never_raises(self) -> None:
        """TC: even with all mocks broken, dispatch returns a string, never raises."""
        with patch("agent_dispatcher.requests.post", side_effect=Exception("chaos")):
            try:
                result = self._dispatcher.dispatch("sovereign", "health_sweep", "test")
                self.assertIsInstance(result, str)
            except Exception:
                self.fail("dispatch() raised an exception — it must never raise")

    def test_build_message_body_health(self) -> None:
        body = self._dispatcher._build_message_body("health_sweep", "status", "id-1")
        self.assertEqual(body["type"], "health_request")
        self.assertEqual(body["message_id"], "id-1")

    def test_build_message_body_ticker(self) -> None:
        body = self._dispatcher._build_message_body("ticker_query", "AAPL", "id-2")
        self.assertEqual(body["type"], "verdict_request")
        self.assertEqual(body["ticker"], "AAPL")


if __name__ == "__main__":
    unittest.main()
