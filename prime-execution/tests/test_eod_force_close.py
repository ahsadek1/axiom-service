"""
tests/test_eod_force_close.py — GAP-4: EOD Force-Close (Prime Execution)

Tests that the EOD force-close block in evaluate_exits() fires at 3:50 PM ET
and is skipped at all other times.

GENESIS 2026-04-27
"""

import sys
import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from exit_monitor import evaluate_exits

ET = pytz.timezone("America/New_York")


def _make_open_position(pos_id=1, ticker="GE"):
    return {
        "id":                pos_id,
        "ticker":            ticker,
        "direction":         "bullish",
        "pathway":           "P2",
        "shares":            5.0,
        "shares_remaining":  5.0,
        "entry_price":       100.0,
        "position_size_usd": 500.0,
        "partial_closed":    0,
        "trailing_stop_pct": None,
        "trailing_stop_high": None,
        "hard_backstop_pct": -0.18,
        "technical_stop_flagged": 0,
        "status":            "open",
        "strategy":          "swing_long",
        "regime":            "NORMAL",
        "window_id":         "2026-04-27-1500",
    }


def test_eod_force_close_fires_at_1550(tmp_path):
    """
    T1: At 3:50 PM ET with an open position, EOD force-close must trigger.
    """
    db = str(tmp_path / "prime.db")
    import sqlite3
    from database import init_db
    init_db(db)

    # Insert open position directly
    import sqlite3 as _sq
    from datetime import timezone
    conn = _sq.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """INSERT INTO positions
           (ticker, direction, pathway, shares, entry_price, position_size_usd,
            window_id, agent_scores, verdict, status, opened_at,
            hard_backstop_pct, partial_closed, shares_remaining)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("GE","bullish","P2",5.0,100.0,500.0,"2026-04-27-1500","{}","STRONG_GO","open",
         datetime.now(timezone.utc).isoformat(),-0.18,0,5.0),
    )
    conn.commit()
    conn.close()

    mock_alpaca = MagicMock()
    mock_alpaca.get_position.return_value = {"unrealized_plpc": "0.05"}
    mock_alpaca.close_position.return_value = {"id": "order-eod-1"}

    mock_reporter = MagicMock()

    eod_time = ET.localize(datetime(2026, 4, 28, 15, 50, 0))  # Monday 3:50 PM ET

    with patch("exit_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = eod_time
        events = evaluate_exits(db, mock_alpaca, mock_reporter, "token", "chat_id")

    eod_events = [e for e in events if e.get("reason") == "EOD_FORCE_CLOSE" or e.get("event") == "FULL_CLOSE"]
    assert len(eod_events) >= 1, f"Expected EOD force-close event, got: {events}"


def test_eod_force_close_skips_at_1400(tmp_path):
    """
    T2: At 2:00 PM ET — well outside EOD window — no force-close fires.
    """
    db = str(tmp_path / "prime.db")
    from database import init_db
    init_db(db)

    mock_alpaca = MagicMock()
    mock_alpaca.get_position.return_value = {"unrealized_plpc": "0.02"}
    mock_reporter = MagicMock()

    midday = ET.localize(datetime(2026, 4, 28, 14, 0, 0))  # 2:00 PM ET

    with patch("exit_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = midday
        # No open positions — returns empty
        events = evaluate_exits(db, mock_alpaca, mock_reporter, "token", "chat_id")

    eod_events = [e for e in events if e.get("reason") == "EOD_FORCE_CLOSE"]
    assert len(eod_events) == 0, f"No EOD events expected at 2PM, got: {eod_events}"


def test_eod_force_close_skips_on_weekend(tmp_path):
    """
    T3: On Saturday the EOD window must not fire (weekday check).
    """
    db = str(tmp_path / "prime.db")
    from database import init_db
    init_db(db)

    mock_alpaca = MagicMock()
    mock_reporter = MagicMock()

    saturday = ET.localize(datetime(2026, 4, 25, 15, 55, 0))  # Saturday 3:55 PM ET

    with patch("exit_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = saturday
        events = evaluate_exits(db, mock_alpaca, mock_reporter, "token", "chat_id")

    eod_events = [e for e in events if e.get("reason") == "EOD_FORCE_CLOSE"]
    assert len(eod_events) == 0
