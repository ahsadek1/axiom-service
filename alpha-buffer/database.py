"""
database.py — Alpha Buffer SQLite Layer

All DB operations for submissions, concordance results, and circuit breaker state.
WAL mode on every connection. Foreign keys enforced.
No silent failures — every exception propagates to caller.
"""

import json
import logging
import os
import sys
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Generator, Optional

logger = logging.getLogger("alpha_buffer.database")


# ── Connection (SQLite + Postgres via shared adapter) ─────────────────────────

# Prefer the shared pg_adapter when available; fall back to SQLite-only.
try:
    _shared_path = os.path.join(os.path.dirname(__file__), "..", "shared")
    if _shared_path not in sys.path:
        sys.path.insert(0, _shared_path)
    from pg_adapter import get_conn as _pg_get_conn  # type: ignore
    _PG_ADAPTER_AVAILABLE = True
    logger.debug("pg_adapter loaded — SQLite/Postgres unified backend active")
except ImportError:
    _PG_ADAPTER_AVAILABLE = False
    logger.debug("pg_adapter not found — using SQLite-only backend")


@contextmanager
def get_conn(db_path: str) -> Generator:
    """
    Context manager yielding a database connection.

    Routes to Postgres when DATABASE_URL is set (Railway), SQLite otherwise.
    The yielded object has a consistent sqlite3-like interface in both cases.

    Args:
        db_path: Path to the SQLite database file (ignored when using Postgres).

    Yields:
        Connection with WAL mode (SQLite) or autocommit-off (Postgres).
    """
    if _PG_ADAPTER_AVAILABLE:
        with _pg_get_conn(db_path) as conn:
            yield conn
    else:
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


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db(db_path: str) -> None:
    """
    Initialize the Alpha Buffer database schema.

    Idempotent — safe to call multiple times. Creates all tables if absent.

    Args:
        db_path: Path to the SQLite database file (created if absent).
    """
    with get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS submissions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                window_id     TEXT    NOT NULL,
                agent         TEXT    NOT NULL,
                ticker        TEXT    NOT NULL,
                direction     TEXT    NOT NULL,  -- 'bullish' or 'bearish'
                score         REAL    NOT NULL,
                reasoning     TEXT,
                received_at   TEXT    NOT NULL,
                UNIQUE(window_id, agent, ticker, direction)
            );

            CREATE INDEX IF NOT EXISTS idx_submissions_window_ticker
                ON submissions(window_id, ticker, direction);

            CREATE TABLE IF NOT EXISTS concordance_results (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                window_id       TEXT    NOT NULL,
                ticker          TEXT    NOT NULL,
                direction       TEXT    NOT NULL,
                pathway         TEXT    NOT NULL,   -- P1, P2, P3
                agent_count     INTEGER NOT NULL,
                weighted_score  REAL    NOT NULL,
                sizing_mult     REAL    NOT NULL,
                verdict         TEXT    NOT NULL,   -- GO, STRONG_GO
                agents_involved TEXT    NOT NULL,   -- JSON array
                scores          TEXT    NOT NULL,   -- JSON dict
                omni_dispatched   INTEGER NOT NULL DEFAULT 0,
                omni_response     TEXT,
                dispatch_attempts INTEGER NOT NULL DEFAULT 0,
                created_at        TEXT    NOT NULL,
                UNIQUE(window_id, ticker, direction)
            );

            CREATE TABLE IF NOT EXISTS circuit_breaker_state (
                id                    INTEGER PRIMARY KEY CHECK (id = 1),
                status                TEXT    NOT NULL DEFAULT 'NORMAL',
                consecutive_losses    INTEGER NOT NULL DEFAULT 0,
                daily_trades          INTEGER NOT NULL DEFAULT 0,
                daily_wins            INTEGER NOT NULL DEFAULT 0,
                daily_losses          INTEGER NOT NULL DEFAULT 0,
                daily_pnl_pct         REAL    NOT NULL DEFAULT 0.0,
                weekly_losses         INTEGER NOT NULL DEFAULT 0,
                weekly_pnl_pct        REAL    NOT NULL DEFAULT 0.0,
                portfolio_pnl_pct     REAL    NOT NULL DEFAULT 0.0,
                last_trade_date       TEXT,
                last_weekly_reset     TEXT,
                last_updated          TEXT    NOT NULL,
                manual_override       INTEGER NOT NULL DEFAULT 0,
                manual_override_note  TEXT
            );

            CREATE TABLE IF NOT EXISTS trade_outcomes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker       TEXT    NOT NULL,
                direction    TEXT    NOT NULL,
                pathway      TEXT    NOT NULL,
                entry_date   TEXT    NOT NULL,
                exit_date    TEXT,
                pnl_pct      REAL,
                won          INTEGER,   -- 1=win, 0=loss, NULL=open
                window_id    TEXT,
                recorded_at  TEXT    NOT NULL
            );
        """)

    # OMNI H2 fix: add dispatch_attempts column to existing DBs (migration-safe)
    with get_conn(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(concordance_results)").fetchall()}
        if "dispatch_attempts" not in cols:
            conn.execute("ALTER TABLE concordance_results ADD COLUMN dispatch_attempts INTEGER NOT NULL DEFAULT 0")
            conn.commit()
            logger.info("Migrated concordance_results: added dispatch_attempts column")

    # OMNI C1 fix: add last_weekly_reset column to existing DBs (migration-safe)
    with get_conn(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(circuit_breaker_state)").fetchall()}
        if "last_weekly_reset" not in cols:
            conn.execute("ALTER TABLE circuit_breaker_state ADD COLUMN last_weekly_reset TEXT")
            conn.commit()
            logger.info("Migrated circuit_breaker_state: added last_weekly_reset column")

    logger.info("Alpha Buffer database initialized at %s", db_path)


# ── Submissions ───────────────────────────────────────────────────────────────

def save_submission(
    db_path:   str,
    window_id: str,
    agent:     str,
    ticker:    str,
    direction: str,
    score:     float,
    reasoning: Optional[str],
) -> int:
    """
    Persist an agent submission. Upserts on duplicate (window, agent, ticker, direction).

    Args:
        db_path:   Path to database.
        window_id: 15-minute window ID.
        agent:     Agent name (Cipher/Atlas/Sage).
        ticker:    Stock ticker symbol.
        direction: 'bullish' or 'bearish'.
        score:     Agent's conviction score 0-100.
        reasoning: Optional reasoning text.

    Returns:
        Row ID of the inserted/updated row.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO submissions
                (window_id, agent, ticker, direction, score, reasoning, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(window_id, agent, ticker, direction)
            DO UPDATE SET
                score       = excluded.score,
                reasoning   = excluded.reasoning,
                received_at = excluded.received_at
            """,
            (window_id, agent, ticker, direction, score, reasoning, now),
        )
        return cursor.lastrowid


def get_window_submissions(
    db_path:   str,
    window_id: str,
    ticker:    str,
    direction: str,
) -> list[dict]:
    """
    Get all submissions for a specific ticker/direction/window.

    Args:
        db_path:   Path to database.
        window_id: 15-minute window ID.
        ticker:    Stock ticker symbol.
        direction: 'bullish' or 'bearish'.

    Returns:
        List of submission rows as dicts.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT agent, score, reasoning, received_at
            FROM submissions
            WHERE window_id=? AND ticker=? AND direction=?
            ORDER BY received_at ASC
            """,
            (window_id, ticker, direction),
        ).fetchall()
    return [dict(r) for r in rows]


def is_already_submitted_today(
    db_path:   str,
    agent:     str,
    ticker:    str,
    direction: str,
    et_date:   Optional[str] = None,
) -> bool:
    """
    Check if the same agent already submitted this ticker+direction today (any window).

    Prevents the same agent from submitting TSLA bullish 7 times in 7 different windows
    and flooding the concordance system with duplicate weight.

    Uses the window_id date (YYYY-MM-DD prefix, ET-based) as the day boundary — NOT the
    UTC timestamp. This prevents false deduplication when a late-night window (e.g.
    2026-04-14-2215 at 10:15 PM ET) rolls past midnight UTC and creates a UTC April 15
    record that would block the real April 15 ET trading session.

    Args:
        db_path:   Path to database.
        agent:     Agent name (Cipher/Atlas/Sage).
        ticker:    Stock ticker symbol.
        direction: 'bullish' or 'bearish'.
        et_date:   Optional YYYY-MM-DD ET date override (for testing). Defaults to today in ET.

    Returns:
        True if a submission already exists today for this agent+ticker+direction.
    """
    from zoneinfo import ZoneInfo
    if et_date is None:
        et_date = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    with get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM submissions
            WHERE agent = ?
              AND ticker = ?
              AND direction = ?
              AND SUBSTR(window_id, 1, 10) = ?
            LIMIT 1
            """,
            (agent, ticker, direction, et_date),
        ).fetchone()
    return row is not None


def get_all_tickers_in_window(db_path: str, window_id: str) -> list[tuple[str, str]]:
    """
    Get all (ticker, direction) pairs with submissions in a window.

    Args:
        db_path:   Path to database.
        window_id: 15-minute window ID.

    Returns:
        List of (ticker, direction) tuples with at least one submission.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ticker, direction
            FROM submissions
            WHERE window_id=?
            """,
            (window_id,),
        ).fetchall()
    return [(r["ticker"], r["direction"]) for r in rows]


# ── Concordance Results ───────────────────────────────────────────────────────

def save_concordance_result(
    db_path:        str,
    window_id:      str,
    ticker:         str,
    direction:      str,
    pathway:        str,
    agent_count:    int,
    weighted_score: float,
    sizing_mult:    float,
    verdict:        str,
    agents_involved: list[str],
    scores:         dict[str, float],
) -> int:
    """
    Persist a concordance result. Upserts on duplicate (window, ticker, direction).

    Args:
        db_path:         Path to database.
        window_id:       15-minute window ID.
        ticker:          Stock ticker symbol.
        direction:       'bullish' or 'bearish'.
        pathway:         'P1', 'P2', or 'P3'.
        agent_count:     Number of agents in agreement.
        weighted_score:  Calculated weighted conviction score.
        sizing_mult:     Position size multiplier.
        verdict:         'GO' or 'STRONG_GO'.
        agents_involved: List of agent names.
        scores:          Dict mapping agent name to score.

    Returns:
        Row ID of the inserted/updated row.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO concordance_results
                (window_id, ticker, direction, pathway, agent_count, weighted_score,
                 sizing_mult, verdict, agents_involved, scores, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(window_id, ticker, direction)
            DO UPDATE SET
                pathway         = excluded.pathway,
                agent_count     = excluded.agent_count,
                weighted_score  = excluded.weighted_score,
                sizing_mult     = excluded.sizing_mult,
                verdict         = excluded.verdict,
                agents_involved = excluded.agents_involved,
                scores          = excluded.scores,
                created_at      = excluded.created_at
            """,
            (
                window_id, ticker, direction, pathway, agent_count, weighted_score,
                sizing_mult, verdict,
                json.dumps(agents_involved), json.dumps(scores), now,
            ),
        )
        return cursor.lastrowid


def mark_omni_dispatched(
    db_path:       str,
    window_id:     str,
    ticker:        str,
    direction:     str,
    omni_response: Optional[str] = None,
) -> None:
    """
    Mark a concordance result as dispatched to OMNI.

    Args:
        db_path:       Path to database.
        window_id:     15-minute window ID.
        ticker:        Stock ticker symbol.
        direction:     'bullish' or 'bearish'.
        omni_response: Optional OMNI response summary for audit.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE concordance_results
            SET omni_dispatched=1, omni_response=?
            WHERE window_id=? AND ticker=? AND direction=?
            """,
            (omni_response, window_id, ticker, direction),
        )


def get_undispatched_concordances(
    db_path:       str,
    older_than_s:  int = 30,
    max_attempts:  int = 3,
) -> list[dict]:
    """
    Return concordances that failed OMNI dispatch and are eligible for retry.

    OMNI H2 fix: concordances with omni_dispatched=0 were permanently abandoned.
    This query feeds the background retry loop.

    Args:
        db_path:      Path to database.
        older_than_s: Only return records older than this many seconds (debounce).
        max_attempts: Skip records that have already been attempted this many times.

    Returns:
        List of concordance dicts eligible for retry.
    """
    cutoff = (datetime.now(timezone.utc).timestamp() - older_than_s)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM concordance_results
            WHERE omni_dispatched=0
              AND created_at < ?
              AND dispatch_attempts < ?
            ORDER BY created_at ASC
            """,
            (cutoff_iso, max_attempts),
        ).fetchall()
    return [dict(r) for r in rows]


def increment_dispatch_attempts(
    db_path:   str,
    window_id: str,
    ticker:    str,
    direction: str,
) -> None:
    """Increment the dispatch_attempts counter after a failed retry."""
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE concordance_results
            SET dispatch_attempts = dispatch_attempts + 1
            WHERE window_id=? AND ticker=? AND direction=?
            """,
            (window_id, ticker, direction),
        )


def get_concordance_result(
    db_path:   str,
    window_id: str,
    ticker:    str,
    direction: str,
) -> Optional[dict]:
    """
    Retrieve a concordance result by window/ticker/direction.

    Returns None if not found.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM concordance_results
            WHERE window_id=? AND ticker=? AND direction=?
            """,
            (window_id, ticker, direction),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["agents_involved"] = json.loads(d["agents_involved"])
    d["scores"]          = json.loads(d["scores"])
    return d


# ── Circuit Breaker ───────────────────────────────────────────────────────────

def init_circuit_breaker(db_path: str) -> None:
    """
    Ensure a single circuit breaker state row exists (id=1).

    Safe to call multiple times.

    Args:
        db_path: Path to database.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO circuit_breaker_state
                (id, status, last_updated)
            VALUES (1, 'NORMAL', ?)
            """,
            (now,),
        )


def get_circuit_breaker_state(db_path: str) -> dict:
    """
    Get the current circuit breaker state.

    Args:
        db_path: Path to database.

    Returns:
        Circuit breaker state dict. Always returns a valid dict.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM circuit_breaker_state WHERE id=1"
        ).fetchone()
    return dict(row) if row else {"status": "NORMAL", "consecutive_losses": 0}


def update_circuit_breaker_state(db_path: str, updates: dict) -> None:
    """
    Update circuit breaker state fields. Only updates provided fields.

    Args:
        db_path:  Path to database.
        updates:  Dict of field → value pairs to update.
    """
    if not updates:
        return

    now = datetime.now(timezone.utc).isoformat()
    updates["last_updated"] = now

    set_clause = ", ".join(f"{k}=?" for k in updates)
    values     = list(updates.values())
    # WHERE id=1 is a literal — do NOT append 1 to values

    with get_conn(db_path) as conn:
        conn.execute(
            f"UPDATE circuit_breaker_state SET {set_clause} WHERE id=1",
            values,
        )


# ── Trade Outcomes ────────────────────────────────────────────────────────────

def record_trade_outcome(
    db_path:   str,
    ticker:    str,
    direction: str,
    pathway:   str,
    entry_date: str,
    pnl_pct:   Optional[float] = None,
    won:       Optional[int]   = None,
    exit_date: Optional[str]   = None,
    window_id: Optional[str]   = None,
) -> int:
    """
    Record a trade outcome for performance tracking.

    Args:
        db_path:    Path to database.
        ticker:     Stock ticker symbol.
        direction:  'bullish' or 'bearish'.
        pathway:    Trade pathway (P1/P2/P3).
        entry_date: ISO date string.
        pnl_pct:    P&L as decimal (0.35 = +35%).
        won:        1 if win, 0 if loss, None if open.
        exit_date:  ISO date when closed.
        window_id:  Concordance window ID.

    Returns:
        Row ID of inserted record.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO trade_outcomes
                (ticker, direction, pathway, entry_date, exit_date,
                 pnl_pct, won, window_id, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticker, direction, pathway, entry_date, exit_date,
             pnl_pct, won, window_id, now),
        )
        return cursor.lastrowid
