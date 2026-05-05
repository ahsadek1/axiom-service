"""
state.py — NSP persistent state management.

All intervention state and calibration mode are written to SQLite BEFORE
every autonomous action.  This allows NSP to recover mid-intervention on
restart by reading the intervention_log table.

State categories stored in nsp_state (key-value):
  calibration_mode        — bool ("true"/"false") — survives restarts
  calibration_clean_days  — int — days of clean trading observed
  freeze_mode             — bool — circuit breaker FREEZE flag
  freeze_reason           — str — why FREEZE was triggered

Intervention state stored in intervention_log:
  One row per attempt, written BEFORE the action is taken.
  outcome is updated AFTER to "success" or "failed:<reason>".
"""

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from db import enqueue_write_sync, read_db

logger = logging.getLogger("nsp.state")

# ---------------------------------------------------------------------------
# nsp_state key constants
# ---------------------------------------------------------------------------

KEY_CALIBRATION_MODE: str = "calibration_mode"
KEY_CALIBRATION_CLEAN_DAYS: str = "calibration_clean_days"
KEY_CALIBRATION_START_TS: str = "calibration_start_ts"
KEY_FREEZE_MODE: str = "freeze_mode"
KEY_FREEZE_REASON: str = "freeze_reason"
KEY_FREEZE_LIFTED_BY: str = "freeze_lifted_by"


# ---------------------------------------------------------------------------
# Schema assertion (called at startup to ensure table exists)
# ---------------------------------------------------------------------------

def ensure_state_table() -> None:
    """Verify the nsp_state table is reachable.

    Reads one row to confirm the DB writer has initialised the schema.
    Logs a warning if the table is empty (first run) — this is normal.

    Raises:
        Exception: If the table cannot be read (DB not initialised).
    """
    try:
        rows = read_db("SELECT COUNT(*) FROM nsp_state")
        count = rows[0][0] if rows else 0
        logger.info("nsp_state table reachable — %d key(s) persisted", count)
    except Exception as exc:
        logger.error("nsp_state table check failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Generic key-value accessors
# ---------------------------------------------------------------------------

def get_state_raw(key: str) -> Optional[str]:
    """Read a raw string value from nsp_state.

    Args:
        key: State key to look up.

    Returns:
        Raw string value, or None if not set.
    """
    try:
        rows = read_db("SELECT value FROM nsp_state WHERE key = ?", (key,))
        return rows[0][0] if rows else None
    except Exception as exc:
        logger.error("get_state_raw(%s) failed: %s", key, exc)
        return None


def set_state_raw(key: str, value: str) -> None:
    """Write a raw string value to nsp_state (synchronous — survives restart).

    Args:
        key: State key.
        value: Raw string value.
    """
    try:
        enqueue_write_sync(
            "INSERT OR REPLACE INTO nsp_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, time.time()),
        )
    except Exception as exc:
        logger.error("set_state_raw(%s) failed: %s", key, exc)
        raise


def get_state_json(key: str) -> Optional[Any]:
    """Read and JSON-decode a value from nsp_state.

    Args:
        key: State key.

    Returns:
        Decoded Python object, or None if not set or decode fails.
    """
    raw = get_state_raw(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("get_state_json(%s) JSON decode failed: %s", key, exc)
        return None


def set_state_json(key: str, value: Any) -> None:
    """JSON-encode and write a value to nsp_state.

    Args:
        key: State key.
        value: Python object (must be JSON-serialisable).
    """
    set_state_raw(key, json.dumps(value))


# ---------------------------------------------------------------------------
# Calibration mode
# ---------------------------------------------------------------------------

def get_calibration_mode() -> bool:
    """Return whether NSP is in calibration mode (observe-only, no interventions).

    Defaults to True on first run. Persisted across restarts.

    Returns:
        True if in calibration mode, False if predictive engine is active.
    """
    raw = get_state_raw(KEY_CALIBRATION_MODE)
    if raw is None:
        # First run — default to calibration ON and persist
        _init_calibration()
        return True
    return raw == "true"


def set_calibration_mode(enabled: bool) -> None:
    """Persist calibration mode to SQLite.

    Args:
        enabled: True to enter calibration mode, False to activate predictive engine.
    """
    set_state_raw(KEY_CALIBRATION_MODE, "true" if enabled else "false")
    logger.info("Calibration mode set to: %s", enabled)


def _init_calibration() -> None:
    """Initialise calibration state on first run."""
    try:
        enqueue_write_sync(
            "INSERT OR IGNORE INTO nsp_state (key, value, updated_at) VALUES (?, ?, ?)",
            (KEY_CALIBRATION_MODE, "true", time.time()),
        )
        enqueue_write_sync(
            "INSERT OR IGNORE INTO nsp_state (key, value, updated_at) VALUES (?, ?, ?)",
            (KEY_CALIBRATION_CLEAN_DAYS, "0", time.time()),
        )
        enqueue_write_sync(
            "INSERT OR IGNORE INTO nsp_state (key, value, updated_at) VALUES (?, ?, ?)",
            (KEY_CALIBRATION_START_TS, str(time.time()), time.time()),
        )
        logger.info("Calibration state initialised (first run)")
    except Exception as exc:
        logger.error("Failed to initialise calibration state: %s", exc)
        raise


def get_calibration_clean_days() -> int:
    """Return the number of clean trading days observed during calibration.

    Returns:
        Integer day count (0 on first run).
    """
    raw = get_state_raw(KEY_CALIBRATION_CLEAN_DAYS)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def increment_calibration_clean_day() -> int:
    """Increment the calibration clean-day counter by 1 and persist it.

    Returns:
        New day count after increment.
    """
    current = get_calibration_clean_days()
    new_count = current + 1
    set_state_raw(KEY_CALIBRATION_CLEAN_DAYS, str(new_count))
    logger.info("Calibration clean day %d recorded", new_count)
    return new_count


# ---------------------------------------------------------------------------
# Freeze mode (circuit breaker FREEZE)
# ---------------------------------------------------------------------------

def get_freeze_mode() -> bool:
    """Return whether NSP is in FREEZE mode (all autonomous actions suspended).

    Returns:
        True if FROZEN, False if normal operation.
    """
    raw = get_state_raw(KEY_FREEZE_MODE)
    return raw == "true" if raw is not None else False


def set_freeze_mode(enabled: bool, reason: str = "", lifted_by: str = "") -> None:
    """Set FREEZE mode state.

    FREEZE is set when circuit breaker trips (3 restarts/service/hr or
    10 total/hr). Lifted only by SOVEREIGN or Ahmed via API.

    Args:
        enabled: True to FREEZE, False to lift.
        reason: Why FREEZE was triggered (required when enabling).
        lifted_by: Who lifted the freeze (required when disabling).
    """
    set_state_raw(KEY_FREEZE_MODE, "true" if enabled else "false")
    if enabled and reason:
        set_state_raw(KEY_FREEZE_REASON, reason)
    if not enabled and lifted_by:
        set_state_raw(KEY_FREEZE_LIFTED_BY, lifted_by)
    action = "ACTIVATED" if enabled else "LIFTED"
    logger.warning("FREEZE mode %s — reason=%s lifted_by=%s", action, reason, lifted_by)


# ---------------------------------------------------------------------------
# Intervention state — written BEFORE every action
# ---------------------------------------------------------------------------

def write_intervention_state(
    service: str,
    failure_class: str,
    attempt_number: int,
    action: str,
    state_snapshot: Dict[str, Any],
    incident_id: Optional[str] = None,
    outcome: Optional[str] = None,
) -> str:
    """Persist intervention state to SQLite BEFORE taking the action.

    This is the core durability guarantee: if NSP is killed mid-intervention,
    the next start reads intervention_log and resumes correctly.

    Args:
        service: Target service name.
        failure_class: Root cause class (DEAD, UNRESPONSIVE, DEGRADED, etc.).
        attempt_number: Which attempt number (1, 2, or 3).
        action: Human-readable description of the action about to be taken.
        state_snapshot: Dict snapshot of system state at intervention time.
        incident_id: Existing incident UUID, or None to generate a new one.
        outcome: Optional outcome string (set AFTER action completes).

    Returns:
        incident_id (newly generated if None was passed).

    Raises:
        Exception: If the synchronous write fails — intervention MUST NOT proceed.
    """
    if incident_id is None:
        incident_id = str(uuid.uuid4())

    now = time.time()
    expires_at = now + 4 * 3600.0

    enqueue_write_sync(
        """INSERT INTO intervention_log
           (incident_id, service, failure_class, attempt_number, action,
            outcome, state_snapshot, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            incident_id,
            service,
            failure_class,
            attempt_number,
            action,
            outcome,
            json.dumps(state_snapshot),
            now,
            expires_at,
        ),
    )
    logger.info(
        "Intervention state persisted: incident=%s service=%s class=%s attempt=%d action=%s",
        incident_id,
        service,
        failure_class,
        attempt_number,
        action,
    )
    return incident_id


def update_intervention_outcome(incident_id: str, outcome: str) -> None:
    """Update the outcome of an existing intervention record.

    Called AFTER the action completes (success or failure).

    Args:
        incident_id: The incident UUID returned by write_intervention_state.
        outcome: Outcome string ('success', 'failed:<reason>', 'rolled_back').
    """
    try:
        enqueue_write_sync(
            "UPDATE intervention_log SET outcome = ? WHERE incident_id = ? AND outcome IS NULL",
            (outcome, incident_id),
        )
        logger.info("Intervention outcome updated: incident=%s outcome=%s", incident_id, outcome)
    except Exception as exc:
        logger.error(
            "Failed to update outcome for incident %s: %s", incident_id, exc
        )
        raise


def get_recent_interventions(
    service: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return recent non-expired intervention log entries.

    Args:
        service: Filter to a specific service, or None for all.
        limit: Maximum number of rows to return.

    Returns:
        List of dicts with intervention details.
    """
    now = time.time()
    if service:
        rows = read_db(
            "SELECT * FROM intervention_log "
            "WHERE service = ? AND expires_at > ? "
            "ORDER BY created_at DESC LIMIT ?",
            (service, now, limit),
        )
    else:
        rows = read_db(
            "SELECT * FROM intervention_log "
            "WHERE expires_at > ? "
            "ORDER BY created_at DESC LIMIT ?",
            (now, limit),
        )
    return [dict(r) for r in rows]


def get_intervention_count_last_hour(service: str) -> int:
    """Return number of interventions for a service in the last 60 minutes.

    Used by the circuit breaker: >= 3 → FREEZE that service.

    Args:
        service: Service name.

    Returns:
        Integer count.
    """
    since = time.time() - 3600.0
    rows = read_db(
        "SELECT COUNT(*) FROM intervention_log "
        "WHERE service = ? AND created_at > ? AND expires_at > ?",
        (service, since, time.time()),
    )
    return rows[0][0] if rows else 0


def get_total_intervention_count_last_hour() -> int:
    """Return total interventions across all services in the last 60 minutes.

    Used by the circuit breaker: >= 10 → FREEZE all.

    Returns:
        Integer count.
    """
    since = time.time() - 3600.0
    rows = read_db(
        "SELECT COUNT(*) FROM intervention_log "
        "WHERE created_at > ? AND expires_at > ?",
        (since, time.time()),
    )
    return rows[0][0] if rows else 0
