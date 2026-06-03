"""
tests/test_gap001_auto_heal.py — GAP-001 Auto-Heal Execution Recovery Tests

Tests all 8 required acceptance criteria from the spec.
"""

import asyncio
import threading
import unittest
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution_healer import (
    ExecutionErrorClass,
    HealResult,
    auto_heal_execution,
    classify_error,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def run(coro):
    """Run a coroutine synchronously for testing."""
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeAlpacaError(Exception):
    def __init__(self, status_code: int, body: str = "error"):
        super().__init__(body)
        self.status_code = status_code


class _FakeTimeout(Exception):
    """Simulates a requests.exceptions.Timeout."""
    pass


def make_alpaca_error(status_code: int, body: str = "error") -> Exception:
    """Create a mock AlpacaError with a given status code."""
    return _FakeAlpacaError(status_code, body)


def make_state():
    """Create a fresh app_state and state_lock."""
    state = {"execution_paused": True, "first_exec_failed": True}
    lock = threading.Lock()
    skipped = set()
    return state, lock, skipped


def make_heal_context(exc, ticker="AAPL"):
    state, lock, skipped = make_state()
    return dict(
        exc=exc,
        ticker=ticker,
        app_state=state,
        state_lock=lock,
        api_key="test_key",
        api_secret="test_secret",
        skipped_tickers=skipped,
    )


# ── Test 1: AUTH_FAILURE escalates, never resumes ────────────────────────────

class TestAuthFailureEscalatesNotResumes(unittest.TestCase):
    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    @patch("execution_healer._probe_alpaca", return_value=(False, None))
    def test_401_escalates_not_resumes(self, mock_probe, mock_sov, mock_ahmed):
        exc = make_alpaca_error(401, "unauthorized")
        ctx = make_heal_context(exc)

        result = run(auto_heal_execution(**ctx))

        self.assertFalse(result.resolved)
        self.assertFalse(result.resume_approved)
        self.assertTrue(result.escalate_to_ahmed)
        self.assertTrue(result.escalate_to_sovereign)
        self.assertEqual(result.error_class, ExecutionErrorClass.AUTH_FAILURE)
        # execution must remain paused
        self.assertTrue(ctx["app_state"]["execution_paused"])
        mock_ahmed.assert_called_once()
        mock_sov.assert_called_once()

    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    @patch("execution_healer._probe_alpaca", return_value=(True, None))
    def test_403_no_account_keyword_is_auth_failure(self, mock_probe, mock_sov, mock_ahmed):
        exc = make_alpaca_error(403, "forbidden")
        ctx = make_heal_context(exc)

        result = run(auto_heal_execution(**ctx))

        self.assertFalse(result.resolved)
        self.assertEqual(result.error_class, ExecutionErrorClass.AUTH_FAILURE)
        mock_ahmed.assert_called_once()


# ── Test 2: RATE_LIMITED waits and resumes ────────────────────────────────────

class TestRateLimitWaitsAndResumes(unittest.TestCase):
    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    @patch("asyncio.sleep", return_value=None)  # don't actually wait
    def test_429_waits_retry_after_and_resumes(self, mock_sleep, mock_sov, mock_ahmed):
        exc = make_alpaca_error(429, "too many requests")
        exc.retry_after = "30"
        ctx = make_heal_context(exc)

        result = run(auto_heal_execution(**ctx))

        self.assertTrue(result.resolved)
        self.assertTrue(result.resume_approved)
        self.assertFalse(result.escalate_to_ahmed)
        self.assertEqual(result.error_class, ExecutionErrorClass.RATE_LIMITED)
        # state must be cleared
        self.assertFalse(ctx["app_state"]["execution_paused"])
        self.assertFalse(ctx["app_state"]["first_exec_failed"])
        mock_sleep.assert_called_once_with(30)
        mock_ahmed.assert_not_called()
        mock_sov.assert_called_once()

    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    @patch("asyncio.sleep", return_value=None)
    def test_429_defaults_to_60s_if_no_retry_after(self, mock_sleep, mock_sov, mock_ahmed):
        exc = make_alpaca_error(429, "too many requests")
        ctx = make_heal_context(exc)

        result = run(auto_heal_execution(**ctx))

        self.assertTrue(result.resolved)
        mock_sleep.assert_called_once_with(60)


# ── Test 3: INVALID_PAYLOAD skips ticker, does not pause execution ────────────

class TestInvalidPayloadSkipsTickerNotPauses(unittest.TestCase):
    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    def test_422_skips_ticker_execution_continues(self, mock_sov, mock_ahmed):
        exc = make_alpaca_error(422, "unprocessable entity")
        state = {"execution_paused": False, "first_exec_failed": False}
        lock = threading.Lock()
        skipped = set()
        ctx = dict(
            exc=exc, ticker="NVDA",
            app_state=state, state_lock=lock,
            api_key="k", api_secret="s",
            skipped_tickers=skipped,
        )

        result = run(auto_heal_execution(**ctx))

        self.assertTrue(result.resolved)
        self.assertTrue(result.resume_approved)
        self.assertFalse(result.escalate_to_ahmed)
        self.assertEqual(result.error_class, ExecutionErrorClass.INVALID_PAYLOAD)
        # execution must NOT be paused
        self.assertFalse(state["execution_paused"])
        # ticker must be in skip list
        self.assertIn("NVDA", skipped)
        mock_ahmed.assert_not_called()
        mock_sov.assert_called_once()


# ── Test 4: BROKER_UNAVAILABLE retries 3x then escalates ─────────────────────

class TestBrokerUnavailableRetries3xThenEscalates(unittest.TestCase):
    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    @patch("execution_healer._probe_alpaca", return_value=(False, None))
    @patch("asyncio.sleep", return_value=None)
    def test_5xx_retries_3_times_then_escalates(self, mock_sleep, mock_probe, mock_sov, mock_ahmed):
        exc = make_alpaca_error(503, "service unavailable")
        ctx = make_heal_context(exc)

        result = run(auto_heal_execution(**ctx))

        self.assertFalse(result.resolved)
        self.assertFalse(result.resume_approved)
        self.assertTrue(result.escalate_to_ahmed)
        self.assertEqual(result.error_class, ExecutionErrorClass.BROKER_UNAVAILABLE)
        self.assertEqual(mock_probe.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 3)
        mock_ahmed.assert_called_once()

    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    @patch("execution_healer._probe_alpaca", side_effect=[(False, None), (True, "ACTIVE")])
    @patch("asyncio.sleep", return_value=None)
    def test_5xx_recovers_on_retry_2(self, mock_sleep, mock_probe, mock_sov, mock_ahmed):
        exc = make_alpaca_error(503, "service unavailable")
        ctx = make_heal_context(exc)

        result = run(auto_heal_execution(**ctx))

        self.assertTrue(result.resolved)
        self.assertFalse(ctx["app_state"]["execution_paused"])
        mock_ahmed.assert_not_called()


# ── Test 5: TRANSIENT_NETWORK retries 2x then resumes ────────────────────────

class TestTransientNetworkRetries2xThenResumes(unittest.TestCase):
    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    @patch("execution_healer._probe_alpaca", side_effect=[(False, None), (True, "ACTIVE")])
    @patch("asyncio.sleep", return_value=None)
    def test_network_recovers_on_retry_2(self, mock_sleep, mock_probe, mock_sov, mock_ahmed):
        exc = _FakeTimeout("Connection timed out")
        ctx = make_heal_context(exc)

        result = run(auto_heal_execution(**ctx))

        self.assertTrue(result.resolved)
        self.assertFalse(ctx["app_state"]["execution_paused"])
        mock_ahmed.assert_not_called()

    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    @patch("execution_healer._probe_alpaca", return_value=(False, None))
    @patch("asyncio.sleep", return_value=None)
    def test_network_exhausted_escalates(self, mock_sleep, mock_probe, mock_sov, mock_ahmed):
        exc = _FakeTimeout("Connection timed out")
        ctx = make_heal_context(exc)

        result = run(auto_heal_execution(**ctx))

        self.assertFalse(result.resolved)
        mock_ahmed.assert_called_once()
        self.assertEqual(mock_probe.call_count, 2)


# ── Test 6: UNKNOWN error escalates immediately ───────────────────────────────

class TestUnknownErrorEscalatesImmediately(unittest.TestCase):
    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    def test_unknown_error_escalates_no_retry(self, mock_sov, mock_ahmed):
        exc = Exception("Some unexpected error with no status code")
        ctx = make_heal_context(exc)

        result = run(auto_heal_execution(**ctx))

        self.assertFalse(result.resolved)
        self.assertTrue(result.escalate_to_ahmed)
        self.assertEqual(result.error_class, ExecutionErrorClass.UNKNOWN)
        mock_ahmed.assert_called_once()
        mock_sov.assert_called_once()


# ── Test 7: SOVEREIGN notified on every heal attempt ─────────────────────────

class TestSovereignNotifiedOnEveryHealAttempt(unittest.TestCase):
    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    @patch("execution_healer._probe_alpaca", return_value=(False, None))
    def test_sovereign_notified_on_auth_failure(self, mock_probe, mock_sov, mock_ahmed):
        exc = make_alpaca_error(401)
        ctx = make_heal_context(exc)
        run(auto_heal_execution(**ctx))
        mock_sov.assert_called_once()

    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    @patch("asyncio.sleep", return_value=None)
    def test_sovereign_notified_on_rate_limit_success(self, mock_sleep, mock_sov, mock_ahmed):
        exc = make_alpaca_error(429)
        ctx = make_heal_context(exc)
        run(auto_heal_execution(**ctx))
        mock_sov.assert_called_once()

    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    def test_sovereign_notified_on_422_skip(self, mock_sov, mock_ahmed):
        exc = make_alpaca_error(422)
        state = {"execution_paused": False, "first_exec_failed": False}
        ctx = dict(exc=exc, ticker="AAPL", app_state=state,
                   state_lock=threading.Lock(), api_key="k", api_secret="s",
                   skipped_tickers=set())
        run(auto_heal_execution(**ctx))
        mock_sov.assert_called_once()


# ── Test 8: Ahmed only notified on escalation ────────────────────────────────

class TestAhmedOnlyNotifiedOnEscalation(unittest.TestCase):
    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    @patch("asyncio.sleep", return_value=None)
    def test_ahmed_not_paged_on_rate_limit_recovery(self, mock_sleep, mock_sov, mock_ahmed):
        exc = make_alpaca_error(429)
        ctx = make_heal_context(exc)
        run(auto_heal_execution(**ctx))
        mock_ahmed.assert_not_called()

    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    def test_ahmed_not_paged_on_422_skip(self, mock_sov, mock_ahmed):
        exc = make_alpaca_error(422)
        state = {"execution_paused": False, "first_exec_failed": False}
        ctx = dict(exc=exc, ticker="TSLA", app_state=state,
                   state_lock=threading.Lock(), api_key="k", api_secret="s",
                   skipped_tickers=set())
        run(auto_heal_execution(**ctx))
        mock_ahmed.assert_not_called()

    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    @patch("execution_healer._probe_alpaca", return_value=(False, None))
    def test_ahmed_paged_on_auth_failure(self, mock_probe, mock_sov, mock_ahmed):
        exc = make_alpaca_error(401)
        ctx = make_heal_context(exc)
        run(auto_heal_execution(**ctx))
        mock_ahmed.assert_called_once()

    @patch("execution_healer._notify_ahmed")
    @patch("execution_healer._notify_sovereign")
    def test_ahmed_paged_on_unknown(self, mock_sov, mock_ahmed):
        exc = Exception("weird error")
        ctx = make_heal_context(exc)
        run(auto_heal_execution(**ctx))
        mock_ahmed.assert_called_once()


# ── Error classification unit tests ──────────────────────────────────────────

class TestClassifyError(unittest.TestCase):
    def test_401_is_auth_failure(self):
        self.assertEqual(classify_error(make_alpaca_error(401)), ExecutionErrorClass.AUTH_FAILURE)

    def test_403_generic_is_auth_failure(self):
        self.assertEqual(classify_error(make_alpaca_error(403, "forbidden")), ExecutionErrorClass.AUTH_FAILURE)

    def test_403_account_is_account_blocked(self):
        self.assertEqual(classify_error(make_alpaca_error(403, "account restricted")), ExecutionErrorClass.ACCOUNT_BLOCKED)

    def test_429_is_rate_limited(self):
        self.assertEqual(classify_error(make_alpaca_error(429)), ExecutionErrorClass.RATE_LIMITED)

    def test_422_is_invalid_payload(self):
        self.assertEqual(classify_error(make_alpaca_error(422)), ExecutionErrorClass.INVALID_PAYLOAD)

    def test_503_is_broker_unavailable(self):
        self.assertEqual(classify_error(make_alpaca_error(503)), ExecutionErrorClass.BROKER_UNAVAILABLE)

    def test_timeout_is_transient_network(self):
        exc = _FakeTimeout("timed out")
        self.assertEqual(classify_error(exc), ExecutionErrorClass.TRANSIENT_NETWORK)

    def test_unknown_error(self):
        self.assertEqual(classify_error(Exception("some error")), ExecutionErrorClass.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
