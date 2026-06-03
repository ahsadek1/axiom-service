"""
test_roll_manager.py — Tests for roll_manager.py

Coverage:
  1. Happy path: DTE ≤ 21, position open → rolled successfully
  2. Already rolled today → skipped (dedup)
  3. Alpaca close order fails → position stays open, returns failed
  4. No roll contracts available → skipped with reason
  5. New order fails after close → P0 alert fired, returns failed
  6. Position already closed in DB → skipped
  7. evaluate_rolls() with empty DB → returns []
  8. Roll dedup: second call for same position_id on same day → skipped
"""

import os
import sys
import sqlite3
import pytest
from datetime import date, datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_SVC  = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SVC)
for _p in (_SVC, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Import module under test ──────────────────────────────────────────────────
import roll_manager
from roll_manager import RollResult, evaluate_rolls, _attempt_roll, _rolled_today


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_dedup():
    """Reset in-memory dedup dict before each test."""
    _rolled_today.clear()
    yield
    _rolled_today.clear()


@pytest.fixture()
def tmp_db(tmp_path):
    """Minimal SQLite DB with positions table for testing."""
    from database import init_db
    db_path = str(tmp_path / "test_roll.db")
    init_db(db_path)
    return db_path


def _insert_position(db_path: str, **overrides) -> int:
    """Insert a test position row and return its ID."""
    defaults = {
        "ticker":               "SPY",
        "direction":            "bullish",
        "pathway":              "P1",
        "option_type":          "put",
        "short_strike":         480.0,
        "long_strike":          470.0,
        "contracts":            1,
        "position_size_usd":    2000.0,
        "dte_at_open":          40,
        "window_id":            "test-window",
        "agent_scores":         "{}",
        "verdict":              "GO",
        "short_contract_symbol": "SPYP00480000",
        "long_contract_symbol":  "SPYP00470000",
        "entry_price":          2.50,
        "expiration_date":      (date.today() + timedelta(days=15)).isoformat(),
        "status":               "open",
        "opened_at":            datetime.now(timezone.utc).isoformat(),
    }
    defaults.update(overrides)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO positions
                (ticker, direction, pathway, option_type, short_strike, long_strike,
                 contracts, position_size_usd, dte_at_open, window_id,
                 agent_scores, verdict, short_contract_symbol, long_contract_symbol,
                 entry_price, expiration_date, status, opened_at)
            VALUES
                (:ticker, :direction, :pathway, :option_type, :short_strike, :long_strike,
                 :contracts, :position_size_usd, :dte_at_open, :window_id,
                 :agent_scores, :verdict, :short_contract_symbol, :long_contract_symbol,
                 :entry_price, :expiration_date, :status, :opened_at)
            """,
            defaults,
        )
        return cur.lastrowid


def _make_alpaca(
    price=500.0,
    contracts=None,
    close_raises=None,
    open_raises=None,
) -> MagicMock:
    """Build a mock AlpacaClient with controllable behavior."""
    client = MagicMock()
    client.get_latest_price.return_value = price

    # Default option contracts
    if contracts is None:
        contracts = [
            {"symbol": "SPYP00475000", "strike_price": "475.0", "expiration_date": "2026-06-20"},
            {"symbol": "SPYP00450000", "strike_price": "450.0", "expiration_date": "2026-06-20"},
            {"symbol": "SPYP00480000", "strike_price": "480.0", "expiration_date": "2026-06-20"},
        ]
    client.get_option_contracts.return_value = contracts

    if close_raises:
        # First call (close) raises, second call (open) is normal
        client.place_spread_order.side_effect = close_raises
    elif open_raises:
        # First call (close) succeeds, second (open) raises
        client.place_spread_order.side_effect = [{"id": "close-ord-001"}, open_raises]
    else:
        client.place_spread_order.return_value = {"id": "fake-spread-001", "status": "accepted"}

    return client


# ── Test 1: Happy path ────────────────────────────────────────────────────────

def test_happy_path_roll(tmp_db):
    """Position at DTE=15 rolls successfully."""
    pos_id = _insert_position(tmp_db)
    client = _make_alpaca()

    with patch("roll_manager._send_roll_alert") as mock_alert, \
         patch("roll_manager._log_to_chronicle"), \
         patch("roll_manager._send_p0_alert"):

        pos = {
            "id": pos_id,
            "ticker": "SPY",
            "direction": "bullish",
            "contracts": 1,
            "position_size_usd": 2000.0,
            "window_id": "test-window",
            "agent_scores": "{}",
            "verdict": "GO", "pathway": "P1",
            "short_contract_symbol": "SPYP00480000",
            "long_contract_symbol":  "SPYP00470000",
            "expiration_date": (date.today() + timedelta(days=15)).isoformat(),
            "status": "open",
        }
        result = _attempt_roll(pos, tmp_db, client, "tok", "chat")

    assert result.action == "rolled", f"Expected 'rolled', got '{result.action}': {result.error}"
    assert result.new_position_id is not None
    mock_alert.assert_called_once()
    # dedup entry set
    assert _rolled_today.get(pos_id) == date.today()


# ── Test 2: Already rolled today ─────────────────────────────────────────────

def test_dedup_skip_same_day(tmp_db):
    """Calling _attempt_roll twice for same pos_id on same day → second is skipped."""
    pos_id = _insert_position(tmp_db)
    _rolled_today[pos_id] = date.today()

    pos = {
        "id": pos_id, "ticker": "SPY", "direction": "bullish",
        "contracts": 1, "position_size_usd": 2000.0,
        "window_id": "w", "agent_scores": "{}", "verdict": "GO", "pathway": "P1",
        "short_contract_symbol": "SPYP00480000",
        "long_contract_symbol":  "SPYP00470000",
        "expiration_date": (date.today() + timedelta(days=15)).isoformat(),
        "status": "open",
    }
    result = _attempt_roll(pos, tmp_db, _make_alpaca(), "tok", "chat")

    assert result.action == "skipped"
    assert result.reason == "already_rolled_today"


# ── Test 3: Close order fails ─────────────────────────────────────────────────

def test_close_order_fails(tmp_db):
    """Alpaca close order raises → old position stays open, result is failed."""
    pos_id = _insert_position(tmp_db)
    client = _make_alpaca(close_raises=RuntimeError("Alpaca 503"))

    pos = {
        "id": pos_id, "ticker": "SPY", "direction": "bullish",
        "contracts": 1, "position_size_usd": 2000.0,
        "window_id": "w", "agent_scores": "{}", "verdict": "GO", "pathway": "P1",
        "short_contract_symbol": "SPYP00480000",
        "long_contract_symbol":  "SPYP00470000",
        "expiration_date": (date.today() + timedelta(days=15)).isoformat(),
        "status": "open",
    }
    with patch("roll_manager._send_roll_alert") as mock_alert:
        result = _attempt_roll(pos, tmp_db, client, "tok", "chat")

    assert result.action == "failed"
    assert result.reason == "close_order_failed"
    assert "Alpaca 503" in result.error

    # Old position still open in DB
    with sqlite3.connect(tmp_db) as conn:
        row = conn.execute("SELECT status FROM positions WHERE id=?", (pos_id,)).fetchone()
    assert row[0] == "open"

    mock_alert.assert_called_once()


# ── Test 4: No contracts available ───────────────────────────────────────────

def test_no_contracts_available(tmp_db):
    """Alpaca returns empty option chain → skipped."""
    pos_id = _insert_position(tmp_db)
    client = _make_alpaca(contracts=[])

    pos = {
        "id": pos_id, "ticker": "SPY", "direction": "bullish",
        "contracts": 1, "position_size_usd": 2000.0,
        "window_id": "w", "agent_scores": "{}", "verdict": "GO", "pathway": "P1",
        "short_contract_symbol": "SPYP00480000",
        "long_contract_symbol":  "SPYP00470000",
        "expiration_date": (date.today() + timedelta(days=15)).isoformat(),
        "status": "open",
    }
    with patch("roll_manager._send_roll_alert") as mock_alert:
        result = _attempt_roll(pos, tmp_db, client, "tok", "chat")

    assert result.action == "skipped"
    assert result.reason == "no_contracts_available"
    mock_alert.assert_called_once()


# ── Test 5: New order fails after close ──────────────────────────────────────

def test_new_order_fails_after_close(tmp_db):
    """Close succeeds but reopen fails → P0 alert fired, returns failed."""
    pos_id = _insert_position(tmp_db)
    client = _make_alpaca(open_raises=RuntimeError("order rejected"))

    pos = {
        "id": pos_id, "ticker": "SPY", "direction": "bullish",
        "contracts": 1, "position_size_usd": 2000.0,
        "window_id": "w", "agent_scores": "{}", "verdict": "GO", "pathway": "P1",
        "short_contract_symbol": "SPYP00480000",
        "long_contract_symbol":  "SPYP00470000",
        "expiration_date": (date.today() + timedelta(days=15)).isoformat(),
        "status": "open",
    }
    with patch("roll_manager._send_p0_alert") as mock_p0, \
         patch("roll_manager._log_to_chronicle") as mock_chronicle, \
         patch("roll_manager._send_roll_alert"):

        result = _attempt_roll(pos, tmp_db, client, "tok", "chat")

    assert result.action == "failed"
    assert result.reason == "new_order_failed_after_close"
    mock_p0.assert_called_once()
    mock_chronicle.assert_called_once()


# ── Test 6: Position already closed in DB ────────────────────────────────────

def test_position_already_closed_skipped(tmp_db):
    """Position status='closed' in DB → _attempt_roll returns skipped."""
    pos_id = _insert_position(tmp_db, status="closed")

    pos = {
        "id": pos_id, "ticker": "SPY", "direction": "bullish",
        "contracts": 1, "position_size_usd": 2000.0,
        "window_id": "w", "agent_scores": "{}", "verdict": "GO", "pathway": "P1",
        "short_contract_symbol": "SPYP00480000",
        "long_contract_symbol":  "SPYP00470000",
        "expiration_date": (date.today() + timedelta(days=15)).isoformat(),
        "status": "closed",
    }
    result = _attempt_roll(pos, tmp_db, _make_alpaca(), "tok", "chat")
    assert result.action == "skipped"
    assert result.reason == "position_not_open"


# ── Test 7: evaluate_rolls with empty DB ─────────────────────────────────────

def test_evaluate_rolls_empty_db(tmp_db):
    """No open positions → evaluate_rolls returns empty list, no errors."""
    results = evaluate_rolls(tmp_db, _make_alpaca(), "tok", "chat")
    assert results == []


# ── Test 8: Positions above DTE threshold not touched ────────────────────────

def test_evaluate_rolls_skips_high_dte(tmp_db):
    """Position with DTE=30 (above threshold=21) should not be touched."""
    _insert_position(
        tmp_db,
        expiration_date=(date.today() + timedelta(days=30)).isoformat(),
    )
    with patch("roll_manager._attempt_roll") as mock_attempt:
        results = evaluate_rolls(tmp_db, _make_alpaca(), "tok", "chat")

    mock_attempt.assert_not_called()
    assert results == []
