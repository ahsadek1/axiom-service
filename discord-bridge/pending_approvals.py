"""
pending_approvals.py — SQLite-backed store for open approval requests.

Tracks approval requests posted to #ahmed-decisions, their status,
and the Discord message ID needed to update the embed after a decision.
"""
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).parent / "pending_approvals.db")
EXPIRY_SECONDS = 48 * 3600  # 48 hours


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: str = DB_PATH) -> None:
    """Initialize the pending approvals table."""
    conn = _connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_approvals (
                request_id  TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                agent       TEXT NOT NULL,
                reply_to    TEXT NOT NULL,
                message_id  TEXT,
                channel_id  TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  REAL NOT NULL,
                decided_at  REAL,
                payload     TEXT
            )
        """)
        conn.commit()
        logger.info(f"Pending approvals DB initialized at {db_path}")
    finally:
        conn.close()
    expire_old(db_path=db_path)


def insert(
    request_id:  str,
    title:       str,
    agent:       str,
    reply_to:    str,
    payload:     Dict[str, Any],
    db_path:     str = DB_PATH,
) -> None:
    """Insert a new pending approval request."""
    conn = _connect(db_path)
    try:
        conn.execute("""
            INSERT OR IGNORE INTO pending_approvals
                (request_id, title, agent, reply_to, status, created_at, payload)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """, (request_id, title, agent, reply_to, time.time(), json.dumps(payload)))
        conn.commit()
    finally:
        conn.close()


def set_message(request_id: str, message_id: str, channel_id: str,
                db_path: str = DB_PATH) -> None:
    """Store the Discord message ID after the embed is posted."""
    conn = _connect(db_path)
    try:
        conn.execute("""
            UPDATE pending_approvals
            SET message_id = ?, channel_id = ?
            WHERE request_id = ?
        """, (message_id, channel_id, request_id))
        conn.commit()
    finally:
        conn.close()


def get(request_id: str, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Retrieve an approval request by ID. Returns None if not found."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM pending_approvals WHERE request_id = ?",
            (request_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_decided(request_id: str, decision: str,
                 db_path: str = DB_PATH) -> None:
    """Mark an approval request as approved or rejected."""
    conn = _connect(db_path)
    try:
        conn.execute("""
            UPDATE pending_approvals
            SET status = ?, decided_at = ?
            WHERE request_id = ? AND status = 'pending'
        """, (decision, time.time(), request_id))
        conn.commit()
    finally:
        conn.close()


def expire_old(db_path: str = DB_PATH) -> int:
    """
    Expire pending requests older than EXPIRY_SECONDS.
    Returns count of expired items.
    """
    cutoff = time.time() - EXPIRY_SECONDS
    conn = _connect(db_path)
    try:
        cur = conn.execute("""
            UPDATE pending_approvals
            SET status = 'expired', decided_at = ?
            WHERE status = 'pending' AND created_at < ?
        """, (time.time(), cutoff))
        conn.commit()
        count = cur.rowcount
        if count:
            logger.info(f"Expired {count} stale approval request(s)")
        return count
    finally:
        conn.close()


def list_pending(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Return all currently pending approval requests."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM pending_approvals WHERE status = 'pending' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
