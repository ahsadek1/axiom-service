"""
THESIS — SQLite migration module.

Runs at service startup to ensure thesis_context and thesis_events tables
exist in the CHRONICLE database. THESIS and CHRONICLE share the same
Mac Mini (.42), so direct SQLite access is safe and fast.

Never drops or modifies existing tables — purely additive DDL.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL statements
# ---------------------------------------------------------------------------

CREATE_THESIS_CONTEXT = """
CREATE TABLE IF NOT EXISTS thesis_context (
    id               INTEGER PRIMARY KEY,
    layer            TEXT    NOT NULL,
    valid_from       TEXT    NOT NULL,
    valid_until      TEXT    NOT NULL,
    trading_posture  TEXT    NOT NULL,
    sizing_multiplier REAL   NOT NULL,
    favored_sectors  TEXT,
    avoid_sectors    TEXT,
    favored_strategies TEXT,
    confidence_adjustment INTEGER,
    macro_gate       TEXT    NOT NULL,
    risk_reward_gate TEXT    NOT NULL,
    thesis_sentence  TEXT    NOT NULL,
    primary_authority TEXT,
    full_thesis      TEXT,
    created_at       TEXT    DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_THESIS_EVENTS = """
CREATE TABLE IF NOT EXISTS thesis_events (
    id               INTEGER PRIMARY KEY,
    event_type       TEXT    NOT NULL,
    detected_at      TEXT    NOT NULL,
    description      TEXT    NOT NULL,
    affected_sectors TEXT,
    thesis_paused    INTEGER DEFAULT 0,
    thesis_updated_at TEXT,
    resolved_at      TEXT
);
"""

CREATE_THESIS_CONTEXT_IDX = """
CREATE INDEX IF NOT EXISTS idx_thesis_context_layer_valid
    ON thesis_context (layer, valid_from DESC);
"""

CREATE_THESIS_EVENTS_IDX = """
CREATE INDEX IF NOT EXISTS idx_thesis_events_detected
    ON thesis_events (detected_at DESC);
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_migration(db_path: str) -> bool:
    """
    Apply all THESIS schema migrations to the CHRONICLE SQLite database.

    Args:
        db_path: Absolute path to chronicle.db (from CHRONICLE_DB_PATH env var).

    Returns:
        True if migration succeeded, False on failure (service continues safely).
    """
    path = Path(db_path)

    if not path.parent.exists():
        logger.error(
            "THESIS migration: parent directory does not exist: %s", path.parent
        )
        return False

    if not path.exists():
        logger.warning(
            "THESIS migration: chronicle.db not found at %s — "
            "CHRONICLE may not have started yet. THESIS will retry.",
            db_path,
        )
        return False

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")

        with conn:
            conn.execute(CREATE_THESIS_CONTEXT)
            conn.execute(CREATE_THESIS_EVENTS)
            conn.execute(CREATE_THESIS_CONTEXT_IDX)
            conn.execute(CREATE_THESIS_EVENTS_IDX)

        conn.close()
        logger.info("THESIS migration: schema applied successfully to %s", db_path)
        return True

    except sqlite3.Error as exc:
        logger.error("THESIS migration: SQLite error — %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error("THESIS migration: unexpected error — %s", exc)
        return False


def verify_schema(db_path: str) -> bool:
    """
    Confirm that both THESIS tables exist in the database.

    Args:
        db_path: Path to chronicle.db.

    Returns:
        True if both tables are present.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('thesis_context', 'thesis_events');"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()
        return "thesis_context" in tables and "thesis_events" in tables
    except sqlite3.Error as exc:
        logger.error("THESIS schema verify: %s", exc)
        return False
