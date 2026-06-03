"""
test_execution_recovery.py — End-to-end tests for OMNI autonomous execution recovery.

GAP-007 (2026-04-27): Verify every error class is classified correctly, the right
autonomous action fires, escalation reaches SOVEREIGN, and Ahmed is paged only when
all retries are exhausted.

Run:  cd /Users/ahmedsadek/nexus/omni && .venv/bin/pytest tests/test_execution_recovery.py -v
"""

import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from execution_recovery import (
    ExecFailClass,
    RecoveryResult,
    auto_recover_execution,
    classify_exec_failure,
)


# ── Classifier tests ──────────────────────────────────────────────────────────

class TestClassifyExecFailure:
    def test_daily_cap(self):
        assert classify_exec_failure("429:DAILY_CAP:Max new positions today reached (8/5)") \
               == ExecFailClass.DAILY_CAP

    def test_ticker_duplicate(self):
        assert classify_exec_failure("429:TICKER_DUPLICATE:GE already has an open position") \
               == ExecFailClass.TICKER_DUPLICATE

    def test_concurrent_cap(self):
        assert classify_exec_failure("429:CONCURRENT_CAP:Max concurrent positions reached (10/10)") \
               == ExecFailClass.CONCURRENT_CAP

    def test_timeout(self):
        assert classify_exec_failure("timeout") == ExecFailClass.TIMEOUT
        assert classify_exec_failure("Request timed out after 150s") == ExecFailClass.TIMEOUT

    def test_broker_5xx(self):
        assert classify_exec_failure("HTTP 503") == ExecFailClass.BROKER_5XX
        assert classify_exec_failure("HTTP 500") == ExecFailClass.BROKER_5XX
        assert classify_exec_failure("HTTP 502") == ExecFailClass.BROKER_5XX

    def test_auth_failure(self):
        assert classify_exec_failure("HTTP 401") == ExecFailClass.AUTH_FAILURE
        assert classify_exec_failure("HTTP 403") == ExecFailClass.AUTH_FAILURE

    def test_network(self):
        assert classify_exec_failure("error: connection refused") == ExecFailClass.NETWORK
        assert classify_exec_failure("HTTPSConnectionPool(host='...') connection error") \
               == ExecFailClass.NETWORK

    def test_unknown(self):
        assert classify_exec_failure("some random thing") == ExecFailClass.UNKNOWN
        assert classify_exec_failure(None) == ExecFailClass.UNKNOWN
        assert classify_exec_failure("") == ExecFailClass.UNKNOWN


# ── Resolution tests ──────────────────────────────────────────────────────────

class TestTickerDuplicate:
    """Safe skip — no escalation, no retry."""
    def test_safe_skip_no_notify(self):
        route_fn = MagicMock(return_value=(True, "HTTP 200"))
        with patch("execution_recovery._notify_sovereign") as mock_sov, \
             patch("execution_recovery._notify_ahmed") as mock_ahmed:
            result = auto_recover_execution(
                "GE", "prime",
                "429:TICKER_DUPLICATE:GE already has an open position",
                route_fn,
            )
        assert result.skipped is True
        assert result.success is False
        assert result.escalated_ahmed is False
        assert result.escalated_sovereign is False
        route_fn.assert_not_called()   # no retry on duplicate
        mock_ahmed.assert_not_called()


class TestConcurrentCap:
    """Safe skip — no escalation, no retry."""
    def test_safe_skip(self):
        route_fn = MagicMock(return_value=(True, "HTTP 200"))
        result = auto_recover_execution(
            "NVDA", "alpha",
            "429:CONCURRENT_CAP:Max concurrent positions reached (10/10)",
            route_fn,
        )
        assert result.skipped is True
        route_fn.assert_not_called()


class TestDailyCap:
    """Cannot auto-fix — must escalate SOVEREIGN immediately."""
    def test_escalates_sovereign(self):
        route_fn = MagicMock(return_value=(True, "HTTP 200"))
        with patch("execution_recovery._notify_sovereign", return_value=True) as mock_sov, \
             patch("execution_recovery._notify_ahmed") as mock_ahmed:
            result = auto_recover_execution(
                "GE", "prime",
                "429:DAILY_CAP:Max new positions today reached (8/50)",
                route_fn,
            )
        assert result.success is False
        assert result.skipped is False
        assert result.escalated_sovereign is True
        mock_sov.assert_called_once()
        route_fn.assert_not_called()  # can't retry — cap won't clear mid-session

    def test_falls_back_to_ahmed_if_sovereign_unreachable(self):
        route_fn = MagicMock(return_value=(True, "HTTP 200"))
        with patch("execution_recovery._notify_sovereign", return_value=False), \
             patch("execution_recovery._notify_ahmed") as mock_ahmed:
            result = auto_recover_execution(
                "GE", "prime",
                "429:DAILY_CAP:Max new positions today reached (8/50)",
                route_fn,
            )
        assert result.escalated_ahmed is True
        mock_ahmed.assert_called_once()


class TestTimeout:
    """Retry once after 5s — succeed on retry."""
    def test_retry_succeeds(self):
        call_count = {"n": 0}
        def route_fn():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return (False, "timeout")
            return (True, "HTTP 200")

        with patch("execution_recovery.time.sleep"):  # don't actually wait
            with patch("execution_recovery._notify_sovereign", return_value=True) as mock_sov:
                result = auto_recover_execution("NVDA", "alpha", "timeout", route_fn)

        assert result.success is True
        assert result.attempts == 2   # succeeds on attempt 2 (first retry)
        assert call_count["n"] == 2   # route_fn called twice (attempt 1 + attempt 2)
        mock_sov.assert_called_once()

    def test_retry_exhausted_escalates(self):
        route_fn = MagicMock(return_value=(False, "timeout"))
        with patch("execution_recovery.time.sleep"), \
             patch("execution_recovery._notify_sovereign", return_value=True) as mock_sov, \
             patch("execution_recovery._notify_ahmed") as mock_ahmed:
            result = auto_recover_execution("NVDA", "alpha", "timeout", route_fn)

        assert result.success is False
        assert result.escalated_sovereign is True
        # Ahmed is NOT paged for timeout exhaustion — SOVEREIGN handles it
        assert result.escalated_ahmed is False


class TestBroker5xx:
    """Retry 2x with 15s backoff — succeed on retry 2."""
    def test_retry_succeeds_on_second_attempt(self):
        call_count = {"n": 0}
        def route_fn():
            call_count["n"] += 1
            if call_count["n"] < 2:
                return (False, "HTTP 503")
            return (True, "HTTP 200")

        with patch("execution_recovery.time.sleep"), \
             patch("execution_recovery._notify_sovereign", return_value=True):
            result = auto_recover_execution("TSLA", "alpha", "HTTP 503", route_fn)

        assert result.success is True
        assert call_count["n"] == 2

    def test_all_retries_exhausted_escalates_sovereign(self):
        route_fn = MagicMock(return_value=(False, "HTTP 503"))
        with patch("execution_recovery.time.sleep"), \
             patch("execution_recovery._notify_sovereign", return_value=True) as mock_sov:
            result = auto_recover_execution("TSLA", "alpha", "HTTP 503", route_fn)

        assert result.success is False
        assert result.escalated_sovereign is True
        mock_sov.assert_called_once()


class TestAuthFailure:
    """Auth failure: escalate BOTH SOVEREIGN and Ahmed immediately — no retry."""
    def test_escalates_both_immediately(self):
        route_fn = MagicMock(return_value=(True, "HTTP 200"))
        with patch("execution_recovery._notify_sovereign", return_value=True) as mock_sov, \
             patch("execution_recovery._notify_ahmed") as mock_ahmed:
            result = auto_recover_execution("SPY", "alpha", "HTTP 401", route_fn)

        assert result.success is False
        assert result.escalated_sovereign is True
        assert result.escalated_ahmed is True
        route_fn.assert_not_called()   # no retry — can't fix credentials
        mock_sov.assert_called_once()
        mock_ahmed.assert_called_once()


class TestUnknown:
    """Unknown error: escalate SOVEREIGN immediately, Ahmed as fallback."""
    def test_escalates_sovereign_and_ahmed(self):
        route_fn = MagicMock(return_value=(True, "HTTP 200"))
        with patch("execution_recovery._notify_sovereign", return_value=True) as mock_sov, \
             patch("execution_recovery._notify_ahmed") as mock_ahmed:
            result = auto_recover_execution("QQQ", "alpha", "some weird error xyz", route_fn)

        assert result.error_class == ExecFailClass.UNKNOWN
        assert result.escalated_sovereign is True
        route_fn.assert_not_called()


class TestSovereignFallback:
    """If SOVEREIGN unreachable, Ahmed always gets paged."""
    def test_sovereign_down_pages_ahmed_for_unknown(self):
        route_fn = MagicMock(return_value=(False, "error"))
        with patch("execution_recovery._notify_sovereign", return_value=False), \
             patch("execution_recovery._notify_ahmed") as mock_ahmed:
            result = auto_recover_execution("SPY", "alpha", "mystery error", route_fn)

        assert result.escalated_ahmed is True
        mock_ahmed.assert_called_once()


class TestAsyncRecovery:
    """async wrapper fires in background thread and doesn't block caller."""
    def test_fires_in_background(self):
        from execution_recovery import auto_recover_async
        executed = {"done": False}

        def route_fn():
            executed["done"] = True
            return (True, "HTTP 200")

        with patch("execution_recovery._notify_sovereign", return_value=True), \
             patch("execution_recovery.time.sleep"):
            auto_recover_async("GE", "prime", "timeout", route_fn)
            # Should return immediately — background thread does the work
            time.sleep(0.1)

        assert executed["done"] is True


# ── Integration smoke test ────────────────────────────────────────────────────

class TestFullPipeline:
    """Simulate the full execution failure → recovery → SOVEREIGN notification chain."""
    def test_network_error_retries_then_notifies_sovereign(self):
        attempts = {"n": 0}
        sovereign_msgs = []

        def route_fn():
            attempts["n"] += 1
            return (True, "HTTP 200") if attempts["n"] >= 2 else (False, "connection refused")

        def fake_sovereign(result, ticker, system, action, rec_result, exec_resp):
            sovereign_msgs.append({
                "ticker": ticker, "class": rec_result.error_class.value,
                "resolved": rec_result.success,
            })
            return True

        with patch("execution_recovery._notify_sovereign", side_effect=fake_sovereign), \
             patch("execution_recovery.time.sleep"):
            result = auto_recover_execution(
                "AAPL", "alpha", "error: connection refused", route_fn
            )

        assert result.success is True
        assert len(sovereign_msgs) == 1
        assert sovereign_msgs[0]["resolved"] is True
        assert sovereign_msgs[0]["class"] == "network"
