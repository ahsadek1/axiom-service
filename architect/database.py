"""
database.py — ARCHITECT Agent Local SQLite Persistence

Tracks review state and caches CHRONICLE writes on failure.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("architect.database")


def init_db(db_path: str) -> None:
    """
    Initialize local SQLite with required tables.
    Safe to call on every startup — uses IF NOT EXISTS.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS reviews (
                review_cycle_id     TEXT NOT NULL,
                service_name        TEXT NOT NULL,
                service_version     TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'IN_PROGRESS',
                overall_assessment  TEXT,
                p0_count            INTEGER DEFAULT 0,
                p1_count            INTEGER DEFAULT 0,
                p2_count            INTEGER DEFAULT 0,
                p3_count            INTEGER DEFAULT 0,
                confidence_level    TEXT,
                chronicle_id        INTEGER,
                started_at          TEXT NOT NULL,
                completed_at        TEXT,
                PRIMARY KEY (review_cycle_id)
            );

            CREATE TABLE IF NOT EXISTS chronicle_cache (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                op                  TEXT NOT NULL,
                chronicle_id        INTEGER,
                service_name        TEXT,
                service_version     TEXT,
                review_cycle_id     TEXT,
                reviewer_agent      TEXT,
                reviewer_brain      TEXT,
                overall_assessment  TEXT,
                p0_count            INTEGER,
                p1_count            INTEGER,
                p2_count            INTEGER,
                p3_count            INTEGER,
                full_report         TEXT,
                confidence_level    TEXT,
                started_at          TEXT,
                completed_at        TEXT,
                queued_at           TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_stats (
                date                TEXT PRIMARY KEY,
                reviews_completed   INTEGER DEFAULT 0,
                brain_failures      INTEGER DEFAULT 0,
                last_review_at      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_reviews_cycle ON reviews(review_cycle_id);
            CREATE INDEX IF NOT EXISTS idx_cache_queued ON chronicle_cache(queued_at);
        """)
        conn.commit()
        logger.info("ARCHITECT database initialized at %s", db_path)
    finally:
        conn.close()


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def upsert_review(
    db_path: str,
    review_cycle_id: str,
    service_name: str,
    service_version: str,
    chronicle_id: int,
) -> None:
    """Record a new review as IN_PROGRESS."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO reviews
               (review_cycle_id, service_name, service_version,
                status, chronicle_id, started_at)
               VALUES (?, ?, ?, 'IN_PROGRESS', ?, ?)""",
            (review_cycle_id, service_name, service_version, chronicle_id, _now_utc()),
        )
        conn.commit()
    finally:
        conn.close()


def complete_review(
    db_path: str,
    review_cycle_id: str,
    overall_assessment: str,
    p0_count: int,
    p1_count: int,
    p2_count: int,
    p3_count: int,
    confidence_level: str,
) -> None:
    """Mark review as completed with findings summary."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """UPDATE reviews SET
               status='COMPLETED',
               overall_assessment=?,
               p0_count=?, p1_count=?, p2_count=?, p3_count=?,
               confidence_level=?,
               completed_at=?
               WHERE review_cycle_id=?""",
            (overall_assessment, p0_count, p1_count, p2_count, p3_count,
             confidence_level, _now_utc(), review_cycle_id),
        )
        conn.commit()
        _increment_stat(db_path, "reviews_completed", review_at=_now_utc())
    finally:
        conn.close()


def fail_review(db_path: str, review_cycle_id: str) -> None:
    """Mark review as FAILED."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE reviews SET status='FAILED', completed_at=? WHERE review_cycle_id=?",
            (_now_utc(), review_cycle_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_review(db_path: str, review_cycle_id: str) -> Optional[dict]:
    """Return review state dict or None."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM reviews WHERE review_cycle_id=?", (review_cycle_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def record_brain_failure(db_path: str) -> None:
    """Increment brain failure counter for today."""
    _increment_stat(db_path, "brain_failures")


def _increment_stat(db_path: str, field: str, review_at: Optional[str] = None) -> None:
    today = _today()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO agent_stats (date) VALUES (?)", (today,)
        )
        if review_at:
            conn.execute(
                f"UPDATE agent_stats SET {field}={field}+1, last_review_at=? WHERE date=?",
                (review_at, today),
            )
        else:
            conn.execute(
                f"UPDATE agent_stats SET {field}={field}+1 WHERE date=?", (today,)
            )
        conn.commit()
    finally:
        conn.close()


def get_today_stats(db_path: str) -> dict:
    """Return today's agent statistics."""
    today = _today()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM agent_stats WHERE date=?", (today,)
        ).fetchone()
        if row:
            return dict(row)
        return {
            "date": today, "reviews_completed": 0,
            "brain_failures": 0, "last_review_at": None,
        }
    finally:
        conn.close()
