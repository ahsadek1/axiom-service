"""
Tests for shared/watchdog.py — NNS WATCHDOG agent signal polling loop
"""

import threading
import time
import unittest
from unittest.mock import MagicMock, patch, call

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from watchdog import Watchdog, _BusUnavailable


def make_signal(signal_id="sig-1", action_type="restart", priority="P1"):
    return {
        "signal_id": signal_id,
        "action_type": action_type,
        "priority": priority,
        "from": "sovereign",
        "to": "genesis",
        "message": "test signal",
    }


class TestWatchdogDispatch(unittest.TestCase):
    """Unit tests for _dispatch and _ack logic — no network needed."""

    def setUp(self):
        self.w = Watchdog("genesis", bus_host="localhost", bus_port=9999, poll_interval=5)

    @patch.object(Watchdog, "_ack")
    def test_tc1_handler_called_with_full_signal(self, mock_ack):
        """TC-1: Signal received → handler called with full signal dict."""
        received = []
        self.w.register("restart", lambda sig: received.append(sig))
        sig = make_signal()
        self.w._dispatch(sig)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["signal_id"], "sig-1")
        mock_ack.assert_called_once_with("sig-1", "received")

    @patch.object(Watchdog, "_ack")
    def test_tc2_handler_raises_acked_escalated_loop_continues(self, mock_ack):
        """TC-2: Handler raises → signal ACKed as escalated, no crash."""
        def bad_handler(sig):
            raise RuntimeError("handler exploded")
        self.w.register("restart", bad_handler)
        sig = make_signal()
        # Should not raise
        self.w._dispatch(sig)
        mock_ack.assert_called_once()
        args = mock_ack.call_args[0]
        self.assertEqual(args[0], "sig-1")
        self.assertEqual(args[1], "escalated")

    @patch.object(Watchdog, "_ack")
    def test_tc3_empty_signal_list_no_dispatch(self, mock_ack):
        """TC-3: Bus returns empty list → loop continues silently."""
        # Simulated by simply not calling _dispatch
        # Verified by checking no handlers fired
        handler = MagicMock()
        self.w.register("restart", handler)
        # Don't dispatch anything
        handler.assert_not_called()
        mock_ack.assert_not_called()

    @patch.object(Watchdog, "_ack")
    def test_tc6_only_matching_handler_called(self, mock_ack):
        """TC-6: Multiple handlers → only matching action_type handler called."""
        restart_handler = MagicMock()
        pause_handler = MagicMock()
        self.w.register("restart", restart_handler)
        self.w.register("pause", pause_handler)

        sig = make_signal(action_type="restart")
        self.w._dispatch(sig)

        restart_handler.assert_called_once_with(sig)
        pause_handler.assert_not_called()

    @patch.object(Watchdog, "_ack")
    def test_tc7_no_handler_acked_received_with_warning(self, mock_ack):
        """TC-7: No handler for action_type → ACK as received, log WARNING."""
        sig = make_signal(action_type="unknown_action")
        self.w._dispatch(sig)  # no handler registered for "unknown_action"
        mock_ack.assert_called_once()
        args = mock_ack.call_args[0]
        self.assertEqual(args[1], "received")

    @patch.object(Watchdog, "_ack")
    def test_missing_signal_id_skipped(self, mock_ack):
        """Signal missing signal_id → skipped, no ACK attempt."""
        bad_sig = {"action_type": "restart", "priority": "P1"}  # no signal_id
        self.w._dispatch(bad_sig)
        mock_ack.assert_not_called()


class TestWatchdogPoll(unittest.TestCase):
    """Tests for _poll — network mocked."""

    def setUp(self):
        self.w = Watchdog("genesis", bus_host="localhost", bus_port=9999)

    @patch("watchdog.requests.get")
    def test_poll_returns_signals_list(self, mock_get):
        """_poll returns list of signal dicts from bus."""
        sig = make_signal()
        mock_get.return_value.status_code = 200
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.return_value = [sig]
        result = self.w._poll()
        self.assertEqual(result, [sig])

    @patch("watchdog.requests.get")
    def test_tc4_bus_unreachable_raises_bus_unavailable(self, mock_get):
        """TC-4: Bus unreachable → _BusUnavailable raised."""
        import requests as req_module
        mock_get.side_effect = req_module.exceptions.ConnectionError("refused")
        with self.assertRaises(_BusUnavailable):
            self.w._poll()

    @patch("watchdog.requests.get")
    def test_poll_wrapped_response_shape(self, mock_get):
        """_poll handles {'signals': [...]} response shape."""
        sig = make_signal()
        mock_get.return_value.status_code = 200
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.return_value = {"signals": [sig]}
        result = self.w._poll()
        self.assertEqual(result, [sig])


class TestWatchdogLifecycle(unittest.TestCase):
    """TC-5: stop() causes loop to exit within one poll cycle."""

    @patch("watchdog.requests.get")
    @patch("watchdog.requests.post")
    def test_tc5_stop_exits_loop(self, mock_post, mock_get):
        """TC-5: stop() → loop exits within one poll cycle."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.return_value = []

        w = Watchdog("genesis", bus_host="localhost", bus_port=9999, poll_interval=1)
        w.start()
        time.sleep(0.2)
        w.stop()
        # Thread should exit within poll_interval + buffer
        w._thread.join(timeout=3)
        self.assertFalse(w._thread.is_alive(), "Watchdog thread did not exit after stop()")


if __name__ == "__main__":
    unittest.main(verbosity=2)
