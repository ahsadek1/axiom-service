"""test_escalator.py — Unit tests for the escalator module."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from escalator import escalate_to_sovereign
from models import AlpacaSnapshot, CycleResult, DiagnosisResult, ServiceSnapshot


def _make_critical_result() -> CycleResult:
    """Build a CycleResult representing a CRITICAL unknown escalation."""
    diag = DiagnosisResult("unknown", "CRITICAL", True, "No known cause.", 1)
    return CycleResult(
        cycle_ts="2026-04-20T14:30:00+00:00",
        market_open=True,
        trades_since_last_cycle=0,
        diagnosis=diag,
        services={
            "alpha_execution": ServiceSnapshot("alpha_execution", "UP", {}),
            "cipher": ServiceSnapshot("cipher", "UP", {}),
            "atlas": ServiceSnapshot("atlas", "UP", {}),
            "sage": ServiceSnapshot("sage", "UP", {}),
        },
        alpaca={
            "v1": AlpacaSnapshot("v1", "UP", buying_power=93792.0),
            "v2": AlpacaSnapshot("v2", "UP", buying_power=100000.0),
        },
        escalated=True,
    )


class TestEscalator(unittest.TestCase):

    def test_escalate_success_returns_true(self):
        """escalate_to_sovereign returns True when bus returns HTTP 200."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("escalator.requests.post", return_value=mock_resp):
            result = escalate_to_sovereign(_make_critical_result())
        self.assertTrue(result)

    def test_escalate_returns_false_on_bus_error(self):
        """escalate_to_sovereign returns False when bus raises an exception."""
        with patch("escalator.requests.post", side_effect=Exception("bus down")):
            result = escalate_to_sovereign(_make_critical_result())
        self.assertFalse(result)

    def test_escalate_returns_false_on_non_2xx(self):
        """escalate_to_sovereign returns False on HTTP 500 from bus."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("escalator.requests.post", return_value=mock_resp):
            result = escalate_to_sovereign(_make_critical_result())
        self.assertFalse(result)

    def test_escalate_message_contains_diagnosis(self):
        """Escalation message includes the diagnosis and severity."""
        captured = {}

        def _capture_post(url, json=None, timeout=None):
            captured["payload"] = json
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            return mock_resp

        with patch("escalator.requests.post", side_effect=_capture_post):
            escalate_to_sovereign(_make_critical_result())

        msg = captured["payload"]["message"]
        self.assertIn("unknown", msg)
        self.assertIn("CRITICAL", msg)
        self.assertIn("Action required: YES", msg)


if __name__ == "__main__":
    unittest.main()
