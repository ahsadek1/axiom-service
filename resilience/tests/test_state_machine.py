"""Tests for trade_state_machine.py"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from trade_state_machine import TradeState, TradeSM, TRANSITIONS, TERMINAL_STATES, _ensure_db


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_trade_states.db")


@pytest.fixture
def sm(db_path):
    return TradeSM(trade_id="TEST_001", db_path=db_path, recovery_fns={})


def test_initial_state(sm):
    assert sm.current_state == TradeState.SIGNAL_GENERATED


def test_transition(sm):
    sm.transition(TradeState.CONCORDANCE_SUBMITTED)
    assert sm.current_state == TradeState.CONCORDANCE_SUBMITTED


def test_transition_logged(sm, db_path):
    import sqlite3
    sm.transition(TradeState.CONCORDANCE_SUBMITTED)
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT state FROM trade_state_log WHERE trade_id='TEST_001'").fetchall()
    conn.close()
    states = [r[0] for r in rows]
    assert TradeState.CONCORDANCE_SUBMITTED.value in states


def test_on_failure_escalates_without_recovery_fn(sm):
    alerts = []
    sm.telegram_alert_fn = alerts.append
    sm.on_failure(Exception("test error"))
    # Should escalate since no recovery_fn registered
    assert sm.current_state == TradeState.ESCALATED


def test_on_failure_calls_recovery_fn(db_path):
    called = []
    def mock_discard(trade_id, metadata):
        called.append(trade_id)
    sm = TradeSM(trade_id="TEST_002", db_path=db_path, recovery_fns={"discard_signal": mock_discard})
    sm.on_failure(Exception("signal failed"))
    assert "TEST_002" in called
    assert sm.current_state == TradeState.RECOVERING


def test_partial_fill_emergency(db_path):
    alerts = []
    sm = TradeSM(
        trade_id="TEST_003", db_path=db_path,
        recovery_fns={},
        telegram_alert_fn=alerts.append,
    )
    sm.current_state = TradeState.LEG1_FILLED
    sm.on_failure(Exception("leg2 failed"))
    assert sm.current_state == TradeState.PARTIAL_FILL_DETECTED
    assert any("PARTIAL FILL" in a for a in alerts)


def test_is_terminal(sm):
    assert not sm.is_terminal()
    sm.current_state = TradeState.CLOSED
    assert sm.is_terminal()


def test_recover_from_crash(db_path):
    sm1 = TradeSM(trade_id="TEST_RECOVER", db_path=db_path, recovery_fns={})
    sm1.transition(TradeState.MONITORING_ACTIVE)

    sm2 = TradeSM.recover_from_crash(trade_id="TEST_RECOVER", db_path=db_path, recovery_fns={})
    assert sm2 is not None
    assert sm2.current_state == TradeState.MONITORING_ACTIVE


def test_recover_nonexistent_trade(db_path):
    result = TradeSM.recover_from_crash(trade_id="NONEXISTENT", db_path=db_path, recovery_fns={})
    assert result is None


def test_all_states_in_transitions_or_terminal():
    """Every non-terminal state should have a defined transition."""
    for state in TradeState:
        if state not in TERMINAL_STATES and state != TradeState.RECOVERING:
            assert state in TRANSITIONS, f"State {state} missing from TRANSITIONS"
