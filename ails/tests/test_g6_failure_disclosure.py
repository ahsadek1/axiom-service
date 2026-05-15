"""
test_g6_failure_disclosure.py — G6 AILS failure disclosure contract tests.

Verifies that every AILS failure mode returns a structured response
with the required fields (status, error_type, severity, recoverable, etc.)
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from failure import error_response, ERROR_TYPES


class TestErrorResponseShape:
    """Verify the required fields are present on all failure responses."""

    REQUIRED_FIELDS = {
        "status", "error_type", "ticker", "endpoint",
        "reason", "severity", "recoverable", "fallback_used",
        "fallback_source", "data",
    }

    def test_all_required_fields_present(self):
        resp = error_response(
            "TICKER_NOT_IN_BACKTEST",
            "/context/AAPL",
            "Ticker not found in backtest",
            ticker="AAPL",
        )
        assert set(resp.keys()) == self.REQUIRED_FIELDS

    def test_status_is_always_error(self):
        for et in ERROR_TYPES:
            resp = error_response(et, "/test", "test")
            assert resp["status"] == "error", f"status not 'error' for {et}"

    def test_data_is_always_none(self):
        for et in ERROR_TYPES:
            resp = error_response(et, "/test", "test")
            assert resp["data"] is None, f"data not None for {et}"

    def test_reason_truncated_to_200_chars(self):
        long_reason = "x" * 500
        resp = error_response("UNEXPECTED_ERROR", "/test", long_reason)
        assert len(resp["reason"]) <= 200

    def test_ticker_nullable(self):
        resp = error_response("AILS_DB_UNAVAILABLE", "/health", "DB down")
        assert resp["ticker"] is None

    def test_ticker_set_when_provided(self):
        resp = error_response("TICKER_NOT_IN_BACKTEST", "/context/TSLA", "missing", ticker="TSLA")
        assert resp["ticker"] == "TSLA"

    def test_fallback_used_false_by_default(self):
        resp = error_response("PATTERN_SEARCH_FAIL", "/similar/AAPL", "no results", ticker="AAPL")
        assert resp["fallback_used"] is False
        assert resp["fallback_source"] is None


class TestErrorTypeSematics:
    """Verify severity and recoverability for key error types."""

    def test_ticker_not_in_backtest_is_missing_data_not_recoverable(self):
        resp = error_response("TICKER_NOT_IN_BACKTEST", "/context/X", "missing", ticker="X")
        assert resp["severity"] == "MISSING_DATA"
        assert resp["recoverable"] is False

    def test_backtest_db_unavailable_is_critical_and_recoverable(self):
        resp = error_response("BACKTEST_DB_UNAVAILABLE", "/context/X", "locked")
        assert resp["severity"] == "CRITICAL"
        assert resp["recoverable"] is True

    def test_bayesian_update_error_is_degraded(self):
        resp = error_response("BAYESIAN_UPDATE_ERROR", "/outcome", "math error")
        assert resp["severity"] == "DEGRADED"
        assert resp["recoverable"] is True

    def test_live_db_write_fail_is_critical_recoverable(self):
        resp = error_response("LIVE_DB_WRITE_FAIL", "/outcome", "write failed")
        assert resp["severity"] == "CRITICAL"
        assert resp["recoverable"] is True

    def test_agent_calibration_missing_is_missing_data(self):
        resp = error_response("AGENT_CALIBRATION_MISSING", "/calibration/cipher", "no rows")
        assert resp["severity"] == "MISSING_DATA"
        assert resp["recoverable"] is False

    def test_db_connection_none_is_critical_recoverable(self):
        resp = error_response("DB_CONNECTION_NONE", "/context/X", "conn is None", ticker="X")
        assert resp["severity"] == "CRITICAL"
        assert resp["recoverable"] is True

    def test_unknown_error_type_falls_back_to_unexpected_error(self):
        resp = error_response("SOME_MADE_UP_TYPE", "/test", "unknown")
        # Falls back to UNEXPECTED_ERROR metadata
        assert resp["severity"] == "CRITICAL"
        assert resp["recoverable"] is True
        assert resp["error_type"] == "SOME_MADE_UP_TYPE"  # preserves caller's type

    def test_pattern_search_fail_not_recoverable(self):
        resp = error_response("PATTERN_SEARCH_FAIL", "/similar/X", "no results", ticker="X")
        assert resp["severity"] == "MISSING_DATA"
        assert resp["recoverable"] is False


class TestAllRegisteredErrorTypes:
    """Every registered error type must produce a valid response."""

    def test_all_error_types_produce_valid_response(self):
        for et in ERROR_TYPES:
            resp = error_response(et, f"/test/{et}", f"Testing {et}")
            assert resp["status"] == "error"
            assert resp["severity"] in ("CRITICAL", "DEGRADED", "MISSING_DATA", "TIMEOUT")
            assert isinstance(resp["recoverable"], bool)
