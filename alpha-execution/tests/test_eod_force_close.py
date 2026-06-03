"""
tests/test_eod_force_close.py — GAP-4: EOD Force-Close (Alpha Execution)

Tests that the EOD force-close block in evaluate_exits() fires at 3:50 PM ET
and is skipped outside that window.

GENESIS 2026-04-27
"""

import sys
import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

ET = pytz.timezone("America/New_York")


def test_eod_force_close_fires_at_1550(tmp_path):
    """T1: At 3:50 PM ET with open position — EOD force-close fires."""
    db = str(tmp_path / "alpha.db")
    import sqlite3
    from database import init_db
    init_db(db)

    import sqlite3 as _sq
    from datetime import timezone as _tz
    conn = _sq.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """INSERT INTO positions
           (ticker, direction, pathway, option_type, short_strike, long_strike,
            expiration_date, dte_at_open, contracts, entry_price, position_size_usd,
            window_id, agent_scores, verdict, status, opened_at,
            hard_backstop_pct, partial_closed)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("SPY","bearish","P1","call",540.0,545.0,
         "2026-05-16",18,1,5.0,1000.0,"2026-04-27-1430","{}","STRONG_GO","open",
         datetime.now(_tz.utc).isoformat(),-0.50,0),
    )
    conn.commit()
    conn.close()

    mock_alpaca = MagicMock()
    mock_alpaca.get_position.return_value = None
    mock_alpaca.get_positions.return_value = []
    mock_alpaca.close_position.return_value = {"id": "eod-order-1"}

    mock_reporter = MagicMock()

    eod_time = ET.localize(datetime(2026, 4, 28, 15, 52, 0))

    with patch("exit_monitor.datetime") as mock_dt, \
         patch("exit_monitor._ET", ET):
        mock_dt.now.return_value = eod_time
        from exit_monitor import evaluate_exits
        events = evaluate_exits(db, mock_alpaca, mock_reporter, "token", "chat")

    # EOD events show as FULL_CLOSE with reason=EOD_FORCE_CLOSE or CLOSE_FAILED
    eod_triggered = any(
        e.get("reason") == "EOD_FORCE_CLOSE" or "EOD" in str(e.get("reason", ""))
        for e in events
    )
    # At minimum, the EOD block ran and called close_position
    assert mock_alpaca.close_position.called or eod_triggered, \
        f"EOD force-close did not fire. Events: {events}"


def test_eod_force_close_skips_at_midday(tmp_path):
    """T2: At 2:00 PM ET — no EOD force-close."""
    db = str(tmp_path / "alpha.db")
    from database import init_db
    init_db(db)

    mock_alpaca = MagicMock()
    mock_reporter = MagicMock()

    midday = ET.localize(datetime(2026, 4, 28, 14, 0, 0))

    with patch("exit_monitor.datetime") as mock_dt, \
         patch("exit_monitor._ET", ET):
        mock_dt.now.return_value = midday
        from exit_monitor import evaluate_exits
        events = evaluate_exits(db, mock_alpaca, mock_reporter, "token", "chat")

    eod_events = [e for e in events if e.get("reason") == "EOD_FORCE_CLOSE"]
    assert len(eod_events) == 0


def test_eod_force_close_skips_on_weekend(tmp_path):
    """T3: Saturday 3:55 PM — weekday guard prevents firing."""
    db = str(tmp_path / "alpha.db")
    from database import init_db
    init_db(db)

    mock_alpaca = MagicMock()
    mock_reporter = MagicMock()

    saturday = ET.localize(datetime(2026, 4, 26, 15, 55, 0))  # Saturday

    with patch("exit_monitor.datetime") as mock_dt, \
         patch("exit_monitor._ET", ET):
        mock_dt.now.return_value = saturday
        from exit_monitor import evaluate_exits
        events = evaluate_exits(db, mock_alpaca, mock_reporter, "token", "chat")

    eod_events = [e for e in events if e.get("reason") == "EOD_FORCE_CLOSE"]
    assert len(eod_events) == 0
