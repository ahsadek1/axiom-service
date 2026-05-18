"""
genesis/alerts/alert_ack_tracker.py — ACK Tracking & Retry State
=================================================================

Persistent tracker for alert delivery status.
Maintains message ID → ACK state mappings.
Detects unacknowledged alerts and triggers escalation.

State machine:
    PENDING → ACK_RECEIVED
    PENDING → (5m timeout) → RESEND_QUEUED → PENDING
    RESEND_QUEUED → (after resend attempt) → PENDING
    PENDING → (10m timeout) → ESCALATE_REQUIRED

Usage:
    from genesis.alerts.alert_ack_tracker import (
        track_alert, is_acked, get_pending_alerts, mark_acked
    )

    # Track a sent alert
    track_alert(
        message_id="genesis-1715857200-1",
        title="Buffer stall",
        agent="genesis"
    )

    # Poll for ACK (from Telegram update handler)
    if is_acked("genesis-1715857200-1"):
        mark_acked("genesis-1715857200-1")

    # Get alerts needing resend (run every 5 min)
    resend_alerts = get_pending_alerts(older_than_min=5)

    # Get alerts needing escalation (run every 10 min)
    escalate_alerts = get_pending_alerts(older_than_min=10, critical=True)

Author: GENESIS 🌱
Date: 2026-05-16
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("genesis.alerts.ack_tracker")

# ── Configuration ────────────────────────────────────────────────────────────

ACK_DB_PATH = os.getenv(
    "GENESIS_ACK_DB",
    "/Users/ahmedsadek/nexus/genesis/data/alert_ack.db",
)
RESEND_TIMEOUT_MIN = 5      # Resend if no ACK after 5 min
ESCALATE_TIMEOUT_MIN = 10   # Page on-call if no ACK after 10 min

_lock = threading.Lock()


# ── DB Setup ─────────────────────────────────────────────────────────────────

def _ensure_db() -> None:
    """Create ACK tracking table if it doesn't exist (idempotent)."""
    Path(ACK_DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(ACK_DB_PATH, timeout=5, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alert_ack_state (
            message_id  TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            agent       TEXT NOT NULL,
            sent_at     TEXT NOT NULL,
            ack_received_at TEXT,
            resend_count INTEGER NOT NULL DEFAULT 0,
            escalated   INTEGER NOT NULL DEFAULT 0,
            last_resend_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_alert_ack_sent_at
            ON alert_ack_state(sent_at);
    """)
    conn.commit()
    conn.close()


def _connect() -> sqlite3.Connection:
    """Get a connection to the ACK tracker DB."""
    conn = sqlite3.connect(ACK_DB_PATH, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _now_utc() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


# ── Public API ───────────────────────────────────────────────────────────────

def track_alert(
    message_id: str,
    title: str,
    agent: str = "genesis",
) -> bool:
    """
    Record a newly sent alert for ACK tracking.

    Args:
        message_id: Unique message ID (from alert_dispatcher).
        title:      Alert title (for context in resend/escalate).
        agent:      Source agent name.

    Returns:
        True on success, False on DB error.
    """
    _ensure_db()

    with _lock:
        try:
            conn = _connect()
            conn.execute(
                """
                INSERT OR IGNORE INTO alert_ack_state
                    (message_id, title, agent, sent_at)
                VALUES (?, ?, ?, ?)
                """,
                (message_id, title, agent, _now_utc()),
            )
            conn.commit()
            conn.close()
            logger.info(
                "ack_tracker: tracked message_id=%s title=%s agent=%s",
                message_id, title[:40], agent,
            )
            return True
        except Exception as e:
            logger.error("ack_tracker: track_alert failed: %s", e)
            return False


def mark_acked(message_id: str) -> bool:
    """
    Mark an alert as acknowledged by Ahmed.

    Args:
        message_id: The message ID to mark as ACKed.

    Returns:
        True if marked, False if not found or DB error.
    """
    _ensure_db()

    with _lock:
        try:
            conn = _connect()
            cur = conn.execute(
                """
                UPDATE alert_ack_state
                SET ack_received_at = ?
                WHERE message_id = ? AND ack_received_at IS NULL
                """,
                (_now_utc(), message_id),
            )
            conn.commit()
            conn.close()
            if cur.rowcount > 0:
                logger.info("ack_tracker: ACK received for message_id=%s", message_id)
                return True
            logger.debug("ack_tracker: mark_acked no-op (already ACKed or not found)")
            return False
        except Exception as e:
            logger.error("ack_tracker: mark_acked failed: %s", e)
            return False


def is_acked(message_id: str) -> bool:
    """
    Check if an alert has been acknowledged.

    Args:
        message_id: The message ID to check.

    Returns:
        True if ACKed, False if pending or not found.
    """
    _ensure_db()

    try:
        conn = _connect()
        row = conn.execute(
            "SELECT ack_received_at FROM alert_ack_state WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        conn.close()
        return row is not None and row["ack_received_at"] is not None
    except Exception as e:
        logger.error("ack_tracker: is_acked failed: %s", e)
        return False


def get_pending_alerts(
    older_than_min: int = 5,
    critical: bool = False,
) -> List[Dict]:
    """
    Get unacknowledged alerts pending action.

    Args:
        older_than_min: Return alerts sent >N minutes ago (default 5).
        critical:       If True, only return alerts >10 min old (escalation candidates).

    Returns:
        List of dicts with keys:
        message_id, title, agent, sent_at, resend_count, escalated, last_resend_at
    """
    _ensure_db()

    try:
        conn = _connect()
        cutoff_time = (
            datetime.now(timezone.utc) - timedelta(minutes=older_than_min)
        ).isoformat()

        if critical:
            # Only escalation-critical alerts (>10 min old)
            cutoff_time = (
                datetime.now(timezone.utc) - timedelta(minutes=ESCALATE_TIMEOUT_MIN)
            ).isoformat()

        rows = conn.execute(
            """
            SELECT message_id, title, agent, sent_at, resend_count, escalated, last_resend_at
            FROM alert_ack_state
            WHERE ack_received_at IS NULL AND sent_at < ? AND escalated = 0
            ORDER BY sent_at ASC
            """,
            (cutoff_time,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    except Exception as e:
        logger.error("ack_tracker: get_pending_alerts failed: %s", e)
        return []


def increment_resend_count(message_id: str) -> bool:
    """
    Increment the resend attempt counter for an alert.

    Args:
        message_id: The message ID being resent.

    Returns:
        True on success, False on error.
    """
    _ensure_db()

    with _lock:
        try:
            conn = _connect()
            conn.execute(
                """
                UPDATE alert_ack_state
                SET resend_count = resend_count + 1,
                    last_resend_at = ?
                WHERE message_id = ?
                """,
                (_now_utc(), message_id),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error("ack_tracker: increment_resend_count failed: %s", e)
            return False


def mark_escalated(message_id: str) -> bool:
    """
    Mark an alert as escalated (page sent to on-call).

    Args:
        message_id: The message ID being escalated.

    Returns:
        True on success, False on error.
    """
    _ensure_db()

    with _lock:
        try:
            conn = _connect()
            conn.execute(
                """
                UPDATE alert_ack_state
                SET escalated = 1
                WHERE message_id = ?
                """,
                (message_id,),
            )
            conn.commit()
            conn.close()
            logger.info("ack_tracker: escalation marked for message_id=%s", message_id)
            return True
        except Exception as e:
            logger.error("ack_tracker: mark_escalated failed: %s", e)
            return False


def get_alert_summary() -> Dict:
    """
    Get summary of alert ACK status for monitoring.

    Returns:
        Dict with keys:
        total_tracked, acked, pending, pending_resend, pending_escalate
    """
    _ensure_db()

    try:
        conn = _connect()

        total = conn.execute(
            "SELECT COUNT(*) as count FROM alert_ack_state"
        ).fetchone()

        acked = conn.execute(
            "SELECT COUNT(*) as count FROM alert_ack_state WHERE ack_received_at IS NOT NULL"
        ).fetchone()

        pending = conn.execute(
            "SELECT COUNT(*) as count FROM alert_ack_state WHERE ack_received_at IS NULL"
        ).fetchone()

        # Pending resend: older than 5 min, not ACKed
        resend_cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=RESEND_TIMEOUT_MIN)
        ).isoformat()
        pending_resend = conn.execute(
            """
            SELECT COUNT(*) as count FROM alert_ack_state
            WHERE ack_received_at IS NULL AND sent_at < ? AND escalated = 0
            """,
            (resend_cutoff,),
        ).fetchone()

        # Pending escalate: older than 10 min, not ACKed, not yet escalated
        escalate_cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=ESCALATE_TIMEOUT_MIN)
        ).isoformat()
        pending_escalate = conn.execute(
            """
            SELECT COUNT(*) as count FROM alert_ack_state
            WHERE ack_received_at IS NULL AND sent_at < ? AND escalated = 0
            """,
            (escalate_cutoff,),
        ).fetchone()

        conn.close()

        return {
            "total_tracked": total["count"],
            "acked": acked["count"],
            "pending": pending["count"],
            "pending_resend": pending_resend["count"],
            "pending_escalate": pending_escalate["count"],
        }

    except Exception as e:
        logger.error("ack_tracker: get_alert_summary failed: %s", e)
        return {
            "total_tracked": 0,
            "acked": 0,
            "pending": 0,
            "pending_resend": 0,
            "pending_escalate": 0,
        }


def cleanup_old_records(older_than_hours: int = 24) -> int:
    """
    Remove ACK records older than N hours (maintenance).

    Args:
        older_than_hours: Delete records older than this (default 24h).

    Returns:
        Number of records deleted.
    """
    _ensure_db()

    with _lock:
        try:
            conn = _connect()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
            ).isoformat()
            cur = conn.execute(
                "DELETE FROM alert_ack_state WHERE sent_at < ?",
                (cutoff,),
            )
            conn.commit()
            conn.close()
            deleted = cur.rowcount
            logger.info("ack_tracker: cleaned %d old records", deleted)
            return deleted
        except Exception as e:
            logger.error("ack_tracker: cleanup_old_records failed: %s", e)
            return 0
