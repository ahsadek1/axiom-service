"""
trs.py — Transaction Result Store for nexus-integrity.

The TRS is the single authoritative source of the current trading readiness score.
It uses WAL-mode SQLite for durability and is strictly fail-closed:
- Any read failure returns TRSResult(score=0, block=True)
- Any write failure blocks the probe result from being recorded
- A stale entry (age > TTL) is treated the same as missing: score=0, block=True

This is the implementation of Vector Amendment V1 (fail-closed TRS) and
Vector Final Rebuttal Amendment V11 (explicit fail-closed contract).
"""

import logging
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional

from config import TRS_DB_PATH, TRS_MAX_AGE_SECONDS
from models import AlertTier, CompositeColor, TRSResult

logger = logging.getLogger("integrity.trs")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trs_state (
    id          INTEGER PRIMARY KEY,
    score       REAL    NOT NULL,
    block       INTEGER NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    color       TEXT    NOT NULL DEFAULT 'BLACK',
    alert_tier  TEXT    NOT NULL DEFAULT 'P0',
    components  TEXT    NOT NULL DEFAULT '{}',
    written_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS trs_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    score       REAL    NOT NULL,
    block       INTEGER NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    color       TEXT    NOT NULL DEFAULT 'BLACK',
    alert_tier  TEXT    NOT NULL DEFAULT 'P0',
    components  TEXT    NOT NULL DEFAULT '{}',
    written_at  REAL    NOT NULL
);
"""


def _get_connection() -> sqlite3.Connection:
    """Open WAL-mode SQLite connection to TRS database.

    Returns:
        sqlite3.Connection: Connection with WAL mode enabled.

    Raises:
        sqlite3.Error: If connection cannot be established.
    """
    path = Path(TRS_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_trs() -> None:
    """Initialize TRS database schema.

    Creates the trs_state and trs_history tables if they do not exist.
    Called once at service startup.

    Raises:
        SystemExit: If database cannot be initialized (fail-closed — no TRS = no trading).
    """
    try:
        conn = _get_connection()
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()
        logger.info("TRS database initialized at %s", TRS_DB_PATH)
    except sqlite3.Error as e:
        logger.critical("FATAL: TRS database init failed: %s — trading will be hard-blocked", e)
        raise SystemExit(f"TRS init failed: {e}") from e


def write_trs(result: TRSResult) -> None:
    """Write a new TRS result to the store.

    Overwrites the single trs_state row and appends to trs_history.
    If this write fails, the probe is considered blocked — caller must
    treat a write failure as a scoring failure (fail-closed).

    Args:
        result: The TRSResult to persist.

    Raises:
        sqlite3.Error: If write fails. Caller must handle this as a block.
    """
    import json

    now = time.time()
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM trs_state")
        conn.execute(
            """INSERT INTO trs_state
               (id, score, block, reason, color, alert_tier, components, written_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.score,
                1 if result.block else 0,
                result.reason,
                result.color.value,
                result.alert_tier.value,
                json.dumps(result.component_scores),
                now,
            ),
        )
        conn.execute(
            """INSERT INTO trs_history
               (score, block, reason, color, alert_tier, components, written_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                result.score,
                1 if result.block else 0,
                result.reason,
                result.color.value,
                result.alert_tier.value,
                json.dumps(result.component_scores),
                now,
            ),
        )
        conn.commit()
        logger.info(
            "TRS written: score=%.1f color=%s block=%s tier=%s",
            result.score, result.color.value, result.block, result.alert_tier.value
        )
    finally:
        conn.close()


def get_trading_readiness_score() -> TRSResult:
    """Read current trading readiness score from TRS store.

    This is the BINDING fail-closed contract (Vector Final Rebuttal V11):
    - TRS store unreachable → score=0, block=True
    - TRS entry missing → score=0, block=True
    - TRS entry stale (age > TTL) → score=0, block=True
    - Any exception → score=0, block=True
    No default-allow path exists. The except clause always returns 0.

    Returns:
        TRSResult: Current score. Always returns a valid object — never raises.
    """
    import json

    try:
        conn = _get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM trs_state WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            logger.warning("TRS store empty — hard block applied")
            return TRSResult(score=0, reason="TRS_STORE_EMPTY", block=True)

        age = time.time() - row["written_at"]
        if age > TRS_MAX_AGE_SECONDS:
            logger.warning(
                "TRS score stale (age=%.0fs > TTL=%ds) — hard block applied",
                age, TRS_MAX_AGE_SECONDS
            )
            return TRSResult(score=0, reason=f"TRS_STALE_age={age:.0f}s", block=True)

        try:
            components = json.loads(row["components"])
        except (ValueError, TypeError):
            components = {}

        result = TRSResult(
            score=float(row["score"]),
            block=bool(row["block"]),
            reason=row["reason"],
            component_scores=components,
            age_seconds=age,
        )
        return result

    except Exception as e:  # noqa: BLE001 — intentional catch-all, fail-closed
        logger.critical(
            "TRS fetch failed: %s — HARD BLOCK applied (fail-closed contract)", e
        )
        return TRSResult(score=0, reason=f"TRS_FETCH_FAILED: {e}", block=True)


def get_trs_history(limit: int = 20) -> list:
    """Retrieve recent TRS history entries.

    Args:
        limit: Maximum number of entries to return (most recent first).

    Returns:
        List of dicts with TRS history rows. Empty list on any error.
    """
    import json

    try:
        conn = _get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM trs_history ORDER BY written_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.error("TRS history fetch failed: %s", e)
        return []
