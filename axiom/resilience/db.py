"""
db.py — BEGIN IMMEDIATE wrapper for Axiom /assess write path.
Spec: AXIOM_30_SPEC v1.0 (Cipher, 2026-05-02)

Prevents concurrent write contention between the HTTP handler (assess requests)
and the scheduler (periodic DB writes). BEGIN IMMEDIATE acquires a reserved lock
immediately, avoiding writer starvation under concurrent load.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from typing import Any


@contextlib.contextmanager
def immediate_transaction(db_path: str):
    """
    Context manager for SQLite BEGIN IMMEDIATE transaction.

    BEGIN IMMEDIATE acquires a reserved lock immediately, preventing
    writer starvation under concurrent load (scheduler + HTTP handlers).

    Usage:
        with immediate_transaction(db_path) as conn:
            conn.execute("INSERT INTO ...")
    """
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def assess_db_write(
    db_path: str,
    ticker: str,
    window_id: str,
    risk_score: float,
    sizing_mult: float,
    hard_stops: list,
    flags: list,
    raw_result: dict,
) -> None:
    """
    Write a risk assessment to DB using BEGIN IMMEDIATE.

    Replaces the direct sqlite3 call in axiom/database.py's save_risk_assessment.
    Call this from main.py /assess endpoint instead of the bare save_risk_assessment.

    Note: Does NOT replace save_risk_assessment entirely — wraps the write path only.
    The function signature mirrors save_risk_assessment for drop-in integration.
    """
    with immediate_transaction(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO risk_assessments
               (ticker, window_id, risk_score, sizing_mult,
                hard_stops, flags, raw_result, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                ticker,
                window_id,
                risk_score,
                sizing_mult,
                json.dumps(hard_stops),
                json.dumps(flags),
                json.dumps(raw_result),
            ),
        )
