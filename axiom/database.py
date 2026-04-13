"""
database.py — Axiom SQLite Database Layer

All database operations for the Axiom service.
WAL mode enabled on every connection for concurrent read safety.
Schema created on first run. Migrations are additive only.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Generator, Optional

logger = logging.getLogger("axiom.database")


def init_db(db_path: str) -> None:
    """
    Initialize the Axiom database — create all tables if they don't exist.
    Safe to call multiple times (idempotent).

    Args:
        db_path: Absolute path to the SQLite database file.

    Raises:
        RuntimeError: If database cannot be created or schema cannot be applied.
    """
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    try:
        with get_conn(db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS anchor_stocks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker      TEXT NOT NULL,
                    run_date    TEXT NOT NULL,
                    run_type    TEXT NOT NULL CHECK (run_type IN ('morning', 'midday')),
                    created_at  TEXT DEFAULT (datetime('now')),
                    UNIQUE(ticker, run_date, run_type)
                );

                CREATE TABLE IF NOT EXISTS pool_snapshots (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    window_id   TEXT NOT NULL,
                    tickers     TEXT NOT NULL,
                    regime      TEXT NOT NULL,
                    pool_size   INTEGER NOT NULL,
                    created_at  TEXT DEFAULT (datetime('now')),
                    UNIQUE(window_id)
                );

                CREATE TABLE IF NOT EXISTS risk_assessments (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker      TEXT NOT NULL,
                    window_id   TEXT NOT NULL,
                    risk_score  REAL NOT NULL,
                    sizing_mult REAL NOT NULL,
                    hard_stops  TEXT NOT NULL,
                    flags       TEXT NOT NULL,
                    raw_result  TEXT NOT NULL,
                    created_at  TEXT DEFAULT (datetime('now')),
                    UNIQUE(ticker, window_id)
                );

                CREATE TABLE IF NOT EXISTS agent_push_log (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    window_id    TEXT NOT NULL,
                    agent        TEXT NOT NULL,
                    status       TEXT NOT NULL CHECK (status IN ('success', 'timeout', 'error')),
                    response_ms  INTEGER,
                    created_at   TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS agent_health (
                    agent               TEXT PRIMARY KEY,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_success_at     TEXT,
                    last_failure_at     TEXT,
                    status              TEXT NOT NULL DEFAULT 'unknown'
                                        CHECK (status IN ('healthy', 'degraded', 'down', 'unknown')),
                    updated_at          TEXT DEFAULT (datetime('now'))
                );
            """)
            conn.commit()
        logger.info("Axiom database initialized at %s", db_path)
    except Exception as e:
        raise RuntimeError(f"Axiom database initialization failed: {e}") from e


@contextmanager
def get_conn(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager for SQLite connections with WAL mode enabled.

    Args:
        db_path: Path to the SQLite database file.

    Yields:
        sqlite3.Connection with WAL mode and row_factory set.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ── Pool Snapshots ────────────────────────────────────────────────────────────

def save_pool_snapshot(
    db_path: str,
    window_id: str,
    tickers: list[str],
    regime: dict,
) -> None:
    """
    Persist a pool snapshot to the database.

    Args:
        db_path:   Path to the SQLite database.
        window_id: 15-minute window identifier (e.g. '2026-04-10-1415').
        tickers:   List of ticker symbols in the pool.
        regime:    Regime dict with VIX, classification, and allowed flags.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO pool_snapshots (window_id, tickers, regime, pool_size)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(window_id) DO UPDATE SET
                tickers=excluded.tickers,
                regime=excluded.regime,
                pool_size=excluded.pool_size
            """,
            (window_id, json.dumps(tickers), json.dumps(regime), len(tickers)),
        )
        conn.commit()


def load_last_pool(db_path: str) -> Optional[dict]:
    """
    Load the most recent pool snapshot from the database.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        Dict with tickers and regime, or None if no snapshot exists.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT window_id, tickers, regime, pool_size FROM pool_snapshots "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    if row is None:
        return None

    return {
        "window_id": row["window_id"],
        "tickers":   json.loads(row["tickers"]),
        "regime":    json.loads(row["regime"]),
        "pool_size": row["pool_size"],
    }


# ── Anchor Stocks ─────────────────────────────────────────────────────────────

def save_anchor_stocks(
    db_path: str,
    tickers: list[str],
    run_date: str,
    run_type: str,
) -> None:
    """
    Persist Tier 1 anchor stocks for a given run.

    Args:
        db_path:  Path to the SQLite database.
        tickers:  List of ticker symbols that passed Tier 1.
        run_date: Date string (YYYY-MM-DD).
        run_type: Either 'morning' or 'midday'.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM anchor_stocks WHERE run_date=? AND run_type=?",
            (run_date, run_type),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO anchor_stocks (ticker, run_date, run_type) VALUES (?, ?, ?)",
            [(t, run_date, run_type) for t in tickers],
        )
        conn.commit()


def load_anchor_stocks(db_path: str, run_date: str) -> list[str]:
    """
    Load the most recent anchor stocks for a given date.

    Args:
        db_path:  Path to the SQLite database.
        run_date: Date string (YYYY-MM-DD).

    Returns:
        List of ticker symbols, or empty list if none found.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM anchor_stocks WHERE run_date=? "
            "ORDER BY ticker",
            (run_date,),
        ).fetchall()
    return [row["ticker"] for row in rows]


# ── Agent Push Log ────────────────────────────────────────────────────────────

def log_agent_push(
    db_path: str,
    window_id: str,
    agent: str,
    status: str,
    response_ms: Optional[int] = None,
) -> None:
    """
    Log the result of an agent webhook push.

    Args:
        db_path:     Path to the SQLite database.
        window_id:   Window ID of this push.
        agent:       Agent name ('Cipher', 'Sage', 'Atlas').
        status:      'success', 'timeout', or 'error'.
        response_ms: Response time in milliseconds, or None.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO agent_push_log (window_id, agent, status, response_ms) VALUES (?,?,?,?)",
            (window_id, agent, status, response_ms),
        )
        conn.commit()


def update_agent_health(
    db_path: str,
    agent: str,
    success: bool,
) -> int:
    """
    Update agent health tracking. Returns current consecutive failure count.

    Args:
        db_path: Path to the SQLite database.
        agent:   Agent name.
        success: True if push succeeded, False if failed.

    Returns:
        Current consecutive failure count after this update.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    with get_conn(db_path) as conn:
        if success:
            conn.execute(
                """
                INSERT INTO agent_health (agent, consecutive_failures, last_success_at, status)
                VALUES (?, 0, ?, 'healthy')
                ON CONFLICT(agent) DO UPDATE SET
                    consecutive_failures=0,
                    last_success_at=?,
                    status='healthy',
                    updated_at=?
                """,
                (agent, now, now, now),
            )
            conn.commit()
            return 0
        else:
            conn.execute(
                """
                INSERT INTO agent_health (agent, consecutive_failures, last_failure_at, status)
                VALUES (?, 1, ?, 'degraded')
                ON CONFLICT(agent) DO UPDATE SET
                    consecutive_failures=consecutive_failures + 1,
                    last_failure_at=?,
                    status=CASE WHEN consecutive_failures + 1 >= 3 THEN 'down' ELSE 'degraded' END,
                    updated_at=?
                """,
                (agent, now, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT consecutive_failures FROM agent_health WHERE agent=?", (agent,)
            ).fetchone()
            return row["consecutive_failures"] if row else 1


def get_agent_health(db_path: str) -> dict[str, dict]:
    """
    Get health status for all agents.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        Dict mapping agent name to health info dict.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM agent_health").fetchall()
    return {
        row["agent"]: {
            "status":               row["status"],
            "consecutive_failures": row["consecutive_failures"],
            "last_success_at":      row["last_success_at"],
            "last_failure_at":      row["last_failure_at"],
        }
        for row in rows
    }


# ── Risk Assessment Cache ─────────────────────────────────────────────────────

def save_risk_assessment(
    db_path: str,
    ticker: str,
    window_id: str,
    risk_score: float,
    sizing_mult: float,
    hard_stops: list[str],
    flags: list[str],
    raw_result: dict,
) -> None:
    """
    Cache a risk assessment result for a ticker/window pair.

    Args:
        db_path:     Path to the SQLite database.
        ticker:      Stock ticker symbol.
        window_id:   Window ID when assessment was run.
        risk_score:  Risk score 0-10 (10 = max risk).
        sizing_mult: Position sizing multiplier (0.0 - 1.0).
        hard_stops:  List of hard stop reason strings.
        flags:       List of critical flag strings.
        raw_result:  Full assessment result dict.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO risk_assessments
                (ticker, window_id, risk_score, sizing_mult, hard_stops, flags, raw_result)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, window_id) DO UPDATE SET
                risk_score=excluded.risk_score,
                sizing_mult=excluded.sizing_mult,
                hard_stops=excluded.hard_stops,
                flags=excluded.flags,
                raw_result=excluded.raw_result
            """,
            (
                ticker, window_id, risk_score, sizing_mult,
                json.dumps(hard_stops), json.dumps(flags), json.dumps(raw_result),
            ),
        )
        conn.commit()
