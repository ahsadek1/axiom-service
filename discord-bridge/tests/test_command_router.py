"""
test_command_router.py — Tests for CommandRouter slash command routing.

Covers TC-04, TC-05, command target resolution, and bus failure handling.
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from command_router import CommandRouter


class TestCommandTargetResolution(unittest.TestCase):
    """Tests for _resolve_target() — maps commands to agent names."""

    def setUp(self) -> None:
        self._router = CommandRouter(bus_url="http://localhost:9999", timeout_s=1)

    def test_status_routes_to_sovereign(self) -> None:
        self.assertEqual(self._router._resolve_target("/status", {}), "sovereign")

    def test_pause_routes_to_sovereign(self) -> None:
        self.assertEqual(self._router._resolve_target("/pause", {"service": "omni"}), "sovereign")

    def test_resume_routes_to_sovereign(self) -> None:
        self.assertEqual(self._router._resolve_target("/resume", {"service": "omni"}), "sovereign")

    def test_queue_routes_to_primus(self) -> None:
        self.assertEqual(self._router._resolve_target("/queue", {}), "primus")

    def test_trades_all_routes_to_alpha_execution(self) -> None:
        self.assertEqual(
            self._router._resolve_target("/trades", {"system": "all"}),
            "alpha-execution",
        )

    def test_trades_alpha_routes_to_alpha_execution(self) -> None:
        self.assertEqual(
            self._router._resolve_target("/trades", {"system": "alpha"}),
            "alpha-execution",
        )

    def test_trades_prime_routes_to_prime_execution(self) -> None:
        self.assertEqual(
            self._router._resolve_target("/trades", {"system": "prime"}),
            "prime-execution",
        )

    def test_trades_default_no_system_routes_to_alpha(self) -> None:
        """When system is omitted, defaults to alpha-execution."""
        self.assertEqual(
            self._router._resolve_target("/trades", {}),
            "alpha-execution",
        )


class TestCommandRouterRouting(unittest.TestCase):
    """Tests for CommandRouter.route() — sending commands and polling responses."""

    def _make_router(self, timeout_s: int = 2) -> CommandRouter:
        return CommandRouter(bus_url="http://localhost:9999", timeout_s=timeout_s)

    # TC-04: /status routed and response returned
    def test_route_returns_response_when_bus_replies(self) -> None:
        """TC-04: route() returns agent response text when bus has a matching reply."""
        router = self._make_router(timeout_s=5)
        message_id_holder = {}

        def mock_post(url: str, json: dict, timeout: int) -> MagicMock:
            message_id_holder["id"] = json.get("id", "")
            resp = MagicMock()
            resp.status_code = 202
            return resp

        def mock_get(url: str, timeout: int) -> MagicMock:
            # Return a matching reply on first poll
            mid = message_id_holder.get("id", "unknown")
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "messages": [
                    {
                        "id": "reply-1",
                        "message": json.dumps({
                            "reply_to": mid,
                            "response": "All systems nominal.",
                        }),
                    }
                ]
            }
            return resp

        with patch("command_router.requests.post", side_effect=mock_post), \
             patch("command_router.requests.get", side_effect=mock_get), \
             patch("command_router.requests.delete"):
            result = router.route(
                command="/status",
                args={},
                requester="Ahmed",
                reply_channel="daily-health-report",
            )

        self.assertEqual(result, "All systems nominal.")

    # TC-05: /status with agent silent → None returned
    def test_route_returns_none_on_timeout(self) -> None:
        """TC-05: route() returns None when agent doesn't respond within timeout."""
        router = self._make_router(timeout_s=1)

        def mock_post(*args, **kwargs) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 202
            return resp

        def mock_get(*args, **kwargs) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"messages": []}  # empty inbox
            return resp

        with patch("command_router.requests.post", side_effect=mock_post), \
             patch("command_router.requests.get", side_effect=mock_get):
            result = router.route(
                command="/status",
                args={},
                requester="Ahmed",
                reply_channel="daily-health-report",
            )

        self.assertIsNone(result)

    def test_route_raises_runtime_error_on_bus_failure(self) -> None:
        """Bus connectivity failure must raise RuntimeError."""
        import requests as req
        router = self._make_router(timeout_s=1)

        with patch("command_router.requests.post",
                   side_effect=req.ConnectionError("refused")):
            with self.assertRaises(RuntimeError) as ctx:
                router.route(
                    command="/status",
                    args={},
                    requester="Ahmed",
                    reply_channel="daily-health-report",
                )
        self.assertIn("unreachable", str(ctx.exception).lower())

    def test_route_raises_on_bus_non_2xx_response(self) -> None:
        """Bus returning non-2xx must raise RuntimeError."""
        router = self._make_router(timeout_s=1)

        def mock_post(*args, **kwargs) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 500
            resp.text = "Internal Server Error"
            return resp

        with patch("command_router.requests.post", side_effect=mock_post):
            with self.assertRaises(RuntimeError):
                router.route(
                    command="/status",
                    args={},
                    requester="Ahmed",
                    reply_channel="daily-health-report",
                )

    def test_route_message_format_correct(self) -> None:
        """The message posted to bus must contain command, args, requester, reply_channel."""
        router = self._make_router(timeout_s=1)
        captured = {}

        def mock_post(url: str, json: dict, timeout: int) -> MagicMock:
            captured["payload"] = json
            resp = MagicMock()
            resp.status_code = 202
            return resp

        def mock_get(*args, **kwargs) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"messages": []}
            return resp

        with patch("command_router.requests.post", side_effect=mock_post), \
             patch("command_router.requests.get", side_effect=mock_get):
            router.route("/status", {"agent": "OMNI"}, "Ahmed", "daily-health-report")

        payload = captured["payload"]
        self.assertEqual(payload["from"], "discord-bridge")
        self.assertEqual(payload["to"], "sovereign")
        inner = json.loads(payload["message"])
        self.assertEqual(inner["command"], "/status")
        self.assertEqual(inner["args"]["agent"], "OMNI")
        self.assertEqual(inner["requester"], "Ahmed")
        self.assertEqual(inner["reply_channel"], "daily-health-report")


if __name__ == "__main__":
    unittest.main()
