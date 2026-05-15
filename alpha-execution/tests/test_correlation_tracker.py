"""
test_correlation_tracker.py — Unit tests for the Correlation Gate

Required test cases (all 5 mandated by spec):
  1. Correlation 0.87 with existing position → 50% size reduction
  2. Correlation 0.95 → BLOCK decision
  3. No existing positions → APPROVE full size
  4. Correlation < 0.60 → APPROVE full size
  5. Three existing positions — max correlation used for gate decision

Built by GENESIS 2026-05-07.
"""

import math
import os
import sqlite3
import sys
import tempfile
from typing import List
from unittest.mock import MagicMock, patch

import pytest

# Ensure alpha-execution dir is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from correlation_tracker import (
    CorrelationResult,
    _ESTIMATED_CORR,
    _decide,
    compute_correlation,
    evaluate_correlation_gate,
    fetch_return_history,
    init_correlation_schema,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_returns(corr_target: float, n: int = 60) -> tuple[List[float], List[float]]:
    """Generate two synthetic return series with approximately the given Pearson correlation.

    Uses a simple approach: returns_b = corr_target * returns_a + noise.
    The actual computed correlation will be close but not exact due to the
    random component — we verify the gate fires as expected, not the exact value.
    """
    import random
    random.seed(42)
    base = [random.gauss(0, 0.01) for _ in range(n)]
    noise_scale = math.sqrt(1 - corr_target ** 2) if abs(corr_target) < 1.0 else 0.0
    mixed = [corr_target * b + noise_scale * random.gauss(0, 0.01) for b in base]
    return base, mixed


def _db_with_schema() -> str:
    """Create a temp DB with correlation_records schema. Returns path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    init_correlation_schema(tmp.name)
    return tmp.name


# ── Test 1: Correlation 0.87 → 50% size reduction ────────────────────────────

def test_correlation_0_87_gives_50pct_reduction():
    """Correlation 0.87 with existing position → REDUCE at 0.50x (50% cut)."""
    returns_new, returns_existing = _make_returns(0.87)
    actual_corr = abs(compute_correlation(returns_new, returns_existing))
    assert 0.80 <= actual_corr <= 0.94, f"Generated corr {actual_corr:.3f} outside expected range"

    gate, adj = _decide(actual_corr)
    assert gate == "REDUCE", f"Expected REDUCE, got {gate}"
    assert adj == 0.50, f"Expected 0.50x size_adjustment, got {adj}"


# ── Test 2: Correlation 0.95 → BLOCK decision ─────────────────────────────────

def test_correlation_0_95_gives_block():
    """Correlation 0.95 → BLOCK decision at 0.0x."""
    gate, adj = _decide(0.95)
    assert gate == "BLOCK", f"Expected BLOCK at 0.95, got {gate}"
    assert adj == 0.0, f"Expected 0.0x size_adjustment, got {adj}"


def test_correlation_above_0_95_gives_block():
    """Correlation 0.97 → BLOCK decision (above threshold)."""
    gate, adj = _decide(0.97)
    assert gate == "BLOCK"
    assert adj == 0.0


# ── Test 3: No existing positions → APPROVE full size ─────────────────────────

def test_no_open_positions_approves_full_size():
    """No open positions → APPROVE at 1.0x without calling Polygon."""
    db_path = _db_with_schema()
    try:
        with patch("correlation_tracker.fetch_return_history") as mock_fetch:
            result = evaluate_correlation_gate(
                new_ticker            = "AAPL",
                direction             = "bullish",
                open_position_tickers = [],
                db_path               = db_path,
                polygon_api_key       = "fake_key",
                telegram_bot_token    = "",
                telegram_chat_id      = "",
            )
        # Polygon should NOT be called when there are no open positions
        mock_fetch.assert_not_called()
        assert result.gate_decision == "APPROVE"
        assert result.size_adjustment == 1.0
        assert result.max_correlation == 0.0
        assert result.existing_tickers == []
    finally:
        os.unlink(db_path)


# ── Test 4: Correlation < 0.60 → APPROVE full size ───────────────────────────

def test_low_correlation_approves_full_size():
    """Correlation < 0.60 → APPROVE at 1.0x with no size adjustment."""
    returns_new, returns_existing = _make_returns(0.30)
    actual_corr = abs(compute_correlation(returns_new, returns_existing))
    assert actual_corr < 0.60, f"Generated corr {actual_corr:.3f} is not < 0.60"

    gate, adj = _decide(actual_corr)
    assert gate == "APPROVE", f"Expected APPROVE for low corr, got {gate}"
    assert adj == 1.0, f"Expected 1.0x, got {adj}"


def test_full_evaluate_gate_low_correlation():
    """End-to-end gate with low correlation: no Polygon calls, APPROVE 1.0x."""
    db_path = _db_with_schema()
    try:
        with patch("correlation_tracker.fetch_return_history") as mock_fetch:
            returns_new, returns_existing = _make_returns(0.30)
            mock_fetch.side_effect = lambda ticker, key, days=60: (
                returns_new if ticker == "MSFT" else returns_existing
            )
            result = evaluate_correlation_gate(
                new_ticker            = "MSFT",
                direction             = "bullish",
                open_position_tickers = ["AAPL"],
                db_path               = db_path,
                polygon_api_key       = "fake_key",
                telegram_bot_token    = "",
                telegram_chat_id      = "",
            )
        assert result.gate_decision == "APPROVE"
        assert result.size_adjustment == 1.0
        assert result.max_correlation < 0.60
    finally:
        os.unlink(db_path)


# ── Test 5: Three existing positions — max correlation used ───────────────────

def test_three_positions_max_correlation_governs():
    """Three existing positions; gate fires on the MAX correlation found."""
    db_path = _db_with_schema()
    try:
        # Build return series with known high correlation (~0.87)
        returns_base, returns_correlated = _make_returns(0.87)
        # Low and mid correlation series are nearly independent
        returns_low = [0.001 * math.sin(i) for i in range(60)]      # near-zero corr
        returns_mid = [0.002 * math.cos(i) for i in range(60)]      # near-zero corr

        def _mock_fetch(ticker: str, key: str, days: int = 60) -> List[float]:
            if ticker == "SPY":
                return returns_base           # new ticker — base series
            elif ticker == "GOOGL":
                return returns_correlated     # high corr (~0.87) with SPY
            elif ticker == "AAPL":
                return returns_low            # low corr with SPY
            else:  # NVDA
                return returns_mid            # low corr with SPY

        with patch("correlation_tracker.fetch_return_history", side_effect=_mock_fetch):
            result = evaluate_correlation_gate(
                new_ticker            = "SPY",
                direction             = "bearish",
                open_position_tickers = ["AAPL", "GOOGL", "NVDA"],
                db_path               = db_path,
                polygon_api_key       = "fake_key",
                telegram_bot_token    = "",
                telegram_chat_id      = "",
            )

        # GOOGL should dominate with high corr → REDUCE at 0.50x
        assert result.gate_decision == "REDUCE", \
            f"Expected REDUCE (high corr with GOOGL), got {result.gate_decision}"
        assert result.size_adjustment == 0.50, \
            f"Expected 0.50x, got {result.size_adjustment}"
        assert result.max_correlation >= 0.80, \
            f"Expected max_corr >= 0.80, got {result.max_correlation:.3f}"
        # All three tickers evaluated
        assert set(result.existing_tickers) == {"AAPL", "GOOGL", "NVDA"}, \
            f"Unexpected existing_tickers: {result.existing_tickers}"
    finally:
        os.unlink(db_path)


# ── Additional edge cases ─────────────────────────────────────────────────────

def test_decide_boundary_0_60():
    """0.60 exactly should be APPROVE (log band) at 1.0x."""
    gate, adj = _decide(0.60)
    assert gate == "APPROVE"
    assert adj == 1.0


def test_decide_boundary_0_75():
    """0.75 exactly should be REDUCE at 0.75x."""
    gate, adj = _decide(0.75)
    assert gate == "REDUCE"
    assert adj == 0.75


def test_decide_boundary_0_85():
    """0.85 exactly should be REDUCE at 0.50x."""
    gate, adj = _decide(0.85)
    assert gate == "REDUCE"
    assert adj == 0.50


def test_compute_correlation_identical_series():
    """Identical series → correlation = 1.0."""
    series = [0.01, -0.02, 0.03, 0.005, -0.01, 0.02, -0.015, 0.012, 0.008, -0.003]
    corr = compute_correlation(series, series)
    assert abs(corr - 1.0) < 1e-9, f"Expected 1.0, got {corr}"


def test_compute_correlation_constant_series():
    """Constant series → zero standard deviation → returns 0.0 (not crash)."""
    a = [0.01] * 20
    b = [0.01, -0.02, 0.03] * 7 + [0.01]
    corr = compute_correlation(a, b)
    assert abs(corr) < 1e-9, f"Expected ~0.0, got {corr}"


def test_evaluate_gate_handles_fetch_failure():
    """If Polygon returns no data, use 0.5 proxy and return a valid result."""
    db_path = _db_with_schema()
    try:
        with patch("correlation_tracker.fetch_return_history", return_value=[]):
            result = evaluate_correlation_gate(
                new_ticker            = "XYZ",
                direction             = "bullish",
                open_position_tickers = ["AAPL"],
                db_path               = db_path,
                polygon_api_key       = "fake_key",
                telegram_bot_token    = "",
                telegram_chat_id      = "",
            )
        assert result.estimated is True
        assert result.max_correlation == _ESTIMATED_CORR
        # 0.50 proxy → APPROVE at 1.0x
        assert result.gate_decision == "APPROVE"
    finally:
        os.unlink(db_path)


def test_db_record_written():
    """Correlation evaluation writes a record to correlation_records table."""
    db_path = _db_with_schema()
    try:
        returns_new, returns_existing = _make_returns(0.40)
        with patch("correlation_tracker.fetch_return_history") as mock_fetch:
            mock_fetch.side_effect = lambda ticker, key, days=60: (
                returns_new if ticker == "SPY" else returns_existing
            )
            evaluate_correlation_gate(
                new_ticker            = "SPY",
                direction             = "bullish",
                open_position_tickers = ["AAPL"],
                db_path               = db_path,
                polygon_api_key       = "fake_key",
                telegram_bot_token    = "",
                telegram_chat_id      = "",
            )
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT * FROM correlation_records").fetchall()
        conn.close()
        assert len(rows) == 1, f"Expected 1 record, got {len(rows)}"
    finally:
        os.unlink(db_path)
