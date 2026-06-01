"""
resilience/regen_throttle.py — Daily Brief Regen Throttle (B6).
Spec: THESIS_RESILIENCE_SPEC v1.0 — Block 6

Limits full thesis regenerations triggered by daily brief to MAX_REGENS_PER_DAY.
Persists regen count to CHRONICLE to survive service restarts.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Optional

import pytz

logger = logging.getLogger("thesis.resilience.regen_throttle")

ET = pytz.timezone("America/New_York")
MAX_REGENS_PER_DAY = 2

# Module-level state (reset daily)
_regen_count_today: int = 0
_regen_count_date: Optional[str] = None


def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _load_regen_count(chronicle_db_path: str) -> int:
    """Load today's regen count from CHRONICLE. Returns 0 on any failure."""
    today = _today_et()
    try:
        conn = sqlite3.connect(chronicle_db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT count FROM thesis_regen_log WHERE date = ?", (today,)
        ).fetchone()
        conn.close()
        return row["count"] if row else 0
    except Exception as e:
        logger.warning("_load_regen_count: failed: %s — starting at 0", e)
        return 0


def _increment_regen_count(chronicle_db_path: str) -> None:
    """Increment and persist today's regen count. Fail-open."""
    today = _today_et()
    try:
        conn = sqlite3.connect(chronicle_db_path, timeout=5)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thesis_regen_log (
                date TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            INSERT INTO thesis_regen_log (date, count) VALUES (?, 1)
            ON CONFLICT(date) DO UPDATE SET count = count + 1
        """, (today,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("_increment_regen_count: failed: %s", e)


def can_regen(chronicle_db_path: Optional[str] = None) -> bool:
    """
    Check whether a daily brief regen is permitted.

    Loads current count from CHRONICLE on first call of the day.
    Returns False if MAX_REGENS_PER_DAY reached.

    Args:
        chronicle_db_path: Path to chronicle.db (optional, uses in-memory count if None).

    Returns:
        True if regen is permitted, False if throttled.
    """
    global _regen_count_today, _regen_count_date

    today = _today_et()

    # Reset on new day or first call
    if _regen_count_date != today:
        _regen_count_date = today
        if chronicle_db_path:
            _regen_count_today = _load_regen_count(chronicle_db_path)
        else:
            _regen_count_today = 0

    if _regen_count_today >= MAX_REGENS_PER_DAY:
        logger.warning(
            "can_regen: throttle hit (%d/%d today) — suppressing regen",
            _regen_count_today, MAX_REGENS_PER_DAY
        )
        return False

    return True


def record_regen(chronicle_db_path: Optional[str] = None) -> None:
    """
    Record that a regen was triggered.
    Call this after can_regen() returns True and regen starts.

    Args:
        chronicle_db_path: Path to chronicle.db.
    """
    global _regen_count_today

    _regen_count_today += 1
    logger.info("record_regen: count now %d/%d today", _regen_count_today, MAX_REGENS_PER_DAY)

    if chronicle_db_path:
        _increment_regen_count(chronicle_db_path)
