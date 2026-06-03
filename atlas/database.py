"""
database.py — Cipher Agent SQLite Persistence

Tracks every pick and every analyzed window for audit and deduplication.
"""

import sqlite3
import logging
from datetime import datetime
from typing import Optional
import pytz

logger = logging.getLogger("atlas.database")
ET = pytz.timezone("America/New_York")


def init_db(db_path: str) -> None:
    """
    Initialize SQLite database with required tables.
    Safe to call on every startup — uses IF NOT EXISTS.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS picks (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                window_id        TEXT    NOT NULL,
                ticker           TEXT    NOT NULL,
                direction        TEXT    NOT NULL,
                score            REAL    NOT NULL,
                reasoning        TEXT,
                alpha_submitted  INTEGER NOT NULL DEFAULT 0,
                prime_submitted  INTEGER NOT NULL DEFAULT 0,
                created_at       TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS windows (
                window_id         TEXT    PRIMARY KEY,
                received_at       TEXT    NOT NULL,
                tickers_received  INTEGER NOT NULL DEFAULT 0,
                tickers_analyzed  INTEGER NOT NULL DEFAULT 0,
                tickers_submitted INTEGER NOT NULL DEFAULT 0,
                completed_at      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_picks_window ON picks(window_id);
            CREATE INDEX IF NOT EXISTS idx_picks_created ON picks(created_at);
        """)
        conn.commit()
        logger.info("Database initialized at %s", db_path)
    finally:
        conn.close()


def is_duplicate_window(db_path: str, window_id: str) -> bool:
    """
    Return True if this window_id has already been processed.
    Used to prevent double-analysis of the same Axiom pool push.
    """
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM windows WHERE window_id = ?", (window_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_window_received(db_path: str, window_id: str, ticker_count: int) -> None:
    """Register that a window was received (before analysis begins)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO windows (window_id, received_at, tickers_received)
               VALUES (?, ?, ?)""",
            (window_id, datetime.now(ET).isoformat(), ticker_count),
        )
        conn.commit()
    finally:
        conn.close()


def record_pick(
    db_path: str,
    window_id: str,
    ticker: str,
    direction: str,
    score: float,
    reasoning: str,
    alpha_submitted: bool,
    prime_submitted: bool,
) -> None:
    """Persist a pick that cleared the submission threshold."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO picks
               (window_id, ticker, direction, score, reasoning,
                alpha_submitted, prime_submitted, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                window_id, ticker, direction, score, reasoning,
                int(alpha_submitted), int(prime_submitted),
                datetime.now(ET).isoformat(),
            ),
        )
        conn.commit()
    except Exception as e:
        logger.error("Failed to record pick %s/%s: %s", ticker, direction, e)
    finally:
        conn.close()


def complete_window(
    db_path: str, window_id: str, analyzed: int, submitted: int
) -> None:
    """Mark a window as fully processed."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """UPDATE windows
               SET tickers_analyzed=?, tickers_submitted=?, completed_at=?
               WHERE window_id=?""",
            (analyzed, submitted, datetime.now(ET).isoformat(), window_id),
        )
        conn.commit()
    finally:
        conn.close()


def has_submitted_today(db_path: str, ticker: str, direction: str) -> bool:
    """Return True if this agent already submitted this ticker+direction today (ET)."""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM picks WHERE ticker=? AND direction=? AND created_at LIKE ? LIMIT 1",
            (ticker.upper(), direction.lower(), f"{today}%"),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_today_picks(db_path: str) -> list:
    """Return all picks submitted today (ET)."""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM picks WHERE created_at LIKE ? ORDER BY created_at DESC",
            (f"{today}%",),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_stats(db_path: str) -> dict:
    """Return aggregate statistics for the health endpoint."""
    conn = sqlite3.connect(db_path)
    try:
        total_picks = conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
        total_windows = conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
        today = datetime.now(ET).strftime("%Y-%m-%d")
        today_picks = conn.execute(
            "SELECT COUNT(*) FROM picks WHERE created_at LIKE ?", (f"{today}%",)
        ).fetchone()[0]
        return {
            "total_picks": total_picks,
            "total_windows": total_windows,
            "today_picks": today_picks,
        }
    finally:
        conn.close()
