"""
test_sentinel_escalation.py — SENTINEL cross-machine escalation routing tests.

Verifies that SENTINEL routes critical failures to SOVEREIGN via message bus,
with Telegram group fallback, and never uses sessions_send.
"""

import sys
import os
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sovereign_escalator


class TestSovereignEscalatorBusRouting:
    def test_routes_to_bus_on_success(self):
        """Bus available → escalation delivered via bus."""
        with patch("sovereign_escalator.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            result = sovereign_escalator.escalate_to_sovereign("test alert", level="WARNING")
        assert result is True
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        url = args[0] if args else kwargs.get("url", "")
        assert "9999" in url or "send" in url.lower()

    def test_falls_back_to_telegram_when_bus_down(self):
        """Bus unreachable → fallback to Telegram group."""
        call_count = {"n": 0}

        def _side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ConnectionError("Bus unreachable")
            return MagicMock(status_code=200)

        with patch("sovereign_escalator.requests.post", side_effect=_side_effect), \
             patch.object(sovereign_escalator, "FALLBACK_BOT_TOKEN", "test_token"), \
             patch.object(sovereign_escalator, "FALLBACK_CHAT_ID", "-1003579956463"):
            result = sovereign_escalator.escalate_to_sovereign("critical event", level="CRITICAL")
        assert result is True
        assert call_count["n"] >= 2  # bus attempt + Telegram fallback

    def test_both_down_returns_false(self):
        """Bus and Telegram both fail → returns False, no exception raised."""
        with patch("sovereign_escalator.requests.post", side_effect=ConnectionError("down")), \
             patch.object(sovereign_escalator, "FALLBACK_BOT_TOKEN", "test_token"):
            result = sovereign_escalator.escalate_to_sovereign("both down", level="CRITICAL")
        assert result is False

    def test_dedup_suppresses_within_window(self):
        """Same dedup_key within window → suppressed (returns True silently)."""
        sovereign_escalator._last_escalated.clear()
        with patch("sovereign_escalator.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            # First call delivers
            r1 = sovereign_escalator.escalate_to_sovereign("msg", dedup_key="test_dedup")
            # Second call within dedup window
            r2 = sovereign_escalator.escalate_to_sovereign("msg", dedup_key="test_dedup")
        assert r1 is True
        assert r2 is True
        assert mock_post.call_count == 1  # Only one actual delivery

    def test_dedup_resets_after_window(self):
        """Same dedup_key after window expires → re-delivers."""
        sovereign_escalator._last_escalated.clear()
        # Fake that last send was DEDUP_WINDOW_S+1 seconds ago
        sovereign_escalator._last_escalated["old_key"] = time.time() - (sovereign_escalator._DEDUP_WINDOW_S + 1)
        with patch("sovereign_escalator.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            result = sovereign_escalator.escalate_to_sovereign("new msg", dedup_key="old_key")
        assert result is True
        mock_post.assert_called_once()

    def test_from_field_identifies_sender_as_sentinel(self):
        """Bus payload must include from='sentinel'."""
        with patch("sovereign_escalator.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            sovereign_escalator.escalate_to_sovereign("sentinel test")
        _, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        assert payload.get("from") == "sentinel"

    def test_to_field_targets_sovereign(self):
        """Bus payload must include to='sovereign'."""
        with patch("sovereign_escalator.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            sovereign_escalator.escalate_to_sovereign("sovereign target test")
        _, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        assert payload.get("to") == "sovereign"

    def test_never_raises_on_any_error(self):
        """Must never raise an exception regardless of failure mode."""
        with patch("sovereign_escalator.requests.post", side_effect=Exception("unexpected")):
            try:
                sovereign_escalator.escalate_to_sovereign("exception test")
            except Exception as exc:
                raise AssertionError(f"escalate_to_sovereign raised: {exc}")


class TestNotifierEscalationWiring:
    """Verify notifier wires escalation for critical failure classes."""

    def test_sovereign_escalation_classes_defined(self):
        """_SOVEREIGN_ESCALATION_CLASSES must be non-empty."""
        import notifier as n
        assert len(n._SOVEREIGN_ESCALATION_CLASSES) > 0

    def test_pipeline_stall_is_escalated(self):
        """PIPELINE_STALL must be in escalation classes."""
        import notifier as n
        assert "PIPELINE_STALL" in n._SOVEREIGN_ESCALATION_CLASSES

    def test_agent_silence_is_escalated(self):
        """AGENT_SILENCE must be in escalation classes."""
        import notifier as n
        assert "AGENT_SILENCE" in n._SOVEREIGN_ESCALATION_CLASSES

    def test_no_sessions_send_calls_in_codebase(self):
        """SENTINEL source files must not contain sessions_send() calls.
        Mentions in docstrings/comments explaining why NOT to use it are allowed."""
        import re
        # Match actual calls, not comment/docstring mentions
        call_pattern = re.compile(r"sessions_send\s*\(")
        sentinel_dir = os.path.join(os.path.dirname(__file__), "..")
        py_files = [
            f for f in os.listdir(sentinel_dir)
            if f.endswith(".py") and f != "__init__.py"
        ]
        for fname in py_files:
            fpath = os.path.join(sentinel_dir, fname)
            with open(fpath) as f:
                content = f.read()
            matches = call_pattern.findall(content)
            assert not matches, \
                f"sessions_send() call found in pipeline-sentinel/{fname} — use message bus instead"
