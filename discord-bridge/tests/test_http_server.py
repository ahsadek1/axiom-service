"""
test_http_server.py — Tests for NexusHTTPHandler (POST /send, GET /health).

Covers TC-02, TC-03, TC-10 and additional validation cases.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from http.server import HTTPServer
from unittest.mock import MagicMock, patch
import threading
import http.client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http_server
from channel_registry import ChannelRegistry


GOOD_SECRET = "test-secret-abc123"


def make_registry(tmp_dir: str) -> ChannelRegistry:
    channel_map = {
        "daily-health-report": {"agent": "SOVEREIGN", "category": "GOVERNANCE"},
        "go-verdicts": {"agent": "OMNI", "category": "TRADING"},
    }
    map_path = os.path.join(tmp_dir, "channel_map.json")
    with open(map_path, "w") as f:
        json.dump(channel_map, f)
    return ChannelRegistry(map_path=map_path)


def make_mock_bot(connected: bool = True, enqueue_result: bool = True) -> MagicMock:
    bot = MagicMock()
    bot.is_bot_connected.return_value = connected
    bot.queue_depth.return_value = 0
    bot.uptime_s.return_value = 42.0
    bot.enqueue.return_value = enqueue_result
    return bot


class TestHTTPServer(unittest.TestCase):
    """Tests for NexusHTTPHandler using a live HTTP server on a random port."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._registry = make_registry(self._tmp)
        self._bot = make_mock_bot(connected=True)
        http_server.set_dependencies(
            bot=self._bot,
            secret=GOOD_SECRET,
            registry=self._registry,
        )
        # Start on a random available port
        self._server = HTTPServer(("127.0.0.1", 0), http_server.NexusHTTPHandler)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.handle_request, daemon=True)
        # We'll use serve_forever in a thread
        self._serve_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._serve_thread.start()

    def tearDown(self) -> None:
        self._server.shutdown()

    def _post(self, path: str, body: dict, headers: dict = None) -> tuple:
        """Make an HTTP POST request; returns (status, response_dict)."""
        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=5)
        encoded = json.dumps(body).encode("utf-8")
        h = {"Content-Type": "application/json", "Content-Length": str(len(encoded))}
        if headers:
            h.update(headers)
        conn.request("POST", path, body=encoded, headers=h)
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read()
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        return status, data

    def _get(self, path: str) -> tuple:
        """Make an HTTP GET request; returns (status, response_dict)."""
        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read()
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        return status, data

    # TC-10: Health endpoint
    def test_health_returns_200_with_required_fields(self) -> None:
        """TC-10: GET /health returns 200 with bot_connected, queue_depth, uptime_s."""
        status, data = self._get("/health")
        self.assertEqual(status, 200)
        self.assertIn("status", data)
        self.assertIn("bot_connected", data)
        self.assertIn("queue_depth", data)
        self.assertIn("uptime_s", data)
        self.assertTrue(data["bot_connected"])
        self.assertEqual(data["status"], "ok")

    def test_health_returns_degraded_when_bot_disconnected(self) -> None:
        """GET /health returns degraded when bot is not connected."""
        self._bot.is_bot_connected.return_value = False
        status, data = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "degraded")
        self.assertFalse(data["bot_connected"])

    # TC-03: Auth failure
    def test_post_send_wrong_secret_returns_401(self) -> None:
        """TC-03: POST /send with wrong auth header returns 401."""
        status, _ = self._post(
            "/send",
            {"channel": "daily-health-report", "agent": "SOVEREIGN", "title": "Test"},
            headers={"X-Discord-Bridge-Secret": "wrong-secret"},
        )
        self.assertEqual(status, 401)

    def test_post_send_missing_secret_returns_401(self) -> None:
        """POST /send with no auth header returns 401."""
        status, _ = self._post(
            "/send",
            {"channel": "daily-health-report", "agent": "SOVEREIGN", "title": "Test"},
        )
        self.assertEqual(status, 401)

    # TC-02: Unknown channel
    def test_post_send_unknown_channel_returns_400(self) -> None:
        """TC-02: POST /send with unknown channel returns 400 with unknown_channel error."""
        status, data = self._post(
            "/send",
            {"channel": "nonexistent-channel", "agent": "SOVEREIGN", "title": "Test"},
            headers={"X-Discord-Bridge-Secret": GOOD_SECRET},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "unknown_channel")
        self.assertEqual(data.get("channel"), "nonexistent-channel")

    def test_post_send_missing_required_field_returns_400(self) -> None:
        """POST /send with missing title returns 400 with missing_field error."""
        status, data = self._post(
            "/send",
            {"channel": "daily-health-report", "agent": "SOVEREIGN"},  # no title
            headers={"X-Discord-Bridge-Secret": GOOD_SECRET},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "missing_field")
        self.assertEqual(data.get("field"), "title")

    def test_post_send_missing_agent_returns_400(self) -> None:
        """POST /send with missing agent returns 400."""
        status, data = self._post(
            "/send",
            {"channel": "daily-health-report", "title": "Test"},  # no agent
            headers={"X-Discord-Bridge-Secret": GOOD_SECRET},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "missing_field")

    # TC-09: Too many fields
    def test_post_send_26_fields_returns_400(self) -> None:
        """TC-09: POST /send with 26 fields returns 400 with too_many_fields error."""
        fields = [{"name": f"F{i}", "value": f"V{i}", "inline": False} for i in range(26)]
        status, data = self._post(
            "/send",
            {
                "channel": "daily-health-report",
                "agent": "SOVEREIGN",
                "title": "Too Many Fields",
                "fields": fields,
            },
            headers={"X-Discord-Bridge-Secret": GOOD_SECRET},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "too_many_fields")
        self.assertEqual(data.get("max"), 25)
        self.assertEqual(data.get("received"), 26)

    def test_post_send_bot_disconnected_returns_503(self) -> None:
        """POST /send when bot not connected returns 503."""
        self._bot.is_bot_connected.return_value = False
        status, data = self._post(
            "/send",
            {"channel": "daily-health-report", "agent": "SOVEREIGN", "title": "Test"},
            headers={"X-Discord-Bridge-Secret": GOOD_SECRET},
        )
        self.assertEqual(status, 503)
        self.assertIn("bot_not_connected", data.get("error", ""))

    def test_post_send_valid_request_returns_202(self) -> None:
        """Happy path: valid POST /send returns 202 with status=queued."""
        status, data = self._post(
            "/send",
            {"channel": "daily-health-report", "agent": "SOVEREIGN", "title": "Test"},
            headers={"X-Discord-Bridge-Secret": GOOD_SECRET},
        )
        self.assertEqual(status, 202)
        self.assertEqual(data.get("status"), "queued")
        self.assertIn("queue_depth", data)

    def test_post_send_calls_bot_enqueue(self) -> None:
        """POST /send must call bot.enqueue() with the task."""
        self._post(
            "/send",
            {"channel": "daily-health-report", "agent": "SOVEREIGN", "title": "Test"},
            headers={"X-Discord-Bridge-Secret": GOOD_SECRET},
        )
        self.assertTrue(self._bot.enqueue.called)

    def test_get_unknown_path_returns_404(self) -> None:
        """GET on unknown path returns 404."""
        status, _ = self._get("/unknown")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
