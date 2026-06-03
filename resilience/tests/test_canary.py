"""Tests for canary_protocol.py"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from canary_protocol import CanaryProtocol, CanaryResult, CanaryAssessment


async def _passing_trade(**kwargs):
    return {
        "order_confirmed": True,
        "db_record": True,
        "telegram_sent": True,
        "monitor_active": True,
    }


async def _failing_trade(**kwargs):
    return {
        "order_confirmed": False,
        "db_record": False,
        "telegram_sent": False,
        "monitor_active": False,
    }


async def _raising_trade(**kwargs):
    raise RuntimeError("Simulated trade failure")


def _make_protocol(execute_fn=None, service_name="NEXUS_ALPHA"):
    alerts = []
    async def alert(msg):
        alerts.append(msg)
    protocol = CanaryProtocol(
        service_name=service_name,
        execute_trade_fn=execute_fn or _passing_trade,
        telegram_alert_fn=alert,
        db_path="/tmp/test_canary.db",
    )
    return protocol, alerts


def test_canary_tickers_nexus_alpha():
    protocol, _ = _make_protocol(service_name="NEXUS_ALPHA")
    assert "SPY" in protocol.canary_tickers
    assert "AAPL" in protocol.canary_tickers


def test_canary_tickers_nexus_prime():
    protocol, _ = _make_protocol(service_name="NEXUS_PRIME")
    assert "SPY" in protocol.canary_tickers
    assert "QQQ" in protocol.canary_tickers


def test_assess_results_all_pass():
    protocol, _ = _make_protocol()
    results = [
        CanaryResult("c1", "NEXUS_ALPHA", "SPY", True, None, None, 100, True, True, True, True, True),
        CanaryResult("c2", "NEXUS_ALPHA", "AAPL", True, None, None, 100, True, True, True, True, True),
    ]
    assessment = protocol._assess_results(results)
    assert assessment.all_passed is True
    assert assessment.full_size_approved is True
    assert assessment.passed_count == 2
    assert assessment.failed_count == 0


def test_assess_results_majority_fail():
    protocol, _ = _make_protocol()
    results = [
        CanaryResult("c1", "NEXUS_ALPHA", "SPY", False, "criteria", "Failed", 100, False, False, False, False, False),
        CanaryResult("c2", "NEXUS_ALPHA", "AAPL", False, "criteria", "Failed", 100, False, False, False, False, False),
    ]
    assessment = protocol._assess_results(results)
    assert assessment.all_passed is False
    assert assessment.full_size_approved is False
    assert assessment.failed_count == 2
    assert "HALTED" in assessment.recommendation or "FAILED" in assessment.recommendation


def test_run_all_pass():
    protocol, alerts = _make_protocol(_passing_trade)
    assessment = asyncio.get_event_loop().run_until_complete(protocol.run())
    assert assessment.all_passed is True
    assert assessment.full_size_approved is True
    assert len(alerts) == 0  # No alerts when passing


def test_run_with_exception_in_trade():
    protocol, alerts = _make_protocol(_raising_trade)
    # Override max retries to speed up test
    protocol.MAX_RETRY_ATTEMPTS = 1
    assessment = asyncio.get_event_loop().run_until_complete(protocol.run())
    assert assessment.all_passed is False
    assert len(alerts) > 0  # Should alert Ahmed
