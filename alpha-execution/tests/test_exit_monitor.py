"""
test_exit_monitor.py — Alpha exit rule unit tests.

Tests all exit triggers: partial close, trailing stop, DTE rules, +100% close.
Uses in-memory DB and mocked Alpaca client.
"""

import sys
import os
import tempfile
from datetime import date, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch
from database import (
    init_db, get_position_by_id, get_open_positions,
    reserve_position_slot, confirm_pending_position,
)
from exit_monitor import _evaluate_position, evaluate_exits


def make_position(
    db_path:      str,
    ticker:       str    = "NVDA",
    direction:    str    = "bullish",
    dte_from_now: int    = 40,
    partial:      bool   = False,
    trail_pct:    float  = None,
    trail_high:   float  = None,
    size_usd:     float  = 2000.0,
) -> dict:
    """Helper to create a test position."""
    expiry = (date.today() + timedelta(days=dte_from_now)).isoformat()
    # Use TOCTOU-safe PENDING pattern (OMNI H4 / Cipher F1+F2)
    pos_id = reserve_position_slot(
        db_path           = db_path,
        ticker            = ticker,
        direction         = direction,
        pathway           = "P1",
        option_type       = "put" if direction == "bullish" else "call",
        short_strike      = 855.0,
        long_strike       = 810.0,
        expiration_date   = expiry,
        dte_at_open       = 40,
        contracts         = 1,
        position_size_usd = size_usd,
        window_id         = "2026-04-10-0930",
        agent_scores      = '{"Cipher": 88}',
        verdict           = "GO",
    )
    confirm_pending_position(
        db_path               = db_path,
        position_id           = pos_id,
        short_alpaca_order_id = "TEST_SHORT",
        long_alpaca_order_id  = "TEST_LONG",
        short_contract_symbol = "SPY260516P855",
        long_contract_symbol  = "SPY260516P810",
        entry_price           = size_usd / 100.0,
    )
    pos = get_position_by_id(db_path, pos_id)
    if partial or trail_pct:
        from database import update_trailing_stop
        update_trailing_stop(db_path, pos_id, trail_pct or 0.15, trail_high or 0.35, partial)
        pos = get_position_by_id(db_path, pos_id)
    return pos


def make_alpaca_mock(pnl_pct: float) -> MagicMock:
    """Return a mocked AlpacaClient that reports a fixed P&L.

    The exit monitor reads unrealized_pl (dollar) from per-leg positions.
    position_size_usd = 2000.0 (default), split equally across two legs.
    So unrealized_pl per leg = pnl_pct * 2000 / 2 = pnl_pct * 1000.
    Net P&L = short_pl + long_pl = pnl_pct * 2000 → net/size_usd = pnl_pct.
    """
    DEFAULT_SIZE_USD = 2000.0
    leg_pl = str(pnl_pct * DEFAULT_SIZE_USD / 2)
    mock = MagicMock()
    mock.get_position.return_value = {"unrealized_pl": leg_pl, "unrealized_plpc": str(pnl_pct)}
    mock.close_position.return_value = {"id": "order-123", "status": "accepted"}
    return mock


def make_reporter_mock() -> MagicMock:
    mock = MagicMock()
    mock.report_outcome.return_value = True
    return mock


@pytest.fixture
def tmp_db(tmp_path):
    db_path = str(tmp_path / "test_alpha_exec.db")
    init_db(db_path)
    return db_path


class TestPartialClose:
    def test_35pct_triggers_partial_close(self, tmp_db):
        """At +35%, position should partially close (50%)."""
        pos    = make_position(tmp_db)
        alpaca = make_alpaca_mock(pnl_pct=0.36)
        result = _evaluate_position(pos, tmp_db, alpaca, make_reporter_mock(), "tok", "chat")

        assert result is not None
        assert result["event"] == "PARTIAL_CLOSE"
        # OMNI Pass 3 Finding 4: partial close now closes BOTH legs (short + long)
        # so close_position is called twice — once per leg — with the same qty.
        assert alpaca.close_position.call_count == 2
        calls = {c.args[0] for c in alpaca.close_position.call_args_list}
        assert "SPY260516P855" in calls  # short leg
        assert "SPY260516P810" in calls  # long leg

    def test_below_35pct_no_action(self, tmp_db):
        pos    = make_position(tmp_db)
        alpaca = make_alpaca_mock(pnl_pct=0.30)
        result = _evaluate_position(pos, tmp_db, alpaca, make_reporter_mock(), "tok", "chat")
        assert result is None

    def test_partial_close_activates_trailing_stop(self, tmp_db):
        """After partial close, trailing stop should be set in DB."""
        pos    = make_position(tmp_db)
        alpaca = make_alpaca_mock(pnl_pct=0.36)
        _evaluate_position(pos, tmp_db, alpaca, make_reporter_mock(), "tok", "chat")

        updated = get_position_by_id(tmp_db, pos["id"])
        assert updated["partial_closed"] == 1
        assert updated["trailing_stop_pct"] == 0.15


class TestTrailingStop:
    def test_trailing_stop_triggered(self, tmp_db):
        """When P&L drops 15% from trailing high, position should close."""
        # trail_high=0.50, trail_pct=0.15 → stop at 0.35
        pos    = make_position(tmp_db, partial=True, trail_pct=0.15, trail_high=0.50)
        alpaca = make_alpaca_mock(pnl_pct=0.34)  # below 0.50 - 0.15 = 0.35
        result = _evaluate_position(pos, tmp_db, alpaca, make_reporter_mock(), "tok", "chat")

        assert result is not None
        assert result["event"]  == "FULL_CLOSE"
        assert result["reason"] == "TRAILING_STOP"

    def test_trailing_stop_not_triggered_above_stop(self, tmp_db):
        """P&L above trailing stop — should not close."""
        pos    = make_position(tmp_db, partial=True, trail_pct=0.15, trail_high=0.50)
        alpaca = make_alpaca_mock(pnl_pct=0.40)  # 0.50 - 0.15 = 0.35; 0.40 > 0.35
        result = _evaluate_position(pos, tmp_db, alpaca, make_reporter_mock(), "tok", "chat")
        assert result is None

    def test_trailing_high_updates(self, tmp_db):
        """Trailing high should update when P&L exceeds previous high."""
        pos    = make_position(tmp_db, partial=True, trail_pct=0.15, trail_high=0.40)
        alpaca = make_alpaca_mock(pnl_pct=0.60)  # new high — updates trail_high
        result = _evaluate_position(pos, tmp_db, alpaca, make_reporter_mock(), "tok", "chat")
        assert result is None  # not stopped yet

        updated = get_position_by_id(tmp_db, pos["id"])
        assert updated["trailing_stop_high"] == 0.60


class TestFullProfit:
    def test_100pct_closes_immediately(self, tmp_db):
        pos    = make_position(tmp_db)
        alpaca = make_alpaca_mock(pnl_pct=1.02)
        result = _evaluate_position(pos, tmp_db, alpaca, make_reporter_mock(), "tok", "chat")

        assert result is not None
        assert result["event"]  == "FULL_CLOSE"
        assert result["reason"] == "PROFIT_TARGET_100"

    def test_99pct_does_not_close(self, tmp_db):
        pos    = make_position(tmp_db)
        alpaca = make_alpaca_mock(pnl_pct=0.99)
        result = _evaluate_position(pos, tmp_db, alpaca, make_reporter_mock(), "tok", "chat")
        # Should trigger partial (>35%) if not already partial
        assert result is not None
        assert result["event"] == "PARTIAL_CLOSE"  # +35% partial fires first


class TestDteRules:
    def test_dte_5_emergency_close(self, tmp_db):
        pos    = make_position(tmp_db, dte_from_now=4)  # ≤ 5 DTE
        alpaca = make_alpaca_mock(pnl_pct=0.10)
        result = _evaluate_position(pos, tmp_db, alpaca, make_reporter_mock(), "tok", "chat")

        assert result is not None
        assert result["event"]  == "FULL_CLOSE"
        assert result["reason"] == "DTE_EMERGENCY"

    def test_dte_6_close(self, tmp_db):
        pos    = make_position(tmp_db, dte_from_now=6)  # ≤ 7 DTE
        alpaca = make_alpaca_mock(pnl_pct=0.05)
        result = _evaluate_position(pos, tmp_db, alpaca, make_reporter_mock(), "tok", "chat")

        assert result is not None
        assert result["reason"] == "DTE_CLOSE"

    def test_dte_30_no_close(self, tmp_db):
        pos    = make_position(tmp_db, dte_from_now=30)
        alpaca = make_alpaca_mock(pnl_pct=0.05)
        result = _evaluate_position(pos, tmp_db, alpaca, make_reporter_mock(), "tok", "chat")
        assert result is None  # 30 DTE → roll flag only, no close


class TestOutcomeReporting:
    def test_close_reports_to_buffer(self, tmp_db):
        pos      = make_position(tmp_db, dte_from_now=4)
        alpaca   = make_alpaca_mock(pnl_pct=-0.15)
        reporter = make_reporter_mock()
        _evaluate_position(pos, tmp_db, alpaca, reporter, "tok", "chat")

        reporter.report_outcome.assert_called_once_with(
            ticker  = "NVDA",
            won     = False,
            pnl_pct = -0.15,
            pathway = "P1",
        )

    def test_win_reported_as_won(self, tmp_db):
        pos      = make_position(tmp_db, dte_from_now=4)
        alpaca   = make_alpaca_mock(pnl_pct=0.45)
        reporter = make_reporter_mock()
        _evaluate_position(pos, tmp_db, alpaca, reporter, "tok", "chat")
        reporter.report_outcome.assert_called_once()
        call_args = reporter.report_outcome.call_args[1]
        assert call_args["won"] is True


class TestNoPosition:
    def test_no_pnl_data_skips(self, tmp_db):
        """If Alpaca can't return P&L, skip this position silently."""
        pos    = make_position(tmp_db, dte_from_now=30)
        alpaca = MagicMock()
        alpaca.get_position.return_value = None   # no Alpaca position found
        result = _evaluate_position(pos, tmp_db, alpaca, make_reporter_mock(), "tok", "chat")
        assert result is None
