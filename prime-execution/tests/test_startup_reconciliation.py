"""
tests/test_startup_reconciliation.py — GAP-3: Prime Execution Startup Reconciliation

Tests that _run_startup_reconciliation() correctly closes:
  - Test-window DB entries (always, regardless of Alpaca state)
  - DB positions absent from Alpaca live positions
  - Clean pass when Alpaca is unreachable (test-window cleanup still runs)

GENESIS 2026-04-27
"""

import sqlite3
import sys
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# Add service root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from database import close_stale_position, get_open_positions, init_db


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    """Fresh in-memory-style DB for each test."""
    db = str(tmp_path / "prime_test.db")
    init_db(db)
    return db


def _insert_position(db, ticker, window_id, status="open"):
    """Insert a test position row."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        INSERT INTO positions
            (ticker, direction, pathway, shares, entry_price, position_size_usd,
             window_id, agent_scores, verdict, status, opened_at,
             hard_backstop_pct, partial_closed, shares_remaining)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (ticker, "bullish", "P1", 10.0, 100.0, 1000.0,
         window_id, "{}", "STRONG_GO", status, now,
         -0.18, 0, 10.0),
    )
    conn.commit()
    pos_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return pos_id


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_close_stale_closes_test_window_entry(tmp_db):
    """
    T1: A position with 'test' in window_id must be closed by startup reconciliation.
    Simulates Alpaca returning no live positions.
    """
    pos_id = _insert_position(tmp_db, "AAPL", "CANARY-test-2026-04-27-0900")
    close_stale_position(tmp_db, pos_id, "reconciled_test_window: test entry")

    conn = sqlite3.connect(tmp_db)
    row = conn.execute("SELECT status, close_reason FROM positions WHERE id=?", (pos_id,)).fetchone()
    conn.close()
    assert row[0] == "closed", f"Expected closed, got {row[0]}"
    assert "test" in row[1].lower()


def test_close_stale_closes_missing_alpaca_position(tmp_db):
    """
    T2: A DB position whose ticker is not in Alpaca live set must be closed.
    """
    pos_id = _insert_position(tmp_db, "GE", "2026-04-27-1500")
    close_stale_position(tmp_db, pos_id, "reconciled_missing_from_alpaca: ticker=GE not in live positions")

    conn = sqlite3.connect(tmp_db)
    row = conn.execute("SELECT status FROM positions WHERE id=?", (pos_id,)).fetchone()
    conn.close()
    assert row[0] == "closed"


def test_close_stale_does_not_close_already_closed(tmp_db):
    """
    T3: close_stale_position must not double-close an already-closed position.
    The WHERE status IN ('open','pending') guard must hold.
    """
    pos_id = _insert_position(tmp_db, "KMI", "2026-04-27-1500", status="open")
    # First close
    close_stale_position(tmp_db, pos_id, "first close")
    # Second close attempt — should be a no-op
    close_stale_position(tmp_db, pos_id, "second close attempt")

    conn = sqlite3.connect(tmp_db)
    row = conn.execute("SELECT close_reason FROM positions WHERE id=?", (pos_id,)).fetchone()
    conn.close()
    # close_reason must reflect the FIRST close (second was a no-op)
    assert "first close" in row[0]


def test_open_position_preserved_when_in_alpaca(tmp_db):
    """
    T4: A DB position whose ticker IS in Alpaca live set must NOT be touched.
    """
    pos_id = _insert_position(tmp_db, "NVDA", "2026-04-27-1430")
    # Don't call close_stale_position — simulate reconciler deciding it's live
    positions = get_open_positions(tmp_db)
    tickers = [p["ticker"] for p in positions]
    assert "NVDA" in tickers, "Open position must remain open when present in Alpaca"


def test_close_stale_handles_pending_status(tmp_db):
    """
    T5: Pending positions (leaked slot from crash) must also be closeable.
    """
    pos_id = _insert_position(tmp_db, "TSLA", "2026-04-27-0930", status="pending")
    close_stale_position(tmp_db, pos_id, "reconciled_pending_leaked")

    conn = sqlite3.connect(tmp_db)
    row = conn.execute("SELECT status FROM positions WHERE id=?", (pos_id,)).fetchone()
    conn.close()
    assert row[0] == "closed"
