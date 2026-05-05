"""
test_g13_orats_retry.py — G13: ORATS Retry Logic + Circuit Breaker Tests

Tests:
  T1: Circuit open → _orats_get returns {"data": [], "error": "circuit_open"} immediately
  T2: 200 response → data returned, _consecutive_failures reset to 0
  T3: 429 rate limit → retries with backoff (no fall-through to error logger)
  T4: ConnectionError → retries (not immediate break)
  T5: All retries exhausted → _consecutive_failures incremented
  T6: _consecutive_failures >= CIRCUIT_BREAK_THRESHOLD → circuit opens
  T7: _check_circuit_breaker with count < threshold → circuit stays closed
  T8: Circuit open then time passes → circuit closed again (requests allowed)
  T9: 403 response → immediate return with error, no retry, circuit not incremented
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import oracle.clients.orats_client as _orats_mod
from oracle.clients.orats_client import (
    CIRCUIT_BREAK_THRESHOLD,
    CIRCUIT_RESET_SEC,
    _check_circuit_breaker,
    _orats_get,
)


def _reset_circuit():
    """Reset circuit breaker state between tests."""
    _orats_mod._circuit_open_until = 0.0
    _orats_mod._consecutive_failures = 0


def _resp(status: int, data: dict = None) -> MagicMock:
    """Build a mock requests.Response."""
    m = MagicMock()
    m.status_code = status
    m.json.return_value = data or {}
    m.text = str(data or {})[:200]
    return m


# ─── T1: Circuit open → returns circuit_open immediately ─────────────────────

def test_t1_circuit_open_returns_immediately():
    """T1: When circuit is open, _orats_get returns error without calling requests.get."""
    _reset_circuit()
    # Force circuit open
    _orats_mod._circuit_open_until = time.time() + 3600

    with patch("requests.get") as mock_get:
        result = _orats_get("/datav2/summaries", {"ticker": "AAPL"})

    assert result["error"] == "circuit_open"
    assert result["data"] == []
    mock_get.assert_not_called()
    _reset_circuit()


# ─── T2: 200 response → data returned + consecutive failures reset ────────────

def test_t2_success_resets_consecutive_failures():
    """T2: Successful 200 response resets _consecutive_failures to 0."""
    _reset_circuit()
    _orats_mod._consecutive_failures = 3  # pre-seed some failures

    with patch("requests.get", return_value=_resp(200, {"data": [{"ticker": "AAPL"}]})):
        result = _orats_get("/datav2/summaries", {"ticker": "AAPL"})

    assert result.get("data") is not None
    assert _orats_mod._consecutive_failures == 0
    _reset_circuit()


# ─── T3: 429 → retries with backoff, no fall-through to error log ─────────────

def test_t3_rate_limit_retries_not_falls_through():
    """T3: 429 causes retry (not fall-through to error logger)."""
    _reset_circuit()

    call_count = [0]
    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] <= 2:
            return _resp(429)
        return _resp(200, {"data": [{"ticker": "SPY"}]})

    with patch("requests.get", side_effect=side_effect):
        with patch("time.sleep"):  # skip backoff delays
            result = _orats_get("/datav2/cores", {"ticker": "SPY"})

    # Should eventually succeed on 3rd attempt
    assert call_count[0] == 3
    assert result.get("data") is not None
    _reset_circuit()


# ─── T4: ConnectionError → retries (not immediate break) ─────────────────────

def test_t4_connection_error_retries():
    """T4: ConnectionError triggers retry instead of immediate break."""
    _reset_circuit()
    import requests as _req

    call_count = [0]
    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] <= 2:
            raise _req.exceptions.ConnectionError("Network unreachable")
        return _resp(200, {"data": [{"ticker": "NVDA"}]})

    with patch("requests.get", side_effect=side_effect):
        with patch("time.sleep"):
            result = _orats_get("/datav2/strikes", {"ticker": "NVDA"})

    # ConnectionError retried, eventually succeeded on 3rd call
    assert call_count[0] == 3
    assert result.get("data") is not None
    _reset_circuit()


# ─── T5: All retries exhausted → _consecutive_failures incremented ────────────

def test_t5_all_retries_exhausted_increments_failures():
    """T5: All retry attempts fail → _consecutive_failures incremented by 1."""
    _reset_circuit()
    initial_failures = _orats_mod._consecutive_failures

    with patch("requests.get", return_value=_resp(500)):
        with patch("time.sleep"):
            _orats_get("/datav2/strikes", {"ticker": "FAIL"})

    assert _orats_mod._consecutive_failures == initial_failures + 1
    _reset_circuit()


# ─── T6: Failures >= CIRCUIT_BREAK_THRESHOLD → circuit opens ─────────────────

def test_t6_failures_at_threshold_open_circuit():
    """T6: Reaching CIRCUIT_BREAK_THRESHOLD consecutive failures opens the circuit."""
    _reset_circuit()
    _orats_mod._consecutive_failures = CIRCUIT_BREAK_THRESHOLD - 1

    with patch("requests.get", return_value=_resp(500)):
        with patch("time.sleep"):
            _orats_get("/datav2/summaries", {"ticker": "CB"})

    # Circuit should now be open
    assert _orats_mod._circuit_open_until > time.time()
    _reset_circuit()


# ─── T7: _check_circuit_breaker count < threshold → stays closed ─────────────

def test_t7_below_threshold_circuit_stays_closed():
    """T7: _consecutive_failures < threshold → circuit breaker does NOT open."""
    _reset_circuit()
    _orats_mod._consecutive_failures = CIRCUIT_BREAK_THRESHOLD - 1
    _check_circuit_breaker()
    # Circuit should still be closed (open_until == 0)
    assert _orats_mod._circuit_open_until == 0.0
    _reset_circuit()


# ─── T8: Circuit open, then time passes → circuit closes ─────────────────────

def test_t8_circuit_closes_after_reset_time():
    """T8: After CIRCUIT_RESET_SEC, the circuit re-closes and requests are allowed."""
    _reset_circuit()
    # Set circuit to have "opened" just past CIRCUIT_RESET_SEC ago
    _orats_mod._circuit_open_until = time.time() - 1.0  # expired 1s ago

    with patch("requests.get", return_value=_resp(200, {"data": [{"ticker": "AAPL"}]})):
        result = _orats_get("/datav2/summaries", {"ticker": "AAPL"})

    # Circuit was expired, request should have gone through
    assert result.get("data") is not None
    assert result.get("error") != "circuit_open"
    _reset_circuit()


# ─── T9: 403 → immediate return, circuit not incremented ─────────────────────

def test_t9_403_returns_immediately_no_circuit_increment():
    """T9: 403 Forbidden returns immediately without retry and without incrementing circuit."""
    _reset_circuit()
    initial_failures = _orats_mod._consecutive_failures

    with patch("requests.get", return_value=_resp(403)):
        result = _orats_get("/datav2/summaries", {"ticker": "PLAN"})

    assert result.get("error") == "403_not_on_plan"
    # 403 is not a failure — circuit should NOT be incremented
    assert _orats_mod._consecutive_failures == initial_failures
    _reset_circuit()
