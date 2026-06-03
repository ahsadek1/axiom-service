"""
chronicle_client.py — ARCHITECT CHRONICLE Critique Bank Writer

Writes findings directly to chronicle.db SQLite (no HTTP layer).
Caches locally in agent SQLite if CHRONICLE is unreachable.
Background flush every 60s retries cached entries.
"""

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("architect.chronicle_client")

AGENT_NAME = "ARCHITECT"
BRAIN_MODEL = "claude-sonnet-4-6"

# Background flush thread
_flush_stop = threading.Event()
_flush_thread: Optional[threading.Thread] = None
_flush_lock = threading.Lock()
FLUSH_INTERVAL_S = 60


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_review_entry(
    chronicle_db_path: str,
    local_db_path: str,
    service_name: str,
    service_version: str,
    review_cycle_id: str,
) -> int:
    """
    Create a new IN_PROGRESS row in critique_bank.
    Returns the row id. Falls back to local cache on failure.
    """
    now = _now_utc()
    try:
        conn = sqlite3.connect(chronicle_db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            """INSERT INTO critique_bank
               (service_name, service_version, review_cycle_id, reviewer_agent,
                reviewer_brain, review_status, review_started_at)
               VALUES (?, ?, ?, ?, ?, 'IN_PROGRESS', ?)""",
            (service_name, service_version, review_cycle_id,
             AGENT_NAME, BRAIN_MODEL, now),
        )
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        logger.info("chronicle: created review entry id=%d cycle=%s", row_id, review_cycle_id)
        return row_id
    except Exception as e:
        logger.error("chronicle: create_review_entry failed: %s — caching locally", e)
        return _cache_pending_create(local_db_path, service_name, service_version, review_cycle_id, now)


def write_findings(
    chronicle_db_path: str,
    local_db_path: str,
    chronicle_id: int,
    overall_assessment: str,
    p0_count: int,
    p1_count: int,
    p2_count: int,
    p3_count: int,
    full_report: list,
    confidence_level: str,
    review_cycle_id: str,
) -> bool:
    """
    Update critique_bank row with completed findings.
    Returns True on success. Caches locally on failure.
    """
    now = _now_utc()
    report_json = json.dumps(full_report)
    try:
        conn = sqlite3.connect(chronicle_db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """UPDATE critique_bank SET
               review_status='COMPLETED',
               overall_assessment=?,
               p0_count=?, p1_count=?, p2_count=?, p3_count=?,
               full_report=?,
               confidence_level=?,
               review_completed_at=?
               WHERE id=?""",
            (overall_assessment, p0_count, p1_count, p2_count, p3_count,
             report_json, confidence_level, now, chronicle_id),
        )
        conn.commit()
        conn.close()
        logger.info("chronicle: findings written for id=%d assessment=%s", chronicle_id, overall_assessment)
        return True
    except Exception as e:
        logger.error("chronicle: write_findings failed id=%d: %s — caching locally", chronicle_id, e)
        _cache_pending_update(local_db_path, chronicle_id, overall_assessment,
                               p0_count, p1_count, p2_count, p3_count,
                               report_json, confidence_level, now, review_cycle_id)
        return False


def mark_failed(chronicle_db_path: str, chronicle_id: int, reason: str) -> None:
    """Mark a review as FAILED in CHRONICLE."""
    now = _now_utc()
    try:
        conn = sqlite3.connect(chronicle_db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """UPDATE critique_bank SET
               review_status='FAILED',
               full_report=?,
               review_completed_at=?
               WHERE id=?""",
            (json.dumps([{"error": reason}]), now, chronicle_id),
        )
        conn.commit()
        conn.close()
        logger.info("chronicle: review id=%d marked FAILED: %s", chronicle_id, reason[:100])
    except Exception as e:
        logger.error("chronicle: mark_failed failed id=%d: %s", chronicle_id, e)


def get_review_status(chronicle_db_path: str, review_cycle_id: str) -> Optional[dict]:
    """Query critique_bank for this agent's review of the given cycle."""
    try:
        conn = sqlite3.connect(chronicle_db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            """SELECT * FROM critique_bank
               WHERE review_cycle_id=? AND reviewer_agent=?
               ORDER BY id DESC LIMIT 1""",
            (review_cycle_id, AGENT_NAME),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return dict(row)
    except Exception as e:
        logger.error("chronicle: get_review_status failed: %s", e)
        return None


# ── Local cache helpers ────────────────────────────────────────────────────────

def _cache_pending_create(
    local_db_path: str,
    service_name: str,
    service_version: str,
    review_cycle_id: str,
    started_at: str,
) -> int:
    """Store a pending create in local SQLite. Returns a negative fake ID."""
    try:
        conn = sqlite3.connect(local_db_path, timeout=5)
        cursor = conn.execute(
            """INSERT INTO chronicle_cache
               (op, service_name, service_version, review_cycle_id,
                reviewer_agent, reviewer_brain, started_at, queued_at)
               VALUES ('CREATE', ?, ?, ?, ?, ?, ?, ?)""",
            (service_name, service_version, review_cycle_id,
             AGENT_NAME, BRAIN_MODEL, started_at, _now_utc()),
        )
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return -row_id
    except Exception as e:
        logger.error("chronicle_cache: pending create failed: %s", e)
        return -999


def _cache_pending_update(
    local_db_path: str,
    chronicle_id: int,
    overall_assessment: str,
    p0_count: int,
    p1_count: int,
    p2_count: int,
    p3_count: int,
    report_json: str,
    confidence_level: str,
    completed_at: str,
    review_cycle_id: str,
) -> None:
    """Store a pending update in local SQLite for later flush."""
    try:
        conn = sqlite3.connect(local_db_path, timeout=5)
        conn.execute(
            """INSERT INTO chronicle_cache
               (op, chronicle_id, overall_assessment, p0_count, p1_count,
                p2_count, p3_count, full_report, confidence_level,
                completed_at, review_cycle_id, reviewer_agent, queued_at)
               VALUES ('UPDATE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chronicle_id, overall_assessment, p0_count, p1_count,
             p2_count, p3_count, report_json, confidence_level,
             completed_at, review_cycle_id, AGENT_NAME, _now_utc()),
        )
        conn.commit()
        conn.close()
        logger.info("chronicle_cache: UPDATE queued for chronicle_id=%d", chronicle_id)
    except Exception as e:
        logger.error("chronicle_cache: pending update failed: %s", e)


def flush_cache(chronicle_db_path: str, local_db_path: str) -> int:
    """
    Attempt to flush all pending cache entries to CHRONICLE.
    Returns count of successfully flushed rows.
    """
    flushed = 0
    try:
        local_conn = sqlite3.connect(local_db_path, timeout=5)
        local_conn.row_factory = sqlite3.Row
        rows = local_conn.execute(
            "SELECT * FROM chronicle_cache ORDER BY id"
        ).fetchall()

        for row in rows:
            row = dict(row)
            op = row.get("op")
            success = False

            if op == "CREATE":
                try:
                    chron_conn = sqlite3.connect(chronicle_db_path, timeout=10)
                    chron_conn.execute("PRAGMA journal_mode=WAL")
                    chron_conn.execute(
                        """INSERT INTO critique_bank
                           (service_name, service_version, review_cycle_id, reviewer_agent,
                            reviewer_brain, review_status, review_started_at)
                           VALUES (?, ?, ?, ?, ?, 'IN_PROGRESS', ?)""",
                        (row["service_name"], row["service_version"], row["review_cycle_id"],
                         AGENT_NAME, BRAIN_MODEL, row["started_at"]),
                    )
                    chron_conn.commit()
                    chron_conn.close()
                    success = True
                except Exception as e:
                    logger.warning("chronicle flush CREATE failed: %s", e)

            elif op == "UPDATE":
                try:
                    chron_conn = sqlite3.connect(chronicle_db_path, timeout=10)
                    chron_conn.execute("PRAGMA journal_mode=WAL")
                    chron_conn.execute(
                        """UPDATE critique_bank SET
                           review_status='COMPLETED',
                           overall_assessment=?,
                           p0_count=?, p1_count=?, p2_count=?, p3_count=?,
                           full_report=?, confidence_level=?, review_completed_at=?
                           WHERE id=?""",
                        (row["overall_assessment"], row["p0_count"], row["p1_count"],
                         row["p2_count"], row["p3_count"], row["full_report"],
                         row["confidence_level"], row["completed_at"], row["chronicle_id"]),
                    )
                    chron_conn.commit()
                    chron_conn.close()
                    success = True
                except Exception as e:
                    logger.warning("chronicle flush UPDATE failed: %s", e)

            if success:
                local_conn.execute("DELETE FROM chronicle_cache WHERE id=?", (row["id"],))
                local_conn.commit()
                flushed += 1

        local_conn.close()
        if flushed:
            logger.info("chronicle_cache: flushed %d entries", flushed)
    except Exception as e:
        logger.error("chronicle_cache: flush failed: %s", e)
    return flushed


def start_flush_thread(chronicle_db_path: str, local_db_path: str) -> None:
    """Start the background cache flush thread (idempotent)."""
    global _flush_thread
    with _flush_lock:
        if _flush_thread and _flush_thread.is_alive():
            return
        _flush_stop.clear()

        def _worker() -> None:
            logger.info("chronicle_cache: flush thread started (interval=%ds)", FLUSH_INTERVAL_S)
            while not _flush_stop.is_set():
                _flush_stop.wait(FLUSH_INTERVAL_S)
                if not _flush_stop.is_set():
                    flush_cache(chronicle_db_path, local_db_path)

        _flush_thread = threading.Thread(
            target=_worker, daemon=True, name="architect-chronicle-flush"
        )
        _flush_thread.start()


def stop_flush_thread() -> None:
    """Stop the background flush thread."""
    _flush_stop.set()
