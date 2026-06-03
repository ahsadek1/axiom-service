"""
test_circuit_breaker.py — Unit tests for circuit breaker logic.

Uses temp DB — never touches production state.
"""

import sys
import os
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from database import init_db, init_circuit_breaker, update_circuit_breaker_state
from circuit_breaker import (
    evaluate_circuit_breaker,
    record_trade_outcome,
    reset_circuit_breaker,
)


@pytest.fixture
def tmp_db(tmp_path):
    db_path = str(tmp_path / "test_alpha.db")
    init_db(db_path)
    init_circuit_breaker(db_path)
    return db_path


class TestNormalState:
    def test_normal_state_can_trade(self, tmp_db):
        status = evaluate_circuit_breaker(tmp_db)
        assert status.can_trade is True
        assert status.status == "NORMAL"

    def test_normal_max_entries_is_five(self, tmp_db):
        status = evaluate_circuit_breaker(tmp_db)
        assert status.max_entries == 5


class TestAmberTriggers:
    def test_two_consecutive_losses_triggers_amber(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"consecutive_losses": 2})
        status = evaluate_circuit_breaker(tmp_db)
        assert status.status == "AMBER"
        assert status.max_entries == 2

    def test_daily_loss_3pct_triggers_amber(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"daily_pnl_pct": -0.031})
        status = evaluate_circuit_breaker(tmp_db)
        assert status.status == "AMBER"

    def test_amber_allows_reduced_trading(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"consecutive_losses": 2})
        status = evaluate_circuit_breaker(tmp_db)
        assert status.can_trade is True    # 0 trades today < 2 limit
        assert status.max_entries == 2


class TestRedTriggers:
    def test_three_consecutive_losses_triggers_red(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"consecutive_losses": 3})
        status = evaluate_circuit_breaker(tmp_db)
        assert status.status == "RED"
        assert status.can_trade is False

    def test_daily_loss_5pct_triggers_red(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"daily_pnl_pct": -0.051})
        status = evaluate_circuit_breaker(tmp_db)
        assert status.status == "RED"
        assert status.auto_resume is True

    def test_weekly_loss_8pct_triggers_red(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"weekly_pnl_pct": -0.081})
        status = evaluate_circuit_breaker(tmp_db)
        assert status.status == "RED"


class TestStopTriggers:
    def test_four_consecutive_losses_triggers_stop(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"consecutive_losses": 4})
        status = evaluate_circuit_breaker(tmp_db)
        assert status.status == "STOP"
        assert status.can_trade is False
        assert status.auto_resume is False

    def test_vix_above_35_triggers_stop(self, tmp_db):
        status = evaluate_circuit_breaker(tmp_db, current_vix=36.0)
        assert status.status == "STOP"
        assert status.can_trade is False

    def test_daily_loss_8pct_triggers_stop(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"daily_pnl_pct": -0.081})
        status = evaluate_circuit_breaker(tmp_db)
        assert status.status == "STOP"

    def test_weekly_loss_12pct_triggers_stop(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"weekly_pnl_pct": -0.121})
        status = evaluate_circuit_breaker(tmp_db)
        assert status.status == "STOP"


class TestManualOverride:
    def test_manual_override_blocks_trading(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {
            "manual_override":      1,
            "manual_override_note": "Testing manual override",
        })
        status = evaluate_circuit_breaker(tmp_db)
        assert status.status == "STOP"
        assert status.can_trade is False
        assert status.auto_resume is False


class TestRecordOutcome:
    def test_win_resets_consecutive_losses(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"consecutive_losses": 3})
        record_trade_outcome(tmp_db, won=True, pnl_pct=0.35)
        status = evaluate_circuit_breaker(tmp_db)
        assert status.status == "NORMAL"

    def test_loss_increments_consecutive(self, tmp_db):
        # Use small loss amounts that only trip the consecutive counter (not daily $ threshold)
        record_trade_outcome(tmp_db, won=False, pnl_pct=-0.005)
        record_trade_outcome(tmp_db, won=False, pnl_pct=-0.005)
        status = evaluate_circuit_breaker(tmp_db)
        assert status.status == "AMBER"  # 2 consecutive losses = AMBER


class TestManualReset:
    def test_reset_clears_stop_state(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"consecutive_losses": 4, "status": "STOP"})
        reset_circuit_breaker(tmp_db)
        status = evaluate_circuit_breaker(tmp_db)
        assert status.status == "NORMAL"
        assert status.can_trade is True
