"""
database.py — Alpha Execution SQLite Layer

Tracks open positions, trade history, and daily entry counts.
WAL mode. No /tmp paths — persistent storage only.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Generator, Optional

logger = logging.getLogger("alpha_exec.database")


@contextmanager
def get_conn(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """
    Initialize the Alpha Execution database schema.

    Args:
        db_path: Path to the SQLite database file.
    """
    with get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker                TEXT    NOT NULL,
                direction             TEXT    NOT NULL,
                pathway               TEXT    NOT NULL,
                option_type           TEXT    NOT NULL,
                short_strike          REAL    NOT NULL,
                long_strike           REAL    NOT NULL,
                expiration_date       TEXT    NOT NULL,
                dte_at_open           INTEGER NOT NULL,
                contracts             INTEGER NOT NULL,
                entry_price           REAL,
                position_size_usd     REAL    NOT NULL,
                short_alpaca_order_id TEXT,
                long_alpaca_order_id  TEXT,
                short_contract_symbol TEXT,   -- OCC symbol for short leg (adversarial fix #1/#4)
                long_contract_symbol  TEXT,   -- OCC symbol for long leg  (adversarial fix #1/#4)
                status                TEXT    NOT NULL DEFAULT 'open',
                partial_closed        INTEGER NOT NULL DEFAULT 0,
                trailing_stop_pct     REAL,
                trailing_stop_high    REAL,
                window_id             TEXT,
                agent_scores          TEXT,   -- JSON
                verdict               TEXT,
                opened_at             TEXT    NOT NULL,
                closed_at             TEXT,
                close_reason          TEXT,   -- DTE_ROLL, DTE_CLOSE, PROFIT_TARGET, TRAILING_STOP
                pnl_pct               REAL,
                pnl_usd               REAL
            );
            

            CREATE INDEX IF NOT EXISTS idx_positions_status
                ON positions(status, ticker);

            CREATE TABLE IF NOT EXISTS daily_entries (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_date    TEXT    NOT NULL,
                ticker        TEXT    NOT NULL,
                position_id   INTEGER,
                entered_at    TEXT    NOT NULL,
                UNIQUE(entry_date, ticker)
            );
        """)
        # Migration: add contract symbol columns for existing DBs (adversarial fix #1/#4)
        # SQLite ALTER TABLE ADD COLUMN is safe to call; we catch the error if column exists.
        for col in ("short_contract_symbol TEXT", "long_contract_symbol TEXT"):
            try:
                conn.execute(f"ALTER TABLE positions ADD COLUMN {col}")
            except Exception:
                pass  # Column already exists — safe to ignore

        # Create the contract symbol index AFTER migration (columns now exist)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_positions_contract_symbols "
            "ON positions(short_contract_symbol, long_contract_symbol)"
        )
    logger.info("Alpha Execution database initialized at %s", db_path)


def count_open_positions(db_path: str) -> int:
    """Return count of currently open or pending positions.

    Cipher Finding 1+2 fix (TOCTOU): must include 'pending' records so that
    a reservation written inside the lock is visible to the next concurrent
    request — preventing limit bypass under concurrent load.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM positions WHERE status IN ('open','pending')"
        ).fetchone()
    return row["cnt"] if row else 0


def reserve_position_slot(
    db_path:           str,
    ticker:            str,
    direction:         str,
    pathway:           str,
    option_type:       str,
    short_strike:      float,
    long_strike:       float,
    expiration_date:   str,
    dte_at_open:       int,
    contracts:         int,
    position_size_usd: float,
    window_id:         str,
    agent_scores:      str,
    verdict:           str,
) -> int:
    """
    Write a status='pending' placeholder record inside the position limit lock.

    Cipher Finding 1+2 fix (TOCTOU): reserves the position slot before releasing
    the lock and calling Alpaca. Prevents two concurrent requests from both passing
    the limit check against the same open count.

    Returns:
        The new position row ID (used to confirm or cancel later).
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO positions (
                ticker, direction, pathway, option_type,
                short_strike, long_strike, expiration_date, dte_at_open,
                contracts, position_size_usd, window_id, agent_scores, verdict,
                status, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (ticker, direction, pathway, option_type,
             short_strike, long_strike, expiration_date, dte_at_open,
             contracts, position_size_usd, window_id, agent_scores, verdict, now),
        )
        conn.commit()
        pending_id = cursor.lastrowid
        if not pending_id:
            # Fallback SELECT for edge cases (e.g., upsert with no lastrowid)
            row = conn.execute(
                "SELECT id FROM positions WHERE ticker=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            pending_id = row["id"] if row else None
    return pending_id


def confirm_pending_position(
    db_path:              str,
    position_id:          int,
    short_alpaca_order_id: str,
    long_alpaca_order_id:  str,
    short_contract_symbol: str,
    long_contract_symbol:  str,
    entry_price:           float,
) -> None:
    """
    Promote a pending reservation to a live open position after Alpaca confirms.

    Sets all Alpaca-returned fields and flips status from 'pending' to 'open'.
    """
    now   = datetime.now(timezone.utc).isoformat()
    today = date.today().isoformat()
    with get_conn(db_path) as conn:
        # Fetch ticker before update (needed for daily_entries)
        row = conn.execute(
            "SELECT ticker FROM positions WHERE id=? AND status='pending'", (position_id,)
        ).fetchone()
        if not row:
            logger.warning("confirm_pending_position: no pending record found for id=%d", position_id)
            return
        ticker = row["ticker"]

        conn.execute(
            """
            UPDATE positions
            SET status='open',
                short_alpaca_order_id=?,
                long_alpaca_order_id=?,
                short_contract_symbol=?,
                long_contract_symbol=?,
                entry_price=?
            WHERE id=? AND status='pending'
            """,
            (short_alpaca_order_id, long_alpaca_order_id,
             short_contract_symbol, long_contract_symbol,
             entry_price, position_id),
        )
        # OMNI MAX_NEW_PER_DAY regression fix: daily_entries insert was in save_position()
        # which was deprecated by the TOCTOU fix. Without this insert, count_new_positions_today()
        # always returns 0 → MAX_NEW_PER_DAY gate is permanently dead.
        conn.execute(
            """
            INSERT OR IGNORE INTO daily_entries (entry_date, ticker, position_id, entered_at)
            VALUES (?, ?, ?, ?)
            """,
            (today, ticker, position_id, now),
        )
        conn.commit()


def cancel_pending_position(db_path: str, position_id: int) -> None:
    """
    Delete a pending reservation when Alpaca order placement fails.

    Ensures failed orders leave no DB trace — no phantom positions.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM positions WHERE id=? AND status='pending'",
            (position_id,),
        )
        conn.commit()


def count_new_positions_today(db_path: str) -> int:
    """Return count of new positions opened today."""
    today = date.today().isoformat()
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM daily_entries WHERE entry_date=?",
            (today,),
        ).fetchone()
    return row["cnt"] if row else 0


def _DEPRECATED_save_position_DO_NOT_USE(
    db_path:               str,
    ticker:                str,
    direction:             str,
    pathway:               str,
    option_type:           str,
    short_strike:          float,
    long_strike:           float,
    expiration_date:       str,
    dte_at_open:           int,
    contracts:             int,
    position_size_usd:     float,
    window_id:             Optional[str],
    agent_scores:          Optional[dict],
    verdict:               Optional[str],
    entry_price:           Optional[float] = None,
    alpaca_order_id:       Optional[str]   = None,
    short_contract_symbol: Optional[str]   = None,
    long_contract_symbol:  Optional[str]   = None,
) -> int:
    """
    DEPRECATED — DO NOT USE. Replaced by reserve_position_slot() + confirm_pending_position().

    OMNI Finding H4: This function bypasses the TOCTOU-safe PENDING record pattern
    (Cipher Findings 1+2). Any execution path that calls this function instead of
    reserve→confirm creates phantom positions and race conditions.

    Kept for DB tooling that may query historical records. The execute handler
    MUST NOT call this function. Use reserve_position_slot() + confirm_pending_position().
    """
    raise RuntimeError(
        "save_position() is deprecated. Use reserve_position_slot() + confirm_pending_position(). "
        "See OMNI Finding H4 / Cipher Findings 1+2."
    )
    now = datetime.now(timezone.utc).isoformat()
    today = date.today().isoformat()

    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO positions (
                ticker, direction, pathway, option_type,
                short_strike, long_strike, expiration_date, dte_at_open,
                contracts, entry_price, position_size_usd,
                short_alpaca_order_id, window_id, agent_scores, verdict,
                short_contract_symbol, long_contract_symbol, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker, direction, pathway, option_type,
                short_strike, long_strike, expiration_date, dte_at_open,
                contracts, entry_price, position_size_usd,
                alpaca_order_id, window_id,
                json.dumps(agent_scores) if agent_scores else None,
                verdict, short_contract_symbol, long_contract_symbol, now,
            ),
        )
        position_id = cursor.lastrowid

        # Record daily entry
        conn.execute(
            """
            INSERT OR IGNORE INTO daily_entries (entry_date, ticker, position_id, entered_at)
            VALUES (?, ?, ?, ?)
            """,
            (today, ticker, position_id, now),
        )

    return position_id


def get_open_positions(db_path: str) -> list[dict]:
    """Return all open positions."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY opened_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_position_by_id(db_path: str, position_id: int) -> Optional[dict]:
    """Return a single position by ID."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE id=?", (position_id,)
        ).fetchone()
    return dict(row) if row else None


def close_position(
    db_path:      str,
    position_id:  int,
    close_reason: str,
    pnl_pct:      float,
    pnl_usd:      float,
) -> None:
    """
    Mark a position as closed with P&L.

    Args:
        db_path:      Database path.
        position_id:  Position row ID.
        close_reason: Reason for closing (DTE_CLOSE, PROFIT_TARGET, etc.).
        pnl_pct:      P&L as decimal.
        pnl_usd:      P&L in dollars.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE positions
            SET status='closed', closed_at=?, close_reason=?, pnl_pct=?, pnl_usd=?
            WHERE id=?
            """,
            (now, close_reason, pnl_pct, pnl_usd, position_id),
        )


def update_alpaca_order_id(db_path: str, position_id: int, order_id: str) -> None:
    """
    Update the Alpaca order ID on a position after successful order placement.

    Args:
        db_path:     Database path.
        position_id: Position row ID.
        order_id:    Alpaca order ID string.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE positions SET short_alpaca_order_id=? WHERE id=?",
            (order_id, position_id),
        )


def update_trailing_stop(
    db_path:           str,
    position_id:       int,
    trailing_stop_pct: float,
    trailing_stop_high: float,
    partial_closed:    bool = False,
) -> None:
    """
    Update trailing stop parameters after partial exit trigger.

    Args:
        db_path:            Database path.
        position_id:        Position row ID.
        trailing_stop_pct:  New trailing stop percentage.
        trailing_stop_high: Highest P&L recorded since trigger.
        partial_closed:     True if partial close has been executed.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE positions
            SET trailing_stop_pct=?, trailing_stop_high=?, partial_closed=?
            WHERE id=?
            """,
            (trailing_stop_pct, trailing_stop_high, 1 if partial_closed else 0, position_id),
        )


def reduce_contracts(db_path: str, position_id: int, qty_closed: int) -> int:
    """
    Reduce the contract count after a partial close.

    Fix for adversarial finding #3: contracts field was never decremented after
    partial close, causing exit monitor to over-close on subsequent ticks.

    Args:
        db_path:     Database path.
        position_id: Position row ID.
        qty_closed:  Number of contracts that were closed.

    Returns:
        Remaining contract count after reduction (minimum 0).
    """
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE positions
            SET contracts = MAX(0, contracts - ?)
            WHERE id=?
            """,
            (qty_closed, position_id),
        )
        row = conn.execute(
            "SELECT contracts FROM positions WHERE id=?", (position_id,)
        ).fetchone()
    return row["contracts"] if row else 0


def set_long_alpaca_order_id(db_path: str, position_id: int, order_id: str) -> None:
    """
    Store the long-leg Alpaca order ID for spread P&L tracking.

    Args:
        db_path:     Database path.
        position_id: Position row ID.
        order_id:    Alpaca order ID for the long leg.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE positions SET long_alpaca_order_id=? WHERE id=?",
            (order_id, position_id),
        )
