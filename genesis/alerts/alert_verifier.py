"""
genesis/alerts/alert_verifier.py — Automatic Alert Verification Engine
========================================================================

Core verification dispatcher. After Ahmed ACKs an alert, this module:
1. Determines the alert type
2. Routes to the appropriate checker
3. Collects evidence
4. Marks alert as VERIFIED, FAILED_VERIFICATION, or MANUAL_REQUIRED
5. Logs all results to Chronicle

Verification runs asynchronously after ACK. Results are logged but not
automatically re-escalated by this module (that's alert_failover's job).

Usage:
    from genesis.alerts.alert_verifier import verify_alert_async

    # After alert is ACKed, trigger verification
    verify_alert_async(
        alert_id="genesis-1715857200-1",
        alert_type="ORPHAN_POSITION",
        context={"position_id": "aapl-long-1000"}
    )

    # In verification loop (runs periodically):
    from genesis.alerts.alert_verifier import check_pending_verifications
    check_pending_verifications()

Author: GENESIS 🌱
Date: 2026-05-16
"""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional, Any, Literal
from enum import Enum

from genesis.alerts.verification_checkers import (
    verify_orphan_position,
    verify_agent_hung,
    verify_code_failure,
    verify_config_mismatch,
    verify_policy_violation,
    verify_threat_detected,
)

logger = logging.getLogger("genesis.alerts.verifier")

# ── Type Definitions ─────────────────────────────────────────────────────────

class VerificationResult(str, Enum):
    """Possible verification outcomes."""
    VERIFIED = "VERIFIED"
    FAILED_VERIFICATION = "FAILED_VERIFICATION"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
    TIMEOUT = "TIMEOUT"
    IN_PROGRESS = "IN_PROGRESS"


# ── Configuration ────────────────────────────────────────────────────────────

CHRONICLE_DB = os.getenv(
    "NEXUS_CHRONICLE_DB",
    "/Users/ahmedsadek/nexus/data/chronicle.db",
)
VERIFICATION_TIMEOUT_MULTIPLIER = 1.5  # Allow 50% buffer beyond base timeout

# Alert type -> timeout in minutes
VERIFICATION_TIMEOUTS = {
    "ORPHAN_POSITION": 5,
    "AGENT_HUNG": 2,
    "CODE_FAILURE": 10,
    "CONFIG_MISMATCH": 3,
    "POLICY_VIOLATION": 5,
    "THREAT_DETECTED": 2,
}

# Alert type -> verification function
VERIFICATION_CHECKERS = {
    "ORPHAN_POSITION": verify_orphan_position,
    "AGENT_HUNG": verify_agent_hung,
    "CODE_FAILURE": verify_code_failure,
    "CONFIG_MISMATCH": verify_config_mismatch,
    "POLICY_VIOLATION": verify_policy_violation,
    "THREAT_DETECTED": verify_threat_detected,
}

_verification_lock = threading.Lock()


# ── DB Setup ─────────────────────────────────────────────────────────────────

def _ensure_chronicle() -> None:
    """Create verification tables in Chronicle if they don't exist (idempotent)."""
    Path(CHRONICLE_DB).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(CHRONICLE_DB, timeout=5, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS verification_queue (
            id INTEGER PRIMARY KEY,
            alert_id TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            context_json TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            timeout_at TEXT NOT NULL
        );
        
        CREATE TABLE IF NOT EXISTS verification_log (
            id INTEGER PRIMARY KEY,
            alert_id TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            attempt_number INTEGER NOT NULL DEFAULT 1,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            result TEXT NOT NULL,
            evidence_json TEXT,
            root_cause TEXT,
            created_at TEXT NOT NULL
        );
        
        CREATE INDEX IF NOT EXISTS idx_verification_queue_alert_id
            ON verification_queue(alert_id);
        CREATE INDEX IF NOT EXISTS idx_verification_queue_status
            ON verification_queue(status);
        CREATE INDEX IF NOT EXISTS idx_verification_queue_timeout_at
            ON verification_queue(timeout_at);
        CREATE INDEX IF NOT EXISTS idx_verification_log_alert_id
            ON verification_log(alert_id);
        CREATE INDEX IF NOT EXISTS idx_verification_log_result
            ON verification_log(result);
    """)
    conn.commit()
    conn.close()


def _connect_chronicle() -> sqlite3.Connection:
    """Get a connection to Chronicle."""
    conn = sqlite3.connect(CHRONICLE_DB, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _now_utc() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


# ── Internal Helpers ─────────────────────────────────────────────────────────

def _queue_verification(
    alert_id: str,
    alert_type: str,
    context: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Add a verification task to the queue.

    Args:
        alert_id: The alert ID to verify
        alert_type: Type of alert (ORPHAN_POSITION, AGENT_HUNG, etc.)
        context: Optional extra data for the verification checker

    Returns:
        True if queued, False on error
    """
    _ensure_chronicle()

    timeout_mins = VERIFICATION_TIMEOUTS.get(alert_type, 5)
    timeout_at = (
        datetime.now(timezone.utc) + timedelta(minutes=timeout_mins)
    ).isoformat()

    context_json = json.dumps(context or {})

    try:
        conn = _connect_chronicle()
        conn.execute(
            """
            INSERT INTO verification_queue
                (alert_id, alert_type, status, context_json, created_at, timeout_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (alert_id, alert_type, "PENDING", context_json, _now_utc(), timeout_at),
        )
        conn.commit()
        conn.close()
        logger.info(
            "verifier: Queued verification — alert_id=%s alert_type=%s timeout=%d min",
            alert_id, alert_type, timeout_mins,
        )
        return True
    except Exception as e:
        logger.error("verifier: Failed to queue verification: %s", e)
        return False


def _log_verification(
    alert_id: str,
    alert_type: str,
    result: VerificationResult,
    evidence: Optional[Dict] = None,
    root_cause: Optional[str] = None,
) -> bool:
    """
    Log a verification attempt to Chronicle.

    Args:
        alert_id: The alert ID
        alert_type: Type of alert
        result: VERIFIED, FAILED_VERIFICATION, MANUAL_REQUIRED, or TIMEOUT
        evidence: Dict of evidence collected during verification
        root_cause: If FAILED, why it failed

    Returns:
        True if logged, False on error
    """
    _ensure_chronicle()

    evidence_json = json.dumps(evidence or {})

    try:
        conn = _connect_chronicle()
        conn.execute(
            """
            INSERT INTO verification_log
                (alert_id, alert_type, started_at, completed_at, result, evidence_json, root_cause, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert_id,
                alert_type,
                _now_utc(),
                _now_utc(),
                result.value,
                evidence_json,
                root_cause,
                _now_utc(),
            ),
        )
        conn.commit()
        conn.close()
        logger.info(
            "verifier: Logged verification — alert_id=%s result=%s",
            alert_id, result.value,
        )
        return True
    except Exception as e:
        logger.error("verifier: Failed to log verification: %s", e)
        return False


def _update_queue_status(
    alert_id: str,
    status: VerificationResult,
    result_detail: Optional[str] = None,
) -> bool:
    """
    Update the status of a queued verification.

    Args:
        alert_id: The alert ID
        status: New status (VERIFIED, FAILED_VERIFICATION, etc.)
        result_detail: Optional additional detail

    Returns:
        True if updated, False on error
    """
    _ensure_chronicle()

    try:
        conn = _connect_chronicle()
        conn.execute(
            """
            UPDATE verification_queue
            SET status = ?, completed_at = ?, result = ?
            WHERE alert_id = ?
            """,
            (status.value, _now_utc(), result_detail, alert_id),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error("verifier: Failed to update queue status: %s", e)
        return False


def _get_pending_verifications() -> list:
    """
    Get all pending verifications from queue.

    Returns:
        List of dicts with keys: alert_id, alert_type, context_json, timeout_at
    """
    _ensure_chronicle()

    try:
        conn = _connect_chronicle()
        rows = conn.execute(
            """
            SELECT alert_id, alert_type, context_json, timeout_at
            FROM verification_queue
            WHERE status = 'PENDING'
            ORDER BY created_at ASC
            """
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("verifier: Failed to get pending verifications: %s", e)
        return []


def _check_timeout(timeout_at_str: str) -> bool:
    """
    Check if a timeout has been reached.

    Args:
        timeout_at_str: ISO string of timeout deadline

    Returns:
        True if timeout has been exceeded, False otherwise
    """
    try:
        timeout_at = datetime.fromisoformat(timeout_at_str)
        now = datetime.now(timezone.utc)
        return now > timeout_at
    except Exception:
        return False


# ── Public API ───────────────────────────────────────────────────────────────

def verify_alert_async(
    alert_id: str,
    alert_type: str,
    context: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Queue an alert for automatic verification after ACK.

    This should be called immediately after Ahmed ACKs an alert.
    The verification will run asynchronously in the next check cycle.

    Args:
        alert_id: The alert ID to verify
        alert_type: Type of alert (must be in VERIFICATION_TIMEOUTS)
        context: Optional dict with type-specific data:
                 - ORPHAN_POSITION: {"position_id": "aapl-long-1000"}
                 - AGENT_HUNG: {"agent_name": "alpha-execution"}
                 - CODE_FAILURE: {"service": "nexus-alpha", "commit_sha": "abc123"}
                 - CONFIG_MISMATCH: {"param": "MAX_POSITIONS"}
                 - POLICY_VIOLATION: {"agent_name": "alpha", "violation_type": "..."}
                 - THREAT_DETECTED: {"agent_name": "alpha", "threat_id": "..."}

    Returns:
        True if queued, False if alert_type is unknown or queue failed
    """
    if alert_type not in VERIFICATION_CHECKERS:
        logger.warning(
            "verifier: Unknown alert_type=%s — cannot verify",
            alert_type,
        )
        return False

    return _queue_verification(alert_id, alert_type, context)


def check_pending_verifications() -> Dict[str, int]:
    """
    Run verification checks for all pending alerts in queue.
    Should be called periodically (every 5-10 seconds).

    Returns:
        Dict with keys: verified, failed, manual, timed_out
    """
    with _verification_lock:
        pending = _get_pending_verifications()

        results = {
            "verified": 0,
            "failed": 0,
            "manual": 0,
            "timed_out": 0,
        }

        for item in pending:
            alert_id = item["alert_id"]
            alert_type = item["alert_type"]
            context = json.loads(item["context_json"] or "{}")
            timeout_at = item["timeout_at"]

            # Check if this verification has timed out
            if _check_timeout(timeout_at):
                logger.warning(
                    "verifier: Verification TIMEOUT — alert_id=%s alert_type=%s",
                    alert_id, alert_type,
                )
                _update_queue_status(alert_id, VerificationResult.TIMEOUT)
                _log_verification(
                    alert_id,
                    alert_type,
                    VerificationResult.TIMEOUT,
                    evidence={"reason": "verification_timeout"},
                    root_cause=f"Verification did not complete within {VERIFICATION_TIMEOUTS.get(alert_type, 5)} minutes",
                )
                results["timed_out"] += 1
                continue

            # Run the appropriate checker
            checker_fn = VERIFICATION_CHECKERS[alert_type]
            try:
                result, evidence, root_cause = checker_fn(context)

                _update_queue_status(alert_id, result)
                _log_verification(
                    alert_id,
                    alert_type,
                    result,
                    evidence=evidence,
                    root_cause=root_cause,
                )

                if result == VerificationResult.VERIFIED:
                    results["verified"] += 1
                    logger.info(
                        "verifier: VERIFIED — alert_id=%s alert_type=%s",
                        alert_id, alert_type,
                    )
                elif result == VerificationResult.FAILED_VERIFICATION:
                    results["failed"] += 1
                    logger.warning(
                        "verifier: FAILED_VERIFICATION — alert_id=%s alert_type=%s reason=%s",
                        alert_id, alert_type, root_cause,
                    )
                elif result == VerificationResult.MANUAL_REQUIRED:
                    results["manual"] += 1
                    logger.info(
                        "verifier: MANUAL_REQUIRED — alert_id=%s alert_type=%s",
                        alert_id, alert_type,
                    )

            except Exception as e:
                logger.error(
                    "verifier: Exception during verification — alert_id=%s alert_type=%s error=%s",
                    alert_id, alert_type, e,
                    exc_info=True,
                )
                # Mark as requiring manual review on exception
                _update_queue_status(alert_id, VerificationResult.MANUAL_REQUIRED)
                _log_verification(
                    alert_id,
                    alert_type,
                    VerificationResult.MANUAL_REQUIRED,
                    root_cause=f"Verification exception: {str(e)}",
                )
                results["manual"] += 1

        return results


def get_verification_status(alert_id: str) -> Optional[Dict]:
    """
    Get the verification status for a specific alert.

    Args:
        alert_id: The alert ID to check

    Returns:
        Dict with keys: status, result, completed_at, evidence
        or None if alert not found
    """
    _ensure_chronicle()

    try:
        conn = _connect_chronicle()
        row = conn.execute(
            """
            SELECT status, result, completed_at FROM verification_queue
            WHERE alert_id = ?
            """,
            (alert_id,),
        ).fetchone()

        if not row:
            return None

        result = {
            "status": row["status"],
            "result": row["result"],
            "completed_at": row["completed_at"],
        }

        # Try to get most recent log entry
        log_row = conn.execute(
            """
            SELECT evidence_json FROM verification_log
            WHERE alert_id = ?
            ORDER BY completed_at DESC
            LIMIT 1
            """,
            (alert_id,),
        ).fetchone()

        if log_row and log_row["evidence_json"]:
            result["evidence"] = json.loads(log_row["evidence_json"])

        conn.close()
        return result

    except Exception as e:
        logger.error("verifier: Failed to get verification status: %s", e)
        return None


def get_verification_summary() -> Dict:
    """
    Get summary of all verifications for monitoring.

    Returns:
        Dict with keys: total_queued, verified, failed, manual, timed_out
    """
    _ensure_chronicle()

    try:
        conn = _connect_chronicle()

        total = conn.execute(
            "SELECT COUNT(*) as count FROM verification_queue"
        ).fetchone()

        verified = conn.execute(
            "SELECT COUNT(*) as count FROM verification_queue WHERE result = ?",
            (VerificationResult.VERIFIED.value,),
        ).fetchone()

        failed = conn.execute(
            "SELECT COUNT(*) as count FROM verification_queue WHERE result = ?",
            (VerificationResult.FAILED_VERIFICATION.value,),
        ).fetchone()

        manual = conn.execute(
            "SELECT COUNT(*) as count FROM verification_queue WHERE result = ?",
            (VerificationResult.MANUAL_REQUIRED.value,),
        ).fetchone()

        timed_out = conn.execute(
            "SELECT COUNT(*) as count FROM verification_queue WHERE result = ?",
            (VerificationResult.TIMEOUT.value,),
        ).fetchone()

        conn.close()

        return {
            "total_queued": total["count"],
            "verified": verified["count"],
            "failed": failed["count"],
            "manual": manual["count"],
            "timed_out": timed_out["count"],
        }

    except Exception as e:
        logger.error("verifier: Failed to get verification summary: %s", e)
        return {
            "total_queued": 0,
            "verified": 0,
            "failed": 0,
            "manual": 0,
            "timed_out": 0,
        }


def cleanup_old_verifications(older_than_hours: int = 24) -> int:
    """
    Remove old verification records for maintenance.

    Args:
        older_than_hours: Delete records older than this (default 24h)

    Returns:
        Number of records deleted
    """
    _ensure_chronicle()

    with _verification_lock:
        try:
            conn = _connect_chronicle()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
            ).isoformat()

            cur = conn.execute(
                "DELETE FROM verification_queue WHERE created_at < ?",
                (cutoff,),
            )
            deleted_queue = cur.rowcount

            cur = conn.execute(
                "DELETE FROM verification_log WHERE created_at < ?",
                (cutoff,),
            )
            deleted_log = cur.rowcount

            conn.commit()
            conn.close()

            total = deleted_queue + deleted_log
            logger.info(
                "verifier: Cleaned %d old verification records (queue=%d log=%d)",
                total, deleted_queue, deleted_log,
            )
            return total

        except Exception as e:
            logger.error("verifier: Cleanup failed: %s", e)
            return 0
