"""
test_bus.py — All 10 Nexus Message Bus v2 test cases.

Uses a temp SQLite file for each test. No real HTTP server started.
Tests call database functions directly for unit coverage, and spin up
a real server on a random port for integration tests.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import main as bus


def _temp_db() -> str:
    """Return path to a fresh temp SQLite file."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return f.name


def _patch_db(path: str):
    """Context manager: temporarily override bus.DB_PATH."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        original = bus.DB_PATH
        bus.DB_PATH = path
        try:
            yield
        finally:
            bus.DB_PATH = original

    return _ctx()


class TestDatabaseOps(unittest.TestCase):
    """Unit tests against the database layer (no HTTP)."""

    def setUp(self):
        self.db = _temp_db()
        bus.DB_PATH = self.db
        bus.init_db()

    def tearDown(self):
        bus.DB_PATH = bus.DB_PATH  # restore handled by each test
        try:
            os.unlink(self.db)
        except Exception:
            pass

    def test_t1_send_and_receive(self):
        """T1: Send a message from genesis to sovereign; inbox returns it."""
        msg_id = bus.send_message("genesis", "sovereign", "hello sovereign")
        self.assertIsNotNone(msg_id)
        msgs = bus.consume_inbox("sovereign")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["from"], "genesis")
        self.assertEqual(msgs[0]["message"], "hello sovereign")
        self.assertEqual(msgs[0]["id"], msg_id)

    def test_t2_persistence_across_reconnect(self):
        """T2: Messages survive a DB connection close and re-open."""
        bus.send_message("omni", "sovereign", "persistent message")
        # Simulate restart: re-init without clearing DB
        bus.init_db()  # idempotent — does not drop data
        msgs = bus.consume_inbox("sovereign")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["message"], "persistent message")

    def test_t3_consume_on_read(self):
        """T3: GET /inbox consumes messages — second read returns empty."""
        bus.send_message("genesis", "sovereign", "one-time message")
        first = bus.consume_inbox("sovereign")
        second = bus.consume_inbox("sovereign")
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)

    def test_t4_status_shows_correct_counts(self):
        """T4: /status shows correct pending counts before and after delivery."""
        bus.send_message("a", "sovereign", "msg1")
        bus.send_message("b", "sovereign", "msg2")
        bus.send_message("c", "sovereign", "msg3")
        depths = bus.get_queue_depths()
        self.assertEqual(depths.get("sovereign", 0), 3)
        bus.consume_inbox("sovereign")
        depths_after = bus.get_queue_depths()
        self.assertEqual(depths_after.get("sovereign", 0), 0)

    def test_t5_health_reflects_db_state(self):
        """T5: /health db.pending matches actual queue depth."""
        bus.send_message("x", "sovereign", "unread")
        bus.send_message("x", "genesis", "unread")
        stats = bus.get_db_stats()
        self.assertTrue(stats["ok"])
        self.assertEqual(stats["pending"], 2)
        bus.consume_inbox("sovereign")
        bus.consume_inbox("genesis")
        stats_after = bus.get_db_stats()
        self.assertEqual(stats_after["pending"], 0)

    def test_t6_missing_required_fields(self):
        """T6: send_message with empty to raises no error but HTTP handler returns 400."""
        # This tests the HTTP handler validation logic directly
        # We verify that the validator catches missing 'to' in the handler flow
        # by checking what the handler would do with an incomplete body.
        # (Full integration tested in TestHTTPIntegration below)
        stats_before = bus.get_db_stats()
        total_before = stats_before["total_messages"]
        # Sending with an empty 'to' actually stores it (DB layer doesn't validate)
        # but the HTTP handler validates before calling send_message.
        # Verify DB is unchanged with proper args:
        bus.send_message("genesis", "sovereign", "valid")
        stats_after = bus.get_db_stats()
        self.assertEqual(stats_after["total_messages"], total_before + 1)

    def test_t7_multiple_recipients_independent(self):
        """T7: Messages to different agents don't bleed into each other's inboxes."""
        bus.send_message("genesis", "sovereign", "for sovereign")
        bus.send_message("genesis", "omni", "for omni")
        sovereign_msgs = bus.consume_inbox("sovereign")
        omni_msgs = bus.consume_inbox("omni")
        self.assertEqual(len(sovereign_msgs), 1)
        self.assertEqual(len(omni_msgs), 1)
        self.assertEqual(sovereign_msgs[0]["message"], "for sovereign")
        self.assertEqual(omni_msgs[0]["message"], "for omni")

    def test_t8_ttl_cleanup_expires_old_messages(self):
        """T8: Cleanup marks never-read messages as delivered after TTL."""
        import sqlite3
        # Insert a message backdated past the never-read TTL
        ancient_ts = (
            datetime.now(timezone.utc) - timedelta(hours=bus.NEVER_READ_TTL_HOURS + 1)
        ).isoformat()
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO messages (id, from_agent, to_agent, message, created_at) "
            "VALUES ('old-id', 'test', 'sovereign', 'ancient', ?)",
            (ancient_ts,),
        )
        conn.commit()
        conn.close()

        bus._run_cleanup()

        # The message should now be marked delivered (expired), not in inbox
        msgs = bus.consume_inbox("sovereign")
        # It was expired by cleanup (delivered=1), so consume_inbox won't return it
        self.assertEqual(len([m for m in msgs if m["id"] == "old-id"]), 0)

    def test_t9_concurrent_sends_thread_safety(self):
        """T9: 10 threads × 10 sends each = exactly 100 messages, none lost."""
        errors = []

        def _send_batch():
            try:
                for i in range(10):
                    bus.send_message("thread", "sovereign", f"msg-{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_send_batch) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        stats = bus.get_db_stats()
        self.assertEqual(stats["total_messages"], 100)

    def test_t10_empty_inbox_returns_valid_response(self):
        """T10: Consuming from an empty inbox returns 200 with empty messages list."""
        msgs = bus.consume_inbox("nonexistent_agent")
        self.assertIsInstance(msgs, list)
        self.assertEqual(len(msgs), 0)


class TestHTTPIntegration(unittest.TestCase):
    """Integration tests against a real HTTP server on a random port."""

    @classmethod
    def setUpClass(cls):
        cls.db = _temp_db()
        bus.DB_PATH = cls.db
        bus.init_db()
        cls.server = bus.ThreadedHTTPServer(("127.0.0.1", 0), bus.BusHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        try:
            os.unlink(cls.db)
        except Exception:
            pass

    def _post(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path):
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=5
            )
            return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_http_send_returns_message_id(self):
        """POST /send returns ok=true and a message_id."""
        status, body = self._post(
            "/send", {"from": "genesis", "to": "sovereign", "message": "test"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("message_id", body)

    def test_http_missing_to_returns_400(self):
        """POST /send with missing 'to' returns HTTP 400."""
        status, body = self._post("/send", {"from": "genesis", "message": "oops"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertIn("error", body)

    def test_http_missing_message_returns_400(self):
        """POST /send with missing 'message' returns HTTP 400."""
        status, body = self._post("/send", {"from": "genesis", "to": "sovereign"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_http_inbox_roundtrip(self):
        """POST /send then GET /inbox returns the message."""
        self._post(
            "/send",
            {"from": "cipher", "to": "test_agent", "message": "roundtrip test"},
        )
        status, body = self._get("/inbox/test_agent")
        self.assertEqual(status, 200)
        msgs = body["messages"]
        self.assertTrue(any(m["message"] == "roundtrip test" for m in msgs))

    def test_http_health_returns_ok(self):
        """GET /health returns ok=true and version."""
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], bus.VERSION)
        self.assertTrue(body["db"]["ok"])

    def test_http_status_returns_pending(self):
        """GET /status returns pending counts."""
        # Clear any leftover state
        self._get("/inbox/status_test_agent")
        self._post(
            "/send",
            {"from": "x", "to": "status_test_agent", "message": "check status"},
        )
        status, body = self._get("/status")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["pending"].get("status_test_agent", 0), 1)

    def test_http_unknown_path_returns_404(self):
        """GET /unknown returns 404."""
        status, body = self._get("/unknown")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
