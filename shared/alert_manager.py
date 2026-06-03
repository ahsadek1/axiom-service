"""
alert_manager.py — Nexus Persistent Alert State Manager
========================================================
Single source of truth for all currently known issues across ALL agents.

This closes the agent observability loop:
  - Any alert raised by any agent is persisted in CHRONICLE
  - Diagnostic crons can check is_known() before acting to avoid duplicate work
  - Alerts are tracked from OPEN → IN_PROGRESS → RESOLVED

Usage:
    from shared.alert_manager import write_alert, get_open_alerts, ack_alert, resolve_alert, is_known

    # Before raising an alert — check if already known:
    if not is_known("omni:silence"):
        write_alert(
            agent="genesis",
            severity="WARNING",
            service="omni",
            issue_key="omni:silence",
            title="OMNI silent for 31min",
            details="No synthesis since 14:31 ET",
        )

    # When actioning:
    ack_alert("omni:silence", handled_by="genesis")

    # When fixed:
    resolve_alert("omni:silence", resolution="Alpha-buffer concordance threshold review — scoring calibration")

FALLBACK: If CHRONICLE is unreachable, falls back to a local JSONL file.
          is_known() returns False on fallback (safe — proceed with diagnosis).

Author: GENESIS | 2026-05-05
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("nexus.alert_manager")

CHRONICLE_DB: str = os.getenv("CHRONICLE_DB", "/Users/ahmedsadek/nexus/data/chronicle.db")
FALLBACK_PATH: str = os.getenv(
    "ALERT_MANAGER_FALLBACK",
    "/Users/ahmedsadek/nexus/data/alert_manager_fallback.jsonl",
)

_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Internal: DB helpers
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    """Open a WAL connection to CHRONICLE."""
    conn = sqlite3.connect(CHRONICLE_DB, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create agent_alerts table if it doesn't exist (idempotent)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent       TEXT    NOT NULL,
            severity    TEXT    NOT NULL
                        CHECK(severity IN ('CRITICAL','WARNING','INFO')),
            service     TEXT    NOT NULL,
            issue_key   TEXT    NOT NULL,
            title       TEXT    NOT NULL,
            details     TEXT    NOT NULL DEFAULT '',
            status      TEXT    NOT NULL DEFAULT 'OPEN'
                        CHECK(status IN ('OPEN','IN_PROGRESS','RESOLVED','SUPPRESSED')),
            opened_at   TEXT    NOT NULL
                        DEFAULT (datetime('now','utc')),
            updated_at  TEXT    NOT NULL
                        DEFAULT (datetime('now','utc')),
            resolved_at TEXT,
            handled_by  TEXT,
            resolution  TEXT,
            notify_sent INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_alerts_open_key
            ON agent_alerts(issue_key)
            WHERE status IN ('OPEN','IN_PROGRESS');
    """)
    conn.commit()


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Internal: Fallback JSONL writer (when CHRONICLE is unreachable)
# ---------------------------------------------------------------------------


def _fallback_write(entry: Dict) -> None:
    """Append alert entry to fallback JSONL file."""
    try:
        Path(FALLBACK_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(FALLBACK_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        logger.error("alert_manager: fallback write failed: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_alert(
    agent: str,
    severity: str,
    service: str,
    issue_key: str,
    title: str,
    details: str = "",
) -> int:
    """
    Write a new alert to CHRONICLE agent_alerts.

    Idempotent: if an OPEN or IN_PROGRESS alert with the same issue_key
    already exists, returns -1 without creating a duplicate.

    Args:
        agent:     Agent raising the alert (e.g. "genesis", "omni").
        severity:  "CRITICAL" | "WARNING" | "INFO"
        service:   Service name (e.g. "omni", "alpha-buffer").
        issue_key: Dedup key (e.g. "omni:silence", "alpha-buffer:no-concordance").
        title:     One-line summary.
        details:   Optional detail text.

    Returns:
        Alert ID (int) if newly created.
        -1 if duplicate (already OPEN/IN_PROGRESS) or on DB error (safe default).
    """
    now = _now_utc()
    severity = severity.upper()

    with _lock:
        try:
            conn = _connect()
            _ensure_table(conn)

            # Check for existing open alert with same key
            existing = conn.execute(
                "SELECT id FROM agent_alerts WHERE issue_key=? AND status IN ('OPEN','IN_PROGRESS')",
                (issue_key,),
            ).fetchone()

            if existing:
                logger.debug(
                    "alert_manager: issue_key='%s' already OPEN/IN_PROGRESS (id=%d) — skipping",
                    issue_key, existing["id"],
                )
                conn.close()
                return -1

            cursor = conn.execute(
                """
                INSERT INTO agent_alerts
                    (agent, severity, service, issue_key, title, details, status, opened_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (agent, severity, service, issue_key, title, details, now, now),
            )
            conn.commit()
            alert_id = cursor.lastrowid
            conn.close()
            logger.info(
                "alert_manager: [OPEN] id=%d agent=%s service=%s key=%s title=%s",
                alert_id, agent, service, issue_key, title,
            )
            return alert_id

        except Exception as exc:
            logger.error("alert_manager: write_alert failed: %s", exc)
            _fallback_write({
                "ts": now, "op": "write", "agent": agent, "severity": severity,
                "service": service, "issue_key": issue_key, "title": title, "details": details,
            })
            return -1


def get_open_alerts(service: Optional[str] = None) -> List[Dict]:
    """
    Return all OPEN and IN_PROGRESS alerts.

    Args:
        service: Optional filter by service name.

    Returns:
        List of dicts with keys:
        id, agent, severity, service, issue_key, title, details, status,
        opened_at, updated_at, handled_by.
        Empty list on error (safe default).
    """
    try:
        conn = _connect()
        _ensure_table(conn)

        if service:
            rows = conn.execute(
                """
                SELECT id, agent, severity, service, issue_key, title, details,
                       status, opened_at, updated_at, handled_by
                FROM agent_alerts
                WHERE status IN ('OPEN','IN_PROGRESS') AND service=?
                ORDER BY opened_at ASC
                """,
                (service,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, agent, severity, service, issue_key, title, details,
                       status, opened_at, updated_at, handled_by
                FROM agent_alerts
                WHERE status IN ('OPEN','IN_PROGRESS')
                ORDER BY opened_at ASC
                """,
            ).fetchall()

        conn.close()
        return [dict(r) for r in rows]

    except Exception as exc:
        logger.error("alert_manager: get_open_alerts failed: %s", exc)
        return []


def is_known(issue_key: str) -> bool:
    """
    Return True if an OPEN or IN_PROGRESS alert exists for this issue_key.

    This is the primary guard for diagnostic crons — call this BEFORE
    raising a new alert or kicking off diagnosis work.

    Safe default on error: returns False (allow diagnosis to proceed).
    """
    try:
        conn = _connect()
        _ensure_table(conn)
        row = conn.execute(
            "SELECT id FROM agent_alerts WHERE issue_key=? AND status IN ('OPEN','IN_PROGRESS')",
            (issue_key,),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception as exc:
        logger.error("alert_manager: is_known failed: %s", exc)
        return False  # Safe default: proceed with diagnosis


def ack_alert(issue_key: str, handled_by: str) -> bool:
    """
    Mark an alert as IN_PROGRESS (agent has picked it up and is working on it).

    Args:
        issue_key:  The dedup key of the alert to acknowledge.
        handled_by: Agent name taking responsibility (e.g. "genesis").

    Returns:
        True on success, False if alert not found or DB error.
    """
    now = _now_utc()
    with _lock:
        try:
            conn = _connect()
            _ensure_table(conn)
            cur = conn.execute(
                """
                UPDATE agent_alerts
                SET status='IN_PROGRESS', handled_by=?, updated_at=?
                WHERE issue_key=? AND status='OPEN'
                """,
                (handled_by, now, issue_key),
            )
            conn.commit()
            conn.close()
            if cur.rowcount > 0:
                logger.info("alert_manager: [IN_PROGRESS] key=%s handled_by=%s", issue_key, handled_by)
                return True
            logger.debug("alert_manager: ack_alert no-op for key=%s (not OPEN)", issue_key)
            return False
        except Exception as exc:
            logger.error("alert_manager: ack_alert failed: %s", exc)
            return False


def resolve_alert(issue_key: str, resolution: str, resolved_by: Optional[str] = None) -> bool:
    """
    Mark an alert as RESOLVED.

    Args:
        issue_key:   The dedup key of the alert to resolve.
        resolution:  Description of what fixed it (logged to CHRONICLE).
        resolved_by: Optional agent name that resolved it.

    Returns:
        True on success, False if alert not found or DB error.
    """
    now = _now_utc()
    with _lock:
        try:
            conn = _connect()
            _ensure_table(conn)
            handled = resolved_by or "genesis"
            cur = conn.execute(
                """
                UPDATE agent_alerts
                SET status='RESOLVED', resolution=?, resolved_at=?, updated_at=?,
                    handled_by=COALESCE(NULLIF(handled_by,''), ?)
                WHERE issue_key=? AND status IN ('OPEN','IN_PROGRESS')
                """,
                (resolution, now, now, handled, issue_key),
            )
            conn.commit()
            conn.close()
            if cur.rowcount > 0:
                logger.info(
                    "alert_manager: [RESOLVED] key=%s resolution=%s",
                    issue_key, resolution[:80],
                )
                return True
            logger.debug("alert_manager: resolve_alert no-op for key=%s (not open)", issue_key)
            return False
        except Exception as exc:
            logger.error("alert_manager: resolve_alert failed: %s", exc)
            return False


def suppress_alert(issue_key: str, reason: str = "") -> bool:
    """
    Mark an alert as SUPPRESSED (known non-issue, e.g. end-of-day silence).

    Args:
        issue_key: The dedup key to suppress.
        reason:    Why it's being suppressed.

    Returns:
        True on success, False on error.
    """
    now = _now_utc()
    with _lock:
        try:
            conn = _connect()
            _ensure_table(conn)
            cur = conn.execute(
                """
                UPDATE agent_alerts
                SET status='SUPPRESSED', resolution=?, updated_at=?
                WHERE issue_key=? AND status IN ('OPEN','IN_PROGRESS')
                """,
                (reason, now, issue_key),
            )
            conn.commit()
            conn.close()
            return cur.rowcount > 0
        except Exception as exc:
            logger.error("alert_manager: suppress_alert failed: %s", exc)
            return False


def get_alert(issue_key: str) -> Optional[Dict]:
    """
    Get the current open/in-progress alert for an issue_key. None if not found.
    """
    try:
        conn = _connect()
        _ensure_table(conn)
        row = conn.execute(
            """
            SELECT * FROM agent_alerts
            WHERE issue_key=? AND status IN ('OPEN','IN_PROGRESS')
            ORDER BY opened_at DESC LIMIT 1
            """,
            (issue_key,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as exc:
        logger.error("alert_manager: get_alert failed: %s", exc)
        return None


def get_recent_resolved(hours: int = 4) -> List[Dict]:
    """
    Return alerts resolved in the last N hours (for diagnostic summaries).
    """
    try:
        conn = _connect()
        _ensure_table(conn)
        rows = conn.execute(
            """
            SELECT id, agent, service, issue_key, title, resolved_at, resolution
            FROM agent_alerts
            WHERE status='RESOLVED'
              AND resolved_at >= datetime('now', ?, 'utc')
            ORDER BY resolved_at DESC
            """,
            (f"-{hours} hours",),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("alert_manager: get_recent_resolved failed: %s", exc)
        return []
