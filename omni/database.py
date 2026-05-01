"""
database.py — OMNI SQLite Layer

Stores synthesis results and full audit trail.
Every OMNI decision is persisted — full accountability on every trade.
WAL mode, foreign keys enforced, no silent failures.
"""

import json
import logging
import os
import sys
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, Optional

logger = logging.getLogger("omni.database")


# ── Connection (SQLite + Postgres via shared adapter) ─────────────────────────

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

    Args:
        db_path: Path to the SQLite database file (ignored when using Postgres).

    Yields:
        Connection with consistent sqlite3-like interface.
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


def init_db(db_path: str) -> None:
    """
    Initialize the OMNI database schema.

    Idempotent — safe to call multiple times.

    Args:
        db_path: Path to the SQLite database file.
    """
    with get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS synthesis_results (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                window_id             TEXT    NOT NULL,
                ticker                TEXT    NOT NULL,
                direction             TEXT    NOT NULL,
                system                TEXT    NOT NULL,   -- 'alpha' or 'prime'
                pathway               TEXT    NOT NULL,   -- P1, P2, P3, P4
                agent_weighted_score  REAL    NOT NULL,

                claude_vote           TEXT,
                claude_confidence     REAL,
                claude_reasoning      TEXT,
                claude_concern_1      TEXT,
                claude_concern_2      TEXT,
                claude_echo_chamber   INTEGER DEFAULT 0,
                claude_error          TEXT,

                o3mini_vote           TEXT,
                o3mini_confidence     REAL,
                o3mini_reasoning      TEXT,
                o3mini_concern_1      TEXT,
                o3mini_concern_2      TEXT,
                o3mini_echo_chamber   INTEGER DEFAULT 0,
                o3mini_error          TEXT,

                gemini_vote           TEXT,
                gemini_confidence     REAL,
                gemini_reasoning      TEXT,
                gemini_concern_1      TEXT,
                gemini_concern_2      TEXT,
                gemini_error          TEXT,

                deepseek_vote         TEXT,
                deepseek_confidence   REAL,
                deepseek_reasoning    TEXT,
                deepseek_concern_1    TEXT,
                deepseek_concern_2    TEXT,
                deepseek_error        TEXT,

                votes_go              INTEGER NOT NULL DEFAULT 0,
                brains_responded      INTEGER NOT NULL DEFAULT 0,
                echo_chamber_flagged  INTEGER NOT NULL DEFAULT 0,
                verdict               TEXT    NOT NULL,
                verdict_notes         TEXT,

                axiom_risk_score      REAL,
                axiom_sizing_mult     REAL,
                axiom_hard_stops      TEXT,   -- JSON array
                axiom_error           TEXT,

                execution_dispatched  INTEGER NOT NULL DEFAULT 0,
                execution_url         TEXT,
                execution_response    TEXT,

                created_at            TEXT    NOT NULL,
                UNIQUE(window_id, ticker, direction, system)
            );

            CREATE INDEX IF NOT EXISTS idx_synthesis_ticker
                ON synthesis_results(ticker, direction, created_at);

            CREATE TABLE IF NOT EXISTS conditional_queue (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                synthesis_id    INTEGER NOT NULL REFERENCES synthesis_results(id),
                window_id       TEXT    NOT NULL,
                ticker          TEXT    NOT NULL,
                direction       TEXT    NOT NULL,
                system          TEXT    NOT NULL,
                verdict         TEXT    NOT NULL,
                votes_go        INTEGER NOT NULL,
                expires_at      TEXT    NOT NULL,   -- auto-expire after 30 min
                resolved        INTEGER NOT NULL DEFAULT 0,
                resolution      TEXT,               -- 'approved', 'rejected', 'expired'
                resolved_at     TEXT,
                created_at      TEXT    NOT NULL
            );
        """)

    logger.info("OMNI database initialized at %s", db_path)


def save_synthesis_result(
    db_path:              str,
    window_id:            str,
    ticker:               str,
    direction:            str,
    system:               str,
    pathway:              str,
    agent_weighted_score: float,
    brain_results:        dict[str, dict],
    votes_go:             int,
    brains_responded:     int,
    echo_chamber_flagged: bool,
    verdict:              str,
    verdict_notes:        str,
    axiom_result:         Optional[dict],
    psychology_overlay:   Optional[dict] = None,
) -> int:
    """
    Persist a complete synthesis result.

    Args:
        db_path:              Database path.
        window_id:            15-min window ID.
        ticker:               Stock ticker.
        direction:            bullish or bearish.
        system:               'alpha' or 'prime'.
        pathway:              P1, P2, P3, or P4.
        agent_weighted_score: Score from concordance buffer.
        brain_results:        Dict of brain_name → {vote, confidence, reasoning, ...}.
        votes_go:             Count of GO votes.
        brains_responded:     Count of brains that responded (not errored).
        echo_chamber_flagged: True if any brain detected echo chamber.
        verdict:              STRONG_GO, GO, CONDITIONAL, or NO_GO.
        verdict_notes:        Human-readable explanation of verdict.
        axiom_result:         Axiom risk assessment dict or None.

    Returns:
        Row ID of the inserted record.
    """
    now = datetime.now(timezone.utc).isoformat()

    def br(brain: str, field: str):
        return brain_results.get(brain, {}).get(field)

    hard_stops_json = json.dumps(axiom_result.get("hard_stops", [])) if axiom_result else None

    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO synthesis_results (
                window_id, ticker, direction, system, pathway, agent_weighted_score,
                claude_vote, claude_confidence, claude_reasoning,
                claude_concern_1, claude_concern_2, claude_echo_chamber, claude_error,
                o3mini_vote, o3mini_confidence, o3mini_reasoning,
                o3mini_concern_1, o3mini_concern_2, o3mini_echo_chamber, o3mini_error,
                gemini_vote, gemini_confidence, gemini_reasoning,
                gemini_concern_1, gemini_concern_2, gemini_error,
                deepseek_vote, deepseek_confidence, deepseek_reasoning,
                deepseek_concern_1, deepseek_concern_2, deepseek_error,
                votes_go, brains_responded, echo_chamber_flagged,
                verdict, verdict_notes,
                axiom_risk_score, axiom_sizing_mult, axiom_hard_stops, axiom_error,
                psychology_overlay,
                created_at
            ) VALUES (
                ?,?,?,?,?,?,
                ?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,
                ?,?,?,?,?,?,
                ?,?,?,?,?,?,
                ?,?,?,?,?,
                ?,?,?,?,?,?
            )
            ON CONFLICT(window_id, ticker, direction, system)
            DO UPDATE SET
                votes_go             = excluded.votes_go,
                verdict              = excluded.verdict,
                verdict_notes        = excluded.verdict_notes,
                -- CRITICAL FIX: never reset execution_dispatched on upsert.
                -- A duplicate concordance submission must NOT re-stage an already-executed trade.
                execution_dispatched = CASE
                    WHEN synthesis_results.execution_dispatched = 1 THEN 1
                    ELSE 0
                END,
                created_at           = excluded.created_at
            """,
            (
                window_id, ticker, direction, system, pathway, agent_weighted_score,
                br("claude", "vote"), br("claude", "confidence"), br("claude", "reasoning"),
                br("claude", "concern_1"), br("claude", "concern_2"),
                1 if br("claude", "echo_chamber") else 0, br("claude", "error"),
                br("o3mini", "vote"), br("o3mini", "confidence"), br("o3mini", "reasoning"),
                br("o3mini", "concern_1"), br("o3mini", "concern_2"),
                1 if br("o3mini", "echo_chamber") else 0, br("o3mini", "error"),
                br("gemini", "vote"), br("gemini", "confidence"), br("gemini", "reasoning"),
                br("gemini", "concern_1"), br("gemini", "concern_2"), br("gemini", "error"),
                br("deepseek", "vote"), br("deepseek", "confidence"), br("deepseek", "reasoning"),
                br("deepseek", "concern_1"), br("deepseek", "concern_2"), br("deepseek", "error"),
                votes_go, brains_responded, 1 if echo_chamber_flagged else 0,
                verdict, verdict_notes,
                axiom_result.get("risk_score") if axiom_result else None,
                axiom_result.get("sizing_mult") if axiom_result else None,
                hard_stops_json,
                axiom_result.get("error") if axiom_result else None,
                json.dumps(psychology_overlay) if psychology_overlay else None,
                now,
            ),
        )
        # Always fetch the actual row ID — lastrowid is 0 on ON CONFLICT DO UPDATE
        row = conn.execute(
            "SELECT id FROM synthesis_results WHERE window_id=? AND ticker=? AND direction=? AND system=?",
            (window_id, ticker, direction, system),
        ).fetchone()
        return row["id"] if row else 0


def mark_execution_dispatched(
    db_path:            str,
    synthesis_id:       int,
    execution_url:      str,
    execution_response: Optional[str],
) -> None:
    """
    Mark a synthesis result as dispatched to the execution engine.

    Args:
        db_path:            Database path.
        synthesis_id:       Row ID of the synthesis result.
        execution_url:      URL that was called.
        execution_response: Response summary from execution engine.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE synthesis_results
            SET execution_dispatched=1, execution_url=?, execution_response=?
            WHERE id=?
            """,
            (execution_url, execution_response, synthesis_id),
        )


def save_conditional(
    db_path:      str,
    synthesis_id: int,
    window_id:    str,
    ticker:       str,
    direction:    str,
    system:       str,
    verdict:      str,
    votes_go:     int,
    expires_at:   str,
) -> int:
    """
    Add a CONDITIONAL result to the queue awaiting Ahmed's approval.

    Args:
        db_path:      Database path.
        synthesis_id: Linked synthesis result ID.
        window_id:    15-min window ID.
        ticker:       Stock ticker.
        direction:    bullish or bearish.
        system:       'alpha' or 'prime'.
        verdict:      Always 'CONDITIONAL' here.
        votes_go:     Number of GO votes (2 for CONDITIONAL).
        expires_at:   ISO timestamp when this expires.

    Returns:
        Row ID of the conditional queue entry.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO conditional_queue
                (synthesis_id, window_id, ticker, direction, system, verdict,
                 votes_go, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (synthesis_id, window_id, ticker, direction, system, verdict,
             votes_go, expires_at, now),
        )
        return cursor.lastrowid


def get_synthesis_result(
    db_path:   str,
    window_id: str,
    ticker:    str,
    direction: str,
    system:    str,
) -> Optional[dict]:
    """Retrieve a synthesis result by window/ticker/direction/system."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM synthesis_results
            WHERE window_id=? AND ticker=? AND direction=? AND system=?
            """,
            (window_id, ticker, direction, system),
        ).fetchone()
    return dict(row) if row else None


def get_recent_syntheses(db_path: str, limit: int = 20) -> list[dict]:
    """Get the most recent synthesis results for status display."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, window_id, ticker, direction, system, pathway,
                   verdict, votes_go, brains_responded, execution_dispatched, created_at
            FROM synthesis_results
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
