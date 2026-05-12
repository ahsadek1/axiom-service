"""
database.py — SQLite operations for Pipeline Sentinel.

All functions are fail-safe: exceptions are caught and logged, never propagated
to the caller. The pipeline must never be blocked by a monitoring write failure.

WAL mode is enabled for concurrent read/write safety.
On startup, PRAGMA integrity_check is run — corrupt DB is deleted and recreated.
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from models import AnomalyReport, SystemHealthResponse, TraceRequest

logger = logging.getLogger("sentinel.database")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id    TEXT    NOT NULL,
    ticker      TEXT    NOT NULL,
    pathway     TEXT    NOT NULL CHECK(pathway IN ('alpha','prime')),
    hop         TEXT    NOT NULL,
    service     TEXT    NOT NULL,
    status      TEXT    NOT NULL CHECK(status IN ('ok','error','timeout')),
    metadata    TEXT,
    ts          REAL    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_trace_id ON traces(trace_id);
CREATE INDEX IF NOT EXISTS idx_traces_ts       ON traces(ts);

CREATE TABLE IF NOT EXISTS health_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    score           REAL    NOT NULL,
    status          TEXT    NOT NULL,
    size_multiplier REAL    NOT NULL,
    submissions_open INTEGER NOT NULL,
    components      TEXT    NOT NULL,
    active_failures TEXT    NOT NULL,
    ts              REAL    NOT NULL,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_ts ON health_scores(ts);

CREATE TABLE IF NOT EXISTS failure_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    failure_class   TEXT    NOT NULL,
    trace_id        TEXT,
    detail          TEXT    NOT NULL,
    auto_recovered  INTEGER NOT NULL DEFAULT 0,
    resolved        INTEGER NOT NULL DEFAULT 0,
    resolved_at     TEXT,
    ts              REAL    NOT NULL,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failures_class ON failure_events(failure_class);

CREATE TABLE IF NOT EXISTS anomaly_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    anomaly_type TEXT NOT NULL,
    service      TEXT NOT NULL,
    detail       TEXT NOT NULL,
    ts           REAL NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anomaly_ts ON anomaly_reports(ts);
"""


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> sqlite3.Connection:
    """
    Initialize the SQLite database.

    Runs PRAGMA integrity_check on startup. If the database is corrupt,
    it is deleted and recreated from scratch (learned from Guardian Angel incident).

    Args:
        db_path: Absolute path to the SQLite file.

    Returns:
        An open sqlite3.Connection in WAL mode.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        # Integrity check before opening for real
        try:
            check_conn = sqlite3.connect(str(path))
            result = check_conn.execute("PRAGMA integrity_check").fetchone()
            check_conn.close()
            if result and result[0] != "ok":
                logger.error("DB integrity check FAILED (%s). Deleting and recreating.", result[0])
                path.unlink(missing_ok=True)
                for suffix in ("-shm", "-wal"):
                    sidecar = Path(str(path) + suffix)
                    sidecar.unlink(missing_ok=True)
        except Exception as exc:
            logger.error("DB integrity check raised: %s. Deleting and recreating.", exc)
            path.unlink(missing_ok=True)
            for suffix in ("-shm", "-wal"):
                sidecar = Path(str(path) + suffix)
                sidecar.unlink(missing_ok=True)

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    logger.info("Database initialized at %s", db_path)
    return conn


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------

def insert_trace(db_path: str, req: TraceRequest) -> None:
    """
    Insert a pipeline hop trace. Never raises — the pipeline must not be blocked.

    Args:
        db_path: Path to the SQLite database.
        req:     Validated TraceRequest from the API layer.
    """
    try:
        now = time.time()
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            INSERT INTO traces (trace_id, ticker, pathway, hop, service, status, metadata, ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                req.trace_id,
                req.ticker,
                req.pathway.value,
                req.hop.value,
                req.service.value,
                req.status.value,
                json.dumps(req.metadata or {}),
                now,
                datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("insert_trace failed (non-critical): %s", exc)


def get_traces_for_id(db_path: str, trace_id: str) -> List[Dict[str, Any]]:
    """
    Return all recorded hops for a given trace_id, ordered by timestamp.

    Args:
        db_path:  Path to the SQLite database.
        trace_id: UUID4 of the pick.

    Returns:
        List of hop dicts, ordered by ts ascending.
    """
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM traces WHERE trace_id=? ORDER BY ts ASC",
            (trace_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("get_traces_for_id failed: %s", exc)
        return []


def get_recent_traces(db_path: str, window_s: int = 900) -> List[Dict[str, Any]]:
    """
    Return all traces within the last window_s seconds.

    Args:
        db_path:  Path to the SQLite database.
        window_s: Lookback window in seconds (default 15 minutes).

    Returns:
        List of trace dicts ordered by ts ascending.
    """
    try:
        cutoff = time.time() - window_s
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM traces WHERE ts >= ? ORDER BY ts ASC",
            (cutoff,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("get_recent_traces failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Health scores
# ---------------------------------------------------------------------------

def insert_health_score(db_path: str, response: SystemHealthResponse) -> None:
    """
    Persist a computed health score snapshot.

    Args:
        db_path:  Path to the SQLite database.
        response: Computed SystemHealthResponse to store.
    """
    try:
        now = time.time()
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            INSERT INTO health_scores
                (score, status, size_multiplier, submissions_open, components, active_failures, ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                response.health_score,
                response.status,
                response.recommended_size_multiplier,
                1 if response.submissions_open else 0,
                response.score_components.json(),
                json.dumps(response.active_failures),
                now,
                datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("insert_health_score failed: %s", exc)


def get_latest_health_score(db_path: str) -> Optional[Dict[str, Any]]:
    """
    Return the most recently computed health score row.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        Dict of the latest row, or None if no scores exist yet.
    """
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM health_scores ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as exc:
        logger.error("get_latest_health_score failed: %s", exc)
        return None


def prune_old_scores(db_path: str, keep_days: int = 30) -> None:
    """
    Delete health score records older than keep_days.

    Args:
        db_path:   Path to the SQLite database.
        keep_days: Retention window in days (default 30).
    """
    try:
        cutoff = time.time() - (keep_days * 86400)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("DELETE FROM health_scores WHERE ts < ?", (cutoff,))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("prune_old_scores failed: %s", exc)


# ---------------------------------------------------------------------------
# Failure events
# ---------------------------------------------------------------------------

def insert_failure_event(
    db_path: str,
    failure_class: str,
    detail: str,
    trace_id: Optional[str] = None,
) -> None:
    """
    Record a new failure event. Idempotent — skips insert if same class is already active.

    Args:
        db_path:       Path to the SQLite database.
        failure_class: One of the 7 failure class names from the spec.
        detail:        Human-readable description of the failure.
        trace_id:      Associated pick trace_id if applicable.
    """
    try:
        now = time.time()
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        # Only insert if no unresolved event for this class already exists
        existing = conn.execute(
            "SELECT id FROM failure_events WHERE failure_class=? AND resolved=0",
            (failure_class,),
        ).fetchone()
        if not existing:
            conn.execute(
                """
                INSERT INTO failure_events
                    (failure_class, trace_id, detail, auto_recovered, resolved, ts, created_at)
                VALUES (?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    failure_class,
                    trace_id,
                    detail,
                    now,
                    datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("insert_failure_event failed: %s", exc)


def get_active_failures(db_path: str) -> List[str]:
    """
    Return list of currently active (unresolved) failure class names.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        List of failure_class strings.
    """
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        rows = conn.execute(
            "SELECT DISTINCT failure_class FROM failure_events WHERE resolved=0"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as exc:
        logger.error("get_active_failures failed: %s", exc)
        return []


def resolve_failure(db_path: str, failure_class: str) -> None:
    """
    Mark all active events for a failure class as resolved.

    Args:
        db_path:       Path to the SQLite database.
        failure_class: Failure class to resolve.
    """
    try:
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "UPDATE failure_events SET resolved=1, resolved_at=? WHERE failure_class=? AND resolved=0",
            (now_iso, failure_class),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("resolve_failure failed: %s", exc)


def get_stalled_picks(db_path: str, stall_window_s: int) -> List[Dict[str, Any]]:
    """
    Return picks that have not received a hop update within stall_window_s seconds
    and have not yet reached the final hop (alpaca_confirmed).

    Args:
        db_path:       Path to the SQLite database.
        stall_window_s: Seconds without activity before a pick is considered stalled.

    Returns:
        List of dicts with trace_id, ticker, pathway, last_hop, last_seen_ts.
    """
    try:
        cutoff = time.time() - stall_window_s
        # Only consider traces from the current trading session (last 12 hours)
        # to avoid counting all-time historical incomplete traces as stalls.
        session_start = time.time() - (12 * 3600)
        # Terminal hops: any of these means the pick reached a final state.
        # Picks without a terminal hop AND no activity for stall_window_s are stalled.
        conn = sqlite3.connect(db_path, check_same_thread=False)
        rows = conn.execute(
            """
            SELECT t.trace_id, t.ticker, t.pathway,
                   t.hop     AS last_hop,
                   MAX(t.ts) AS last_seen_ts
            FROM   traces t
            WHERE  t.ts > ?
            GROUP  BY t.trace_id
            HAVING MAX(t.ts) < ?
               AND t.hop NOT IN (
                   'alpaca_confirmed', 'alpaca_submitted',
                   'no_go_dropped', 'synthesis_complete',
                   'score_below_threshold', 'dropped'
               )
               AND t.hop != 'agent_received'
            """,
            (session_start, cutoff),
        ).fetchall()
        conn.close()
        return [
            {
                "trace_id":     r[0],
                "ticker":       r[1],
                "pathway":      r[2],
                "last_hop":     r[3],
                "last_seen_ts": r[4],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.error("get_stalled_picks failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Anomaly reports
# ---------------------------------------------------------------------------

def insert_anomaly_report(db_path: str, report: AnomalyReport) -> None:
    """
    Store an anomaly report received from Guardian Angel v3.

    Args:
        db_path: Path to the SQLite database.
        report:  Validated AnomalyReport from the API layer.
    """
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            INSERT INTO anomaly_reports (source, anomaly_type, service, detail, ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                report.source,
                report.anomaly_type,
                report.service,
                report.detail,
                report.ts,
                datetime.fromtimestamp(report.ts, tz=timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("insert_anomaly_report failed: %s", exc)


def get_recent_anomalies(db_path: str, window_s: int = 300) -> List[Dict[str, Any]]:
    """
    Return anomaly reports within the last window_s seconds.

    Args:
        db_path:  Path to the SQLite database.
        window_s: Lookback window in seconds (default 5 minutes).

    Returns:
        List of anomaly report dicts.
    """
    try:
        cutoff = time.time() - window_s
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM anomaly_reports WHERE ts >= ? ORDER BY ts DESC",
            (cutoff,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("get_recent_anomalies failed: %s", exc)
        return []
