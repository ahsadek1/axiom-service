"""
triad_db.py — Iron Triad RESILIENCE-DB
Spec: resilience-phase1-infrastructure.md v1.0
Author: GENESIS | Built: 2026-05-02

Shared SQLite database for GENESIS, VECTOR, and Cipher.
Location: /Users/ahmedsadek/nexus/shared/resilience/resilience.db

All writes use BEGIN IMMEDIATE to prevent concurrent write contention.
All reads are direct SELECT — no locking needed.

NEVER raises — callers must not be blocked by DB failures.
Log loudly, swallow, continue.

Tables:
  intervention_log      — every infrastructure action any agent takes
  near_miss_log         — failures that self-recovered (hidden fragility)
  invariant_violations  — logic invariants caught by Cipher
  deployment_history    — deploy fingerprints for rollback
  quarantine_log        — bus directives that failed validation
  circuit_registry      — circuit breaker state (local cache)
  alert_dedup           — alert router cooldown tracking
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger("resilience.triad_db")

# ---------------------------------------------------------------------------
# Database path
# ---------------------------------------------------------------------------

DB_PATH = Path("/Users/ahmedsadek/nexus/shared/resilience/resilience.db")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS intervention_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent           TEXT    NOT NULL,
    action_type     TEXT    NOT NULL,
    target          TEXT    NOT NULL,
    trigger         TEXT    NOT NULL,
    command         TEXT,
    pre_state       TEXT,
    post_state      TEXT,
    outcome         TEXT    NOT NULL,
    elapsed_sec     REAL,
    error           TEXT,
    timestamp       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS near_miss_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    agent            TEXT    NOT NULL,
    service          TEXT    NOT NULL,
    error_signature  TEXT    NOT NULL,
    recovered_by     TEXT    NOT NULL,
    elapsed_sec      REAL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    first_seen       TEXT    NOT NULL,
    last_seen        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS invariant_violations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    invariant_name TEXT    NOT NULL,
    expected       TEXT    NOT NULL,
    actual         TEXT    NOT NULL,
    source_service TEXT    NOT NULL,
    severity       TEXT    NOT NULL,
    resolved       INTEGER NOT NULL DEFAULT 0,
    timestamp      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS deployment_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    service          TEXT    NOT NULL,
    pre_deploy_sha   TEXT    NOT NULL,
    post_deploy_sha  TEXT    NOT NULL,
    deployed_by      TEXT    NOT NULL,
    deploy_timestamp TEXT    NOT NULL,
    health_verified  INTEGER NOT NULL DEFAULT 0,
    rolled_back      INTEGER NOT NULL DEFAULT 0,
    rollback_reason  TEXT
);

CREATE TABLE IF NOT EXISTS quarantine_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    agent             TEXT    NOT NULL,
    raw_directive     TEXT    NOT NULL,
    quarantine_reason TEXT    NOT NULL,
    timestamp         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS circuit_registry (
    service        TEXT PRIMARY KEY,
    state          TEXT NOT NULL DEFAULT 'CLOSED',
    opened_at      TEXT,
    opened_reason  TEXT,
    last_refreshed TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_dedup (
    dedup_key    TEXT PRIMARY KEY,
    last_sent_at TEXT NOT NULL,
    send_count   INTEGER NOT NULL DEFAULT 1
);
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _immediate(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE transaction context manager."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Schema migration — idempotent, run-once guard per table
# ---------------------------------------------------------------------------

def init_triad_db() -> None:
    """
    Initialize RESILIENCE-DB schema. Idempotent — safe to call on every startup.
    Creates tables if they don't exist; never drops existing data.
    """
    try:
        conn = _get_conn()
        conn.executescript(_SCHEMA)
        conn.close()
        logger.info("RESILIENCE-DB initialized at %s", DB_PATH)
    except Exception as e:
        logger.error("RESILIENCE-DB init failed: %s", e)


# ---------------------------------------------------------------------------
# intervention_log
# ---------------------------------------------------------------------------

def log_intervention(
    agent: str,
    action_type: str,
    target: str,
    trigger: str,
    outcome: str,
    command: Optional[str] = None,
    pre_state: Optional[dict] = None,
    post_state: Optional[dict] = None,
    elapsed_sec: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    """
    Log an infrastructure action to intervention_log.
    Never raises — DB failure must not block the action that triggered it.

    Args:
        agent:       "genesis" | "vector" | "cipher"
        action_type: "service_restart" | "rollback" | "escalation" | "fix_deploy" | "triad_signal"
        target:      Service name or component affected
        trigger:     What caused the action (e.g. "health_check_failed:HTTP_503")
        outcome:     "recovered" | "escalated" | "failed" | "rolled_back"
        command:     Exact shell command run, if any
        pre_state:   State dict before action
        post_state:  State dict after action
        elapsed_sec: How long the action took
        error:       Error message if outcome=failed
    """
    try:
        conn = _get_conn()
        with _immediate(conn):
            conn.execute(
                """INSERT INTO intervention_log
                   (agent, action_type, target, trigger, command,
                    pre_state, post_state, outcome, elapsed_sec, error, timestamp)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    agent, action_type, target, trigger,
                    command,
                    json.dumps(pre_state) if pre_state else None,
                    json.dumps(post_state) if post_state else None,
                    outcome, elapsed_sec, error,
                    _now_iso(),
                ),
            )
        conn.close()
    except Exception as e:
        logger.error("log_intervention failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# near_miss_log
# ---------------------------------------------------------------------------

def log_near_miss(
    agent: str,
    service: str,
    error_signature: str,
    recovered_by: str,
    elapsed_sec: Optional[float] = None,
) -> None:
    """
    Log a self-recovered failure. Updates occurrence_count if same signature seen before.
    Never raises.
    """
    try:
        conn = _get_conn()
        now = _now_iso()
        with _immediate(conn):
            existing = conn.execute(
                "SELECT id, occurrence_count FROM near_miss_log "
                "WHERE agent=? AND service=? AND error_signature=?",
                (agent, service, error_signature),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE near_miss_log SET occurrence_count=?, last_seen=? WHERE id=?",
                    (existing["occurrence_count"] + 1, now, existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO near_miss_log
                       (agent, service, error_signature, recovered_by,
                        elapsed_sec, occurrence_count, first_seen, last_seen)
                       VALUES (?,?,?,?,?,1,?,?)""",
                    (agent, service, error_signature, recovered_by, elapsed_sec, now, now),
                )
        conn.close()
    except Exception as e:
        logger.error("log_near_miss failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# invariant_violations
# ---------------------------------------------------------------------------

def log_invariant_violation(
    invariant_name: str,
    expected,
    actual,
    source_service: str,
    severity: str,
) -> None:
    """
    Log a logic invariant violation caught by Cipher.
    severity: "P0" | "P1" | "P2"
    Never raises.
    """
    try:
        conn = _get_conn()
        with _immediate(conn):
            conn.execute(
                """INSERT INTO invariant_violations
                   (invariant_name, expected, actual, source_service, severity, timestamp)
                   VALUES (?,?,?,?,?,?)""",
                (
                    invariant_name,
                    json.dumps(expected),
                    json.dumps(actual),
                    source_service, severity,
                    _now_iso(),
                ),
            )
        conn.close()
    except Exception as e:
        logger.error("log_invariant_violation failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# deployment_history
# ---------------------------------------------------------------------------

def log_deployment(
    service: str,
    pre_deploy_sha: str,
    post_deploy_sha: str,
    deployed_by: str = "genesis",
) -> int:
    """
    Log a deployment fingerprint. Returns the row id for later health verification update.
    Returns -1 on failure. Never raises.
    """
    try:
        conn = _get_conn()
        with _immediate(conn):
            cursor = conn.execute(
                """INSERT INTO deployment_history
                   (service, pre_deploy_sha, post_deploy_sha, deployed_by, deploy_timestamp)
                   VALUES (?,?,?,?,?)""",
                (service, pre_deploy_sha, post_deploy_sha, deployed_by, _now_iso()),
            )
            row_id = cursor.lastrowid
        conn.close()
        return row_id
    except Exception as e:
        logger.error("log_deployment failed (non-fatal): %s", e)
        return -1


def mark_deployment_healthy(deployment_id: int) -> None:
    """Mark a deployment as health-verified. Never raises."""
    try:
        conn = _get_conn()
        with _immediate(conn):
            conn.execute(
                "UPDATE deployment_history SET health_verified=1 WHERE id=?",
                (deployment_id,),
            )
        conn.close()
    except Exception as e:
        logger.error("mark_deployment_healthy failed (non-fatal): %s", e)


def mark_deployment_rolled_back(deployment_id: int, reason: str) -> None:
    """Mark a deployment as rolled back. Never raises."""
    try:
        conn = _get_conn()
        with _immediate(conn):
            conn.execute(
                "UPDATE deployment_history SET rolled_back=1, rollback_reason=? WHERE id=?",
                (reason, deployment_id),
            )
        conn.close()
    except Exception as e:
        logger.error("mark_deployment_rolled_back failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# quarantine_log
# ---------------------------------------------------------------------------

def quarantine_directive(
    agent: str,
    raw_directive: str,
    quarantine_reason: str,
) -> None:
    """
    Quarantine a malformed or injected bus directive. Never raises.
    Quarantined directives are never re-processed.
    """
    try:
        conn = _get_conn()
        with _immediate(conn):
            conn.execute(
                """INSERT INTO quarantine_log
                   (agent, raw_directive, quarantine_reason, timestamp)
                   VALUES (?,?,?,?)""",
                (agent, str(raw_directive)[:2000], quarantine_reason, _now_iso()),
            )
        conn.close()
    except Exception as e:
        logger.error("quarantine_directive failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# circuit_registry
# ---------------------------------------------------------------------------

def get_circuit_state(service: str) -> str:
    """
    Get circuit breaker state for a service.
    Returns "CLOSED" (fail-open) if service not found or DB error.
    Never raises.
    """
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT state FROM circuit_registry WHERE service=?", (service,)
        ).fetchone()
        conn.close()
        return row["state"] if row else "CLOSED"
    except Exception as e:
        logger.error("get_circuit_state failed (fail-open): %s", e)
        return "CLOSED"


def set_circuit_state(service: str, state: str, reason: Optional[str] = None) -> None:
    """
    Update circuit breaker state. state: "OPEN" | "CLOSED" | "HALF_OPEN"
    Never raises.
    """
    try:
        conn = _get_conn()
        now = _now_iso()
        with _immediate(conn):
            conn.execute(
                """INSERT INTO circuit_registry (service, state, opened_at, opened_reason, last_refreshed)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(service) DO UPDATE SET
                     state=excluded.state,
                     opened_at=CASE WHEN excluded.state='OPEN' THEN excluded.opened_at ELSE opened_at END,
                     opened_reason=CASE WHEN excluded.state='OPEN' THEN excluded.opened_reason ELSE opened_reason END,
                     last_refreshed=excluded.last_refreshed""",
                (service, state, now if state == "OPEN" else None, reason, now),
            )
        conn.close()
    except Exception as e:
        logger.error("set_circuit_state failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# alert_dedup
# ---------------------------------------------------------------------------

def check_and_record_alert(dedup_key: str, cooldown_seconds: int) -> bool:
    """
    Check if an alert should be sent (not in cooldown) and record it.
    Returns True if alert should be sent, False if suppressed by cooldown.
    Never raises — returns True on error (fail-open: better to over-alert than under-alert).

    Args:
        dedup_key:        Unique key for this alert type (issue_type + target + severity)
        cooldown_seconds: Minimum seconds between alerts for this key
    """
    try:
        conn = _get_conn()
        now = time.time()
        now_iso = _now_iso()
        suppressed = False
        with _immediate(conn):
            row = conn.execute(
                "SELECT last_sent_at, send_count FROM alert_dedup WHERE dedup_key=?",
                (dedup_key,),
            ).fetchone()

            if row:
                last_sent = row["last_sent_at"]
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(last_sent)
                    age = now - dt.timestamp()
                except Exception:
                    age = cooldown_seconds + 1  # can't parse → send it

                if age < cooldown_seconds:
                    suppressed = True  # mark suppressed but let with-block commit cleanly
                else:
                    conn.execute(
                        "UPDATE alert_dedup SET last_sent_at=?, send_count=send_count+1 WHERE dedup_key=?",
                        (now_iso, dedup_key),
                    )
            else:
                conn.execute(
                    "INSERT INTO alert_dedup (dedup_key, last_sent_at, send_count) VALUES (?,?,1)",
                    (dedup_key, now_iso),
                )
        conn.close()
        if suppressed:
            return False
        return True
    except Exception as e:
        logger.error("check_and_record_alert failed (fail-open — sending alert): %s", e)
        return True  # fail-open: never suppress alert on DB error
