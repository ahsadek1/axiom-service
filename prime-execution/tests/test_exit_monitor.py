"""
test_exit_monitor.py — Prime execution exit rules.

Tests -18% backstop, trailing stop (12% → tighten to 8% at +50%),
partial close, technical stop, and outcome reporting.
"""

import sys
import os
import tempfile
from datetime import date, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock
from database import init_db, save_position, get_position_by_id, flag_technical_stop, update_trailing_stop
from exit_monitor import _evaluate_position, evaluate_exits


def make_position(db_path, ticker="TSLA", direction="bullish",
                  partial=False, trail_pct=None, trail_high=None,
                  technical_stop=False, size_usd=2000.0):
    pos_id = save_position(
        db_path=db_path, ticker=ticker, direction=direction, pathway="P1",
        shares=20, position_size_usd=size_usd, window_id="2026-04-10-0930",
        agent_scores={"Cipher": 85}, verdict="GO", entry_price=100.0,
    )
    if partial or trail_pct:
        update_trailing_stop(db_path, pos_id, trail_pct or 0.12, trail_high or 0.35,
                             partial_closed=partial, shares_remaining=10)
    if technical_stop:
        flag_technical_stop(db_path, pos_id)
    return get_position_by_id(db_path, pos_id)


def mock_alpaca(pnl_pct):
    m = MagicMock()
    m.get_position.return_value = {"unrealized_plpc": str(pnl_pct)}
    m.close_position.return_value = {"id": "ord-1"}
    return m


def mock_reporter():
    m = MagicMock()
    m.report_outcome.return_value = True
    return m


@pytest.fixture
def tmp_db(tmp_path):
    p = str(tmp_path / "prime_exec.db")
    init_db(p)
    return p


class TestBackstop:
    def test_neg_18pct_triggers_close(self, tmp_db):
        pos    = make_position(tmp_db)
        result = _evaluate_position(pos, tmp_db, mock_alpaca(-0.185), mock_reporter(), "t","c")
        assert result is not None
        assert result["reason"] == "HARD_BACKSTOP"

    def test_neg_17pct_no_close(self, tmp_db):
        pos    = make_position(tmp_db)
        result = _evaluate_position(pos, tmp_db, mock_alpaca(-0.17), mock_reporter(), "t","c")
        assert result is None

    def test_backstop_reports_loss_to_buffer(self, tmp_db):
        pos      = make_position(tmp_db)
        reporter = mock_reporter()
        _evaluate_position(pos, tmp_db, mock_alpaca(-0.20), reporter, "t","c")
        reporter.report_outcome.assert_called_once()
        args = reporter.report_outcome.call_args[1]
        assert args["won"] is False


class TestTechnicalStop:
    def test_technical_flag_closes_position(self, tmp_db):
        pos    = make_position(tmp_db, technical_stop=True)
        result = _evaluate_position(pos, tmp_db, mock_alpaca(0.05), mock_reporter(), "t","c")
        assert result is not None
        assert result["reason"] == "TECHNICAL_STOP"

    def test_unflagged_position_not_technical_stopped(self, tmp_db):
        pos    = make_position(tmp_db, technical_stop=False)
        result = _evaluate_position(pos, tmp_db, mock_alpaca(0.05), mock_reporter(), "t","c")
        assert result is None  # no other trigger at +5%


class TestPartialClose:
    def test_35pct_triggers_partial(self, tmp_db):
        pos    = make_position(tmp_db)
        result = _evaluate_position(pos, tmp_db, mock_alpaca(0.36), mock_reporter(), "t","c")
        assert result is not None
        assert result["event"] == "PARTIAL_CLOSE"

    def test_partial_sets_12pct_trailing_stop(self, tmp_db):
        pos = make_position(tmp_db)
        _evaluate_position(pos, tmp_db, mock_alpaca(0.36), mock_reporter(), "t","c")
        updated = get_position_by_id(tmp_db, pos["id"])
        assert updated["partial_closed"] == 1
        assert updated["trailing_stop_pct"] == 0.12


class TestTrailingStop:
    def test_12pct_trailing_stop_fires(self, tmp_db):
        # trail_high=0.45, trail=0.12 → stop at 0.33; current=0.30 → fires
        pos    = make_position(tmp_db, partial=True, trail_pct=0.12, trail_high=0.45)
        result = _evaluate_position(pos, tmp_db, mock_alpaca(0.30), mock_reporter(), "t","c")
        assert result is not None
        assert result["reason"] == "TRAILING_STOP"

    def test_trail_tightens_to_8pct_at_50pct(self, tmp_db):
        """At +50%, trailing stop tightens from 12% to 8%."""
        pos    = make_position(tmp_db, partial=True, trail_pct=0.12, trail_high=0.48)
        # Current = 0.51 (≥ 0.50 tighten trigger)
        result = _evaluate_position(pos, tmp_db, mock_alpaca(0.51), mock_reporter(), "t","c")
        assert result is None  # not stopped yet — 0.51 not below 0.51-0.08=0.43

        updated = get_position_by_id(tmp_db, pos["id"])
        assert updated["trailing_stop_pct"] == 0.08   # tightened

    def test_8pct_tight_stop_fires(self, tmp_db):
        """After tightening to 8%, stop fires when P&L drops 8% from high."""
        pos    = make_position(tmp_db, partial=True, trail_pct=0.08, trail_high=0.60)
        # high=0.60, trail=0.08 → stop at 0.52; current=0.51 → fires
        result = _evaluate_position(pos, tmp_db, mock_alpaca(0.51), mock_reporter(), "t","c")
        assert result is not None
        assert result["reason"] == "TRAILING_STOP"


class TestNoDataSkip:
    def test_no_alpaca_position_skips(self, tmp_db):
        pos    = make_position(tmp_db)
        alpaca = MagicMock()
        alpaca.get_position.return_value = None
        result = _evaluate_position(pos, tmp_db, alpaca, mock_reporter(), "t","c")
        assert result is None
