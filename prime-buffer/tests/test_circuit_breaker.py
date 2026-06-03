"""
test_circuit_breaker.py — Prime Buffer circuit breaker tests.

Same thresholds as Alpha — both systems use identical circuit breaker logic.
"""

import sys
import os
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from database import init_db, init_circuit_breaker, update_circuit_breaker_state
from circuit_breaker import evaluate_circuit_breaker, record_trade_outcome, reset_circuit_breaker


@pytest.fixture
def tmp_db(tmp_path):
    db_path = str(tmp_path / "test_prime.db")
    init_db(db_path)
    init_circuit_breaker(db_path)
    return db_path


class TestNormalState:
    def test_normal_allows_trading(self, tmp_db):
        assert evaluate_circuit_breaker(tmp_db).can_trade is True

    def test_normal_max_5_entries(self, tmp_db):
        assert evaluate_circuit_breaker(tmp_db).max_entries == 5


class TestAmber:
    def test_two_losses_trigger_amber(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"consecutive_losses": 2})
        status = evaluate_circuit_breaker(tmp_db)
        assert status.status == "AMBER"
        assert status.max_entries == 2

    def test_vix_25_triggers_amber(self, tmp_db):
        status = evaluate_circuit_breaker(tmp_db, current_vix=26.0)
        assert status.status == "AMBER"


class TestRed:
    def test_three_losses_trigger_red(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"consecutive_losses": 3})
        assert evaluate_circuit_breaker(tmp_db).status == "RED"

    def test_red_auto_resumes(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"consecutive_losses": 3})
        assert evaluate_circuit_breaker(tmp_db).auto_resume is True


class TestStop:
    def test_four_losses_trigger_stop(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"consecutive_losses": 4})
        assert evaluate_circuit_breaker(tmp_db).status == "STOP"

    def test_crisis_vix_triggers_stop(self, tmp_db):
        assert evaluate_circuit_breaker(tmp_db, current_vix=36.0).status == "STOP"

    def test_stop_no_auto_resume(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"consecutive_losses": 4})
        assert evaluate_circuit_breaker(tmp_db).auto_resume is False


class TestReset:
    def test_reset_clears_stop(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"consecutive_losses": 4})
        reset_circuit_breaker(tmp_db)
        assert evaluate_circuit_breaker(tmp_db).status == "NORMAL"

    def test_win_resets_consecutive_losses(self, tmp_db):
        update_circuit_breaker_state(tmp_db, {"consecutive_losses": 3})
        record_trade_outcome(tmp_db, won=True, pnl_pct=0.40)
        assert evaluate_circuit_breaker(tmp_db).status == "NORMAL"
