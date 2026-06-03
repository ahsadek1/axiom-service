"""test_chronicler.py — Unit tests for the chronicler module."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from chronicler import write_to_chronicle
from models import AlpacaSnapshot, CycleResult, DiagnosisResult, ServiceSnapshot


def _make_result(diagnosis: str = "pipeline_healthy") -> CycleResult:
    """Build a minimal CycleResult for testing."""
    diag = DiagnosisResult(diagnosis, "INFO", False, "Test details.", 0)
    return CycleResult(
        cycle_ts="2026-04-20T14:30:00+00:00",
        market_open=True,
        trades_since_last_cycle=1,
        diagnosis=diag,
        services={
            "alpha_execution": ServiceSnapshot(
                "alpha_execution", "UP", {"trades_today": 1}
            )
        },
        alpaca={"v1": AlpacaSnapshot("v1", "UP", buying_power=93792.0)},
    )


class TestChronicler(unittest.TestCase):

    def test_chronicle_success_returns_true(self):
        """write_to_chronicle returns True on HTTP 200."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("chronicler.requests.post", return_value=mock_resp):
            result = write_to_chronicle(_make_result())
        self.assertTrue(result)

    def test_chronicle_fallback_on_500(self):
        """write_to_chronicle returns False and writes fallback file on HTTP 500."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        tmp = tempfile.mktemp(suffix=".jsonl")
        with (
            patch("chronicler.FALLBACK_LOG", tmp),
            patch("chronicler.requests.post", return_value=mock_resp),
            patch("chronicler.time.sleep"),  # skip retry delay in tests
        ):
            result = write_to_chronicle(_make_result())
        self.assertFalse(result)
        self.assertTrue(os.path.exists(tmp))
        # Verify fallback file is valid JSON
        with open(tmp) as f:
            data = json.loads(f.readline())
        self.assertEqual(data["source"], "outcome-monitor")
        os.unlink(tmp)

    def test_chronicle_fallback_on_exception(self):
        """write_to_chronicle returns False and writes fallback when POST raises."""
        tmp = tempfile.mktemp(suffix=".jsonl")
        with (
            patch("chronicler.FALLBACK_LOG", tmp),
            patch("chronicler.requests.post", side_effect=Exception("network error")),
            patch("chronicler.time.sleep"),
        ):
            result = write_to_chronicle(_make_result())
        self.assertFalse(result)
        self.assertTrue(os.path.exists(tmp))
        os.unlink(tmp)


if __name__ == "__main__":
    unittest.main()
