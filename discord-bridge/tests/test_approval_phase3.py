"""
test_approval_phase3.py — Tests for Phase 3 approval button system.

Covers: pending_approvals store, approval_handler routing,
http_server /approve-request endpoint, discord_client.post_approval_request.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
sys.path.insert(0, "/Users/ahmedsadek/nexus")

import pending_approvals as pa


# ---------------------------------------------------------------------------
# TC: Pending approvals store
# ---------------------------------------------------------------------------

class TestPendingApprovalsStore(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._db = os.path.join(self._tmp, "test_approvals.db")
        pa.init_db(db_path=self._db)

    def _insert(self, rid: str = "req-1") -> None:
        pa.insert(
            request_id=rid,
            title=f"Test Approval {rid}",
            agent="genesis",
            reply_to="genesis",
            payload={"title": f"Test {rid}"},
            db_path=self._db,
        )

    # TC: round-trip
    def test_pending_store_round_trip(self) -> None:
        """insert → get returns the correct record."""
        self._insert("req-abc")
        record = pa.get("req-abc", db_path=self._db)
        self.assertIsNotNone(record)
        self.assertEqual(record["request_id"], "req-abc")
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["agent"], "genesis")

    def test_get_returns_none_for_unknown(self) -> None:
        result = pa.get("does-not-exist", db_path=self._db)
        self.assertIsNone(result)

    # TC: approve button marks approved
    def test_approve_button_marks_approved(self) -> None:
        self._insert("req-approve")
        pa.mark_decided("req-approve", "approved", db_path=self._db)
        record = pa.get("req-approve", db_path=self._db)
        self.assertEqual(record["status"], "approved")
        self.assertIsNotNone(record["decided_at"])

    # TC: reject button marks rejected
    def test_reject_button_marks_rejected(self) -> None:
        self._insert("req-reject")
        pa.mark_decided("req-reject", "rejected", db_path=self._db)
        record = pa.get("req-reject", db_path=self._db)
        self.assertEqual(record["status"], "rejected")

    # TC: double tap — already decided
    def test_double_tap_does_not_change_status(self) -> None:
        """Second mark_decided call on already-decided request is idempotent."""
        self._insert("req-double")
        pa.mark_decided("req-double", "approved", db_path=self._db)
        pa.mark_decided("req-double", "rejected", db_path=self._db)  # should be no-op
        record = pa.get("req-double", db_path=self._db)
        # Status should remain "approved" — mark_decided uses WHERE status='pending'
        self.assertEqual(record["status"], "approved")

    def test_set_message_stores_discord_ids(self) -> None:
        self._insert("req-msg")
        pa.set_message("req-msg", "123456789", "987654321", db_path=self._db)
        record = pa.get("req-msg", db_path=self._db)
        self.assertEqual(record["message_id"], "123456789")
        self.assertEqual(record["channel_id"], "987654321")

    # TC: expired requests cleaned
    def test_expired_requests_cleaned_on_startup(self) -> None:
        """Requests older than EXPIRY_SECONDS are auto-expired."""
        # Insert a request with an old created_at
        conn = pa._connect(self._db)
        old_time = time.time() - (pa.EXPIRY_SECONDS + 100)
        conn.execute("""
            INSERT INTO pending_approvals
                (request_id, title, agent, reply_to, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, ("req-old", "Old Request", "genesis", "genesis", old_time))
        conn.commit()
        conn.close()

        # expire_old should mark it expired
        count = pa.expire_old(db_path=self._db)
        self.assertGreaterEqual(count, 1)
        record = pa.get("req-old", db_path=self._db)
        self.assertEqual(record["status"], "expired")

    def test_recent_request_not_expired(self) -> None:
        """A fresh request must NOT be expired."""
        self._insert("req-fresh")
        pa.expire_old(db_path=self._db)
        record = pa.get("req-fresh", db_path=self._db)
        self.assertEqual(record["status"], "pending")

    def test_list_pending_returns_only_pending(self) -> None:
        self._insert("req-p1")
        self._insert("req-p2")
        pa.mark_decided("req-p1", "approved", db_path=self._db)
        pending = pa.list_pending(db_path=self._db)
        ids = [r["request_id"] for r in pending]
        self.assertNotIn("req-p1", ids)
        self.assertIn("req-p2", ids)


# ---------------------------------------------------------------------------
# TC: ApprovalHandler
# ---------------------------------------------------------------------------

class TestApprovalHandler(unittest.TestCase):

    def setUp(self) -> None:
        from approval_handler import ApprovalHandler
        mock_registry = MagicMock()
        mock_registry.resolve_color.return_value = 0xF39C12
        self._handler = ApprovalHandler(registry=mock_registry)
        self._tmp = tempfile.mkdtemp()
        self._db = os.path.join(self._tmp, "test_approvals.db")
        pa.init_db(db_path=self._db)
        # Monkey-patch pending_approvals module used by handler
        import pending_approvals as _pa
        self._orig_get = _pa.get
        self._orig_mark = _pa.mark_decided

    def tearDown(self) -> None:
        import pending_approvals as _pa
        _pa.get = self._orig_get
        _pa.mark_decided = self._orig_mark

    def test_is_approval_interaction_approve(self) -> None:
        """approve:request-id custom_id is recognized."""
        interaction = MagicMock()
        interaction.type = __import__("discord").InteractionType.component
        interaction.data = {"custom_id": "approve:req-123"}
        self.assertTrue(self._handler.is_approval_interaction(interaction))

    def test_is_approval_interaction_reject(self) -> None:
        interaction = MagicMock()
        interaction.type = __import__("discord").InteractionType.component
        interaction.data = {"custom_id": "reject:req-456"}
        self.assertTrue(self._handler.is_approval_interaction(interaction))

    # TC: unknown custom_id ignored
    def test_unknown_custom_id_not_approval(self) -> None:
        interaction = MagicMock()
        interaction.type = __import__("discord").InteractionType.component
        interaction.data = {"custom_id": "some-other-button"}
        self.assertFalse(self._handler.is_approval_interaction(interaction))

    # TC: agent notified on decision
    def test_notify_agent_posts_to_bus(self) -> None:
        """_notify_agent fires a POST to the message bus."""
        with patch("approval_handler.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=202)
            self._handler._notify_agent(
                reply_to="genesis",
                request_id="req-notify",
                decision="approved",
                title="Test Spec",
            )
            self.assertTrue(mock_post.called)
            call_args = mock_post.call_args
            body = call_args[1]["json"]
            msg = json.loads(body["message"])
            self.assertEqual(msg["decision"], "approved")
            self.assertEqual(msg["request_id"], "req-notify")

    def test_notify_agent_bus_failure_does_not_raise(self) -> None:
        """Bus failure must not propagate as exception."""
        import requests as req
        with patch("approval_handler.requests.post",
                   side_effect=req.ConnectionError("refused")):
            # Must not raise
            self._handler._notify_agent("genesis", "req-fail", "approved", "Test")


# ---------------------------------------------------------------------------
# TC: build_approval_view
# ---------------------------------------------------------------------------

class TestBuildApprovalView(unittest.TestCase):

    def test_view_has_two_buttons(self) -> None:
        from approval_handler import build_approval_view
        view = build_approval_view("req-view-test")
        self.assertEqual(len(view.children), 2)

    def test_approve_button_custom_id(self) -> None:
        from approval_handler import build_approval_view
        view = build_approval_view("req-btn-test")
        custom_ids = [c.custom_id for c in view.children]
        self.assertIn("approve:req-btn-test", custom_ids)

    def test_reject_button_custom_id(self) -> None:
        from approval_handler import build_approval_view
        view = build_approval_view("req-btn-test")
        custom_ids = [c.custom_id for c in view.children]
        self.assertIn("reject:req-btn-test", custom_ids)


# ---------------------------------------------------------------------------
# TC: discord_client.post_approval_request
# ---------------------------------------------------------------------------

class TestDiscordClientApprovalRequest(unittest.TestCase):

    def test_post_approval_request_returns_true_on_202(self) -> None:
        import discord_client as dc
        dc.BRIDGE_SECRET = "test-secret"
        dc.BRIDGE_URL = "http://localhost:8011"

        with patch("discord_client.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=202)
            result = dc.post_approval_request(
                request_id="req-client-test",
                title="SPEC APPROVAL — test",
                agent="genesis",
                reply_to="genesis",
            )
        self.assertTrue(result)

    def test_post_approval_request_returns_false_on_connection_error(self) -> None:
        import discord_client as dc
        import requests as req
        dc.BRIDGE_SECRET = "test-secret"

        with patch("discord_client.requests.post",
                   side_effect=req.ConnectionError("refused")):
            result = dc.post_approval_request(
                request_id="req-fail",
                title="Test",
                agent="genesis",
                reply_to="genesis",
            )
        self.assertFalse(result)

    def test_post_approval_request_no_secret_returns_false(self) -> None:
        import discord_client as dc
        original = dc.BRIDGE_SECRET
        dc.BRIDGE_SECRET = ""
        try:
            result = dc.post_approval_request(
                request_id="req-no-secret",
                title="Test",
                agent="genesis",
                reply_to="genesis",
            )
            self.assertFalse(result)
        finally:
            dc.BRIDGE_SECRET = original


if __name__ == "__main__":
    unittest.main()
