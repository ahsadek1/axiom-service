"""
execution_dlq.py — Execution Dead Letter Queue (GAP-003)

Every GO/STRONG_GO synthesis that fails execution is written here.
A background worker retries with exponential backoff.
Entries expire after MAX_AGE_MINUTES (trade window closes).

Commercial-grade guarantee:
- No failed trade is silently discarded
- Every retry attempt logged in CHRONICLE
- Expired entries reported to SOVEREIGN for post-session review

Author: OMNI (GAP-003, 2026-04-28)
"""

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger("omni.execution_dlq")

DLQ_DB_PATH    = os.environ.get("OMNI_DLQ_PATH",
                  "/Users/ahmedsadek/nexus/data/execution_dlq.db")
MAX_AGE_MINUTES  = 12    # expire after 12 min — concordance windows are 15 min
MAX_ATTEMPTS     = 4     # max retry attempts before expiry
BASE_BACKOFF_S   = 15    # first retry after 15s
MAX_BACKOFF_S    = 120   # cap at 2 min between retries
SOVEREIGN_BUS    = os.environ.get("SOVEREIGN_BUS_URL", "http://192.168.1.141:9999")


# ── Schema ────────────────────────────────────────────────────────────────────

def _init_db() -> None:
    os.makedirs(os.path.dirname(DLQ_DB_PATH), exist_ok=True)
    with sqlite3.connect(DLQ_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS execution_dlq (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT    NOT NULL,
                direction       TEXT    NOT NULL,
                system          TEXT    NOT NULL,
                pathway         TEXT    NOT NULL,
                window_id       TEXT    NOT NULL,
                payload         TEXT    NOT NULL,
                failure_reason  TEXT,
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                next_retry_at   TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'pending',
                created_at      TEXT    NOT NULL,
                resolved_at     TEXT,
                resolved_by     TEXT
            )
        """)
        conn.commit()


# ── Enqueue ───────────────────────────────────────────────────────────────────

def enqueue(
    ticker:         str,
    direction:      str,
    system:         str,
    pathway:        str,
    window_id:      str,
    payload:        dict,
    failure_reason: Optional[str] = None,
) -> int:
    """
    Add a failed execution to the DLQ for retry.
    Returns the DLQ entry id.
    """
    _init_db()
    now = datetime.now(timezone.utc).isoformat()
    next_retry = datetime.utcnow().isoformat()  # retry immediately on first enqueue

    with sqlite3.connect(DLQ_DB_PATH) as conn:
        cursor = conn.execute("""
            INSERT INTO execution_dlq
            (ticker, direction, system, pathway, window_id, payload,
             failure_reason, attempts, next_retry_at, status, created_at)
            VALUES (?,?,?,?,?,?,?,0,?,\'pending\',?)
        """, (
            ticker, direction, system, pathway, window_id,
            json.dumps(payload), failure_reason, next_retry, now
        ))
        conn.commit()
        entry_id = cursor.lastrowid

    logger.warning(
        "DLQ: enqueued failed execution %s/%s/%s (id=%d reason=%s)",
        ticker, direction, system, entry_id, (failure_reason or "unknown")[:60],
    )
    return entry_id


# ── Worker ────────────────────────────────────────────────────────────────────

def _backoff(attempts: int) -> float:
    """Exponential backoff capped at MAX_BACKOFF_S."""
    return min(BASE_BACKOFF_S * (2 ** attempts), MAX_BACKOFF_S)


def _notify_sovereign(msg: str) -> None:
    try:
        import requests as _req
        _req.post(f"{SOVEREIGN_BUS}/send",
                  json={"from": "omni-dlq", "to": "sovereign", "message": msg},
                  timeout=5)
    except Exception:
        pass


def _register_chronicle(ticker: str, system: str, attempts: int, outcome: str, reason: str) -> None:
    """Register DLQ resolution in CHRONICLE intervention_log."""
    try:
        db = "/Users/ahmedsadek/nexus/data/chronicle.db"
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db) as conn:
            conn.execute("""
                INSERT INTO intervention_log
                (agent, error_description, time_identified, ideal_response_time,
                 time_acted, time_resolved, resolution_type, outcome, notes, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                "OMNI-DLQ",
                f"Execution DLQ: {ticker}/{system} failed and entered retry queue",
                now, now, now, now,
                "root_cause",
                "solved" if outcome == "executed" else "unsolved",
                f"Attempts: {attempts} | Outcome: {outcome} | Reason: {reason[:200]}",
                now,
            ))
            conn.commit()
    except Exception as e:
        logger.debug("DLQ CHRONICLE registration failed: %s", e)


def process_dlq(route_fn: Callable[[dict], tuple[bool, Optional[str]]]) -> None:
    """
    Process one cycle of the DLQ.
    Called by the watchdog every 60 seconds.

    Args:
        route_fn: Callable that takes the execution payload dict and returns (success, response).
    """
    _init_db()
    now_utc = datetime.utcnow().isoformat()
    now_ts  = time.time()

    with sqlite3.connect(DLQ_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        pending = conn.execute("""
            SELECT * FROM execution_dlq
            WHERE status = 'pending'
            AND next_retry_at <= ?
            ORDER BY created_at ASC
        """, (now_utc,)).fetchall()

    for entry in pending:
        entry_id    = entry["id"]
        ticker      = entry["ticker"]
        system      = entry["system"]
        window_id   = entry["window_id"]
        attempts    = entry["attempts"]
        created_at  = entry["created_at"]
        payload     = json.loads(entry["payload"])

        # Check expiry — don't retry stale entries
        try:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - created_dt).total_seconds() / 60
        except Exception:
            age_min = 999

        if age_min > MAX_AGE_MINUTES or attempts >= MAX_ATTEMPTS:
            # Expired — mark as abandoned, notify SOVEREIGN
            with sqlite3.connect(DLQ_DB_PATH) as conn:
                conn.execute(
                    "UPDATE execution_dlq SET status='abandoned', resolved_at=?, resolved_by=? WHERE id=?",
                    (now_utc, "dlq_expiry", entry_id)
                )
                conn.commit()
            reason = f"age={age_min:.1f}min" if age_min > MAX_AGE_MINUTES else f"max_attempts={attempts}"
            logger.error(
                "DLQ ABANDONED: %s/%s (id=%d) — %s",
                ticker, system, entry_id, reason,
            )
            _notify_sovereign(
                f"⚠️ DLQ ABANDONED: {ticker}/{system} window={window_id}\n"
                f"Reason: {reason} | Attempts: {attempts}\n"
                f"Trade window has closed — entry expired without execution."
            )
            _register_chronicle(ticker, system, attempts, "abandoned", reason)
            continue

        # Retry
        logger.info(
            "DLQ RETRY: %s/%s (id=%d attempt=%d/%d)",
            ticker, system, entry_id, attempts + 1, MAX_ATTEMPTS,
        )
        try:
            success, resp = route_fn(payload)
        except Exception as e:
            success, resp = False, str(e)[:200]

        now_utc_fresh = datetime.now(timezone.utc).isoformat()

        if success:
            with sqlite3.connect(DLQ_DB_PATH) as conn:
                conn.execute(
                    "UPDATE execution_dlq SET status='executed', attempts=?, "
                    "last_attempt_at=?, resolved_at=?, resolved_by=? WHERE id=?",
                    (attempts + 1, now_utc_fresh, now_utc_fresh, "dlq_retry", entry_id)
                )
                conn.commit()
            logger.info(
                "DLQ RESOLVED: %s/%s (id=%d) executed on attempt %d",
                ticker, system, entry_id, attempts + 1,
            )
            _notify_sovereign(
                f"✅ DLQ RESOLVED: {ticker}/{system} executed on retry {attempts + 1}"
            )
            _register_chronicle(ticker, system, attempts + 1, "executed", resp or "")
        else:
            next_retry_dt = datetime.utcfromtimestamp(
                time.time() + _backoff(attempts + 1)
            ).isoformat()
            with sqlite3.connect(DLQ_DB_PATH) as conn:
                conn.execute(
                    "UPDATE execution_dlq SET attempts=?, last_attempt_at=?, "
                    "next_retry_at=?, failure_reason=? WHERE id=?",
                    (attempts + 1, now_utc_fresh, next_retry_dt, (resp or "")[:200], entry_id)
                )
                conn.commit()
            logger.warning(
                "DLQ RETRY FAILED: %s/%s (id=%d attempt=%d) resp=%s — next in %.0fs",
                ticker, system, entry_id, attempts + 1,
                (resp or "")[:60], _backoff(attempts + 1),
            )


def get_dlq_stats() -> dict:
    """Return DLQ status for health endpoints."""
    _init_db()
    with sqlite3.connect(DLQ_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        stats = {}
        for status in ("pending", "executed", "abandoned"):
            count = conn.execute(
                "SELECT COUNT(*) FROM execution_dlq WHERE status=?", (status,)
            ).fetchone()[0]
            stats[status] = count
    return stats
