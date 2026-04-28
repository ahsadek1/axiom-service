"""
database.py — Prime Execution SQLite Layer

Tracks open swing positions, daily entries, and reconciliation state.
WAL mode. All paths are permanent storage — no /tmp.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Generator, Optional

logger = logging.getLogger("prime_exec.database")


@contextmanager
def get_conn(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """
    Open a WAL-mode SQLite connection with retry on locked errors.

    Adversarial fix #1: the previous version had no retry on OperationalError
    (database is locked). Under concurrent access the scheduler and HTTP handler
    can both hit the DB simultaneously. Three attempts with 0.5 s backoff before
    raising ensures transient lock contention doesn't silently drop DB writes.
    """
    import time
    conn = None
    last_err: Exception = RuntimeError("get_conn: no attempts made")
    for attempt in range(3):
        try:
            conn = sqlite3.connect(db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            break
        except sqlite3.OperationalError as exc:
            last_err = exc
            logger.warning("DB connect attempt %d failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(0.5)
    else:
        raise last_err

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
    Initialize the Prime Execution database schema.

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
                shares                REAL    NOT NULL,
                entry_price           REAL,
                position_size_usd     REAL    NOT NULL,
                alpaca_order_id       TEXT,
                status                TEXT    NOT NULL DEFAULT 'open',
                partial_closed        INTEGER NOT NULL DEFAULT 0,
                shares_remaining      REAL,
                trailing_stop_pct     REAL,
                trailing_stop_high    REAL,
                hard_backstop_pct     REAL    NOT NULL DEFAULT -0.18,
                technical_stop_flagged INTEGER NOT NULL DEFAULT 0,
                window_id             TEXT,
                agent_scores          TEXT,
                verdict               TEXT,
                opened_at             TEXT    NOT NULL,
                closed_at             TEXT,
                close_reason          TEXT,
                pnl_pct               REAL,
                pnl_usd               REAL
            );

            CREATE INDEX IF NOT EXISTS idx_prime_positions_status
                ON positions(status, ticker);

            CREATE TABLE IF NOT EXISTS daily_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_date  TEXT    NOT NULL,
                ticker      TEXT    NOT NULL,
                position_id INTEGER,
                entered_at  TEXT    NOT NULL,
                UNIQUE(entry_date, ticker)
            );

            CREATE TABLE IF NOT EXISTS reconciliation_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at           TEXT    NOT NULL,
                positions_db     INTEGER NOT NULL,
                positions_alpaca INTEGER NOT NULL,
                mismatches       INTEGER NOT NULL DEFAULT 0,
                mismatch_details TEXT,
                execution_paused INTEGER NOT NULL DEFAULT 0
            );
        """)
    logger.info("Prime Execution database initialized at %s", db_path)


def count_open_positions(db_path: str) -> int:
    """Return count of open + pending positions.

    Cipher Finding 1+2 fix (TOCTOU): include 'pending' records so a reservation
    written inside the lock is visible to the next concurrent request.
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
    shares:            float,
    position_size_usd: float,
    window_id:         str,
    agent_scores:      str,
    verdict:           str,
) -> int:
    """
    Write a status='pending' placeholder record inside the position limit lock.

    Cipher Finding 1+2 fix: reserves the slot before releasing the lock.
    Returns the new position row ID.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO positions (
                ticker, direction, pathway, shares, position_size_usd,
                window_id, agent_scores, verdict, status, opened_at,
                hard_backstop_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, -0.18)
            """,
            (ticker, direction, pathway, shares, position_size_usd,
             window_id, agent_scores, verdict, now),
        )
        conn.commit()
        pending_id = cursor.lastrowid
        if not pending_id:
            row = conn.execute(
                "SELECT id FROM positions WHERE ticker=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            pending_id = row["id"] if row else None
    return pending_id


def confirm_pending_position(
    db_path:         str,
    position_id:     int,
    alpaca_order_id: str,
    entry_price:     float,
    shares_remaining: float,
) -> None:
    """Promote a pending reservation to live open position after Alpaca confirms.

    OMNI MAX_NEW_PER_DAY regression fix: also inserts into daily_entries so
    count_new_positions_today() returns correct counts. This insert was in
    save_position() which was deprecated by the TOCTOU fix — without it,
    MAX_NEW_PER_DAY gate was permanently dead (always 0).
    """
    now   = datetime.now(timezone.utc).isoformat()
    today = date.today().isoformat()
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT ticker FROM positions WHERE id=? AND status='pending'", (position_id,)
        ).fetchone()
        if not row:
            logger.warning("confirm_pending_position: no pending record for id=%d", position_id)
            return
        ticker = row["ticker"]

        conn.execute(
            """
            UPDATE positions
            SET status='open', alpaca_order_id=?, entry_price=?, shares_remaining=?
            WHERE id=? AND status='pending'
            """,
            (alpaca_order_id, entry_price, shares_remaining, position_id),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO daily_entries (entry_date, ticker, position_id, entered_at)
            VALUES (?, ?, ?, ?)
            """,
            (today, ticker, position_id, now),
        )
        conn.commit()


def cancel_pending_position(db_path: str, position_id: int) -> None:
    """Delete a pending reservation when Alpaca order fails — no phantom positions."""
    with get_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM positions WHERE id=? AND status='pending'",
            (position_id,),
        )
        conn.commit()


def count_new_positions_today(db_path: str) -> int:
    today = date.today().isoformat()
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM daily_entries WHERE entry_date=?",
            (today,),
        ).fetchone()
    return row["cnt"] if row else 0


def save_position(
    db_path:          str,
    ticker:           str,
    direction:        str,
    pathway:          str,
    shares:           float,
    position_size_usd: float,
    window_id:        Optional[str],
    agent_scores:     Optional[dict],
    verdict:          Optional[str],
    entry_price:      Optional[float] = None,
    alpaca_order_id:  Optional[str]   = None,
) -> int:
    """
    Persist a new open Prime position.

    Returns:
        Row ID of the inserted position.
    """
    now   = datetime.now(timezone.utc).isoformat()
    today = date.today().isoformat()

    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO positions (
                ticker, direction, pathway, shares, entry_price,
                position_size_usd, alpaca_order_id, window_id,
                agent_scores, verdict, shares_remaining, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker, direction, pathway, shares, entry_price,
                position_size_usd, alpaca_order_id, window_id,
                json.dumps(agent_scores) if agent_scores else None,
                verdict, shares, now,
            ),
        )
        position_id = cursor.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO daily_entries (entry_date, ticker, position_id, entered_at) VALUES (?, ?, ?, ?)",
            (today, ticker, position_id, now),
        )
    return position_id


def get_open_positions(db_path: str) -> list[dict]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY opened_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_position_by_id(db_path: str, position_id: int) -> Optional[dict]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE id=?", (position_id,)
        ).fetchone()
    return dict(row) if row else None


def close_position(db_path: str, position_id: int, reason: str, pnl_pct: float, pnl_usd: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, close_reason=?, pnl_pct=?, pnl_usd=? WHERE id=?",
            (now, reason, pnl_pct, pnl_usd, position_id),
        )


def close_stale_position(db_path: str, position_id: int, reason: str) -> None:
    """
    Close a position without PnL data (used by startup reconciler for stale/leaked entries).
    Sets pnl_pct=0, pnl_usd=0 since reconciled positions may not have exit prices.

    Added 2026-04-28 by OMNI — was called from main.py startup reconciliation
    but missing from database.py.
    """
    close_position(db_path, position_id, reason, pnl_pct=0.0, pnl_usd=0.0)


def update_alpaca_order_id(db_path: str, position_id: int, order_id: str) -> None:
    """
    Update the Alpaca order ID on a pending position after successful placement.

    Args:
        db_path:     Database path.
        position_id: Position row ID.
        order_id:    Alpaca order ID string.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE positions SET alpaca_order_id=? WHERE id=?",
            (order_id, position_id),
        )


def update_trailing_stop(
    db_path: str, position_id: int,
    trail_pct: float, trail_high: float,
    partial_closed: bool = False,
    shares_remaining: Optional[float] = None,
) -> None:
    updates = "trailing_stop_pct=?, trailing_stop_high=?, partial_closed=?"
    values  = [trail_pct, trail_high, 1 if partial_closed else 0]
    if shares_remaining is not None:
        updates += ", shares_remaining=?"
        values.append(shares_remaining)
    values.append(position_id)
    with get_conn(db_path) as conn:
        conn.execute(f"UPDATE positions SET {updates} WHERE id=?", values)


def flag_technical_stop(db_path: str, position_id: int) -> None:
    """Mark a position as technically invalidated (primary stop for Prime)."""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE positions SET technical_stop_flagged=1 WHERE id=?",
            (position_id,),
        )


def log_reconciliation(
    db_path:          str,
    positions_db:     int,
    positions_alpaca: int,
    mismatches:       int,
    mismatch_details: Optional[str],
    execution_paused: bool,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO reconciliation_log
                (run_at, positions_db, positions_alpaca, mismatches,
                 mismatch_details, execution_paused)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now, positions_db, positions_alpaca, mismatches,
             mismatch_details, 1 if execution_paused else 0),
        )
