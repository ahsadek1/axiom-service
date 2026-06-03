"""
MANDATE COMPLIANCE LOGGER
Shared utility for all Nexus agents to log intervention incidents.
Implements: Ahmed's Immediate Intervention Mandate (Apr 28, 2026)

Usage:
    from shared.mandate_logger import log_incident, update_incident, resolve_incident

Every agent must call these when:
  - An error/failure is detected        → log_incident()
  - Intervention begins                  → update_incident(intervention_at=...)
  - Fix is confirmed                     → resolve_incident()
"""

import sqlite3
import datetime
import uuid
import os

CHRONICLE_DB = "/Users/ahmedsadek/nexus/data/chronicle.db"

# Failure type constants
FAILURE_EXECUTION_ERROR   = "execution_error"
FAILURE_SERVICE_DOWN      = "service_down"
FAILURE_PIPELINE_BLOCKED  = "pipeline_blocked"
FAILURE_DATA_SOURCE       = "data_source"
FAILURE_GHOST_POSITION    = "ghost_position"
FAILURE_HEALTH_PARSE      = "health_parse"
FAILURE_CREDENTIAL        = "credential"
FAILURE_DB_INCONSISTENCY  = "db_inconsistency"
FAILURE_DEFAULT           = "default"

# Fix type constants
FIX_ROOT_CAUSE   = "ROOT_CAUSE"
FIX_SYMPTOMATIC  = "SYMPTOMATIC"
FIX_PARTIAL      = "PARTIAL"
FIX_UNKNOWN      = "UNKNOWN"

# Outcome constants
OUTCOME_RESOLVED   = "RESOLVED"
OUTCOME_PARTIAL    = "PARTIAL"
OUTCOME_ESCALATED  = "ESCALATED"
OUTCOME_UNRESOLVED = "UNRESOLVED"
OUTCOME_OPEN       = "OPEN"

# Ideal response times (minutes) by failure type
IDEAL_RESPONSE_MIN = {
    FAILURE_EXECUTION_ERROR:   2,
    FAILURE_SERVICE_DOWN:      1,
    FAILURE_PIPELINE_BLOCKED:  5,
    FAILURE_DATA_SOURCE:       3,
    FAILURE_GHOST_POSITION:    5,
    FAILURE_HEALTH_PARSE:     10,
    FAILURE_CREDENTIAL:        5,
    FAILURE_DB_INCONSISTENCY: 10,
    FAILURE_DEFAULT:           5,
}


def _ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mandate_compliance (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id      TEXT UNIQUE NOT NULL,
            agent            TEXT NOT NULL,
            component        TEXT NOT NULL,
            failure_type     TEXT NOT NULL,
            detected_at      TEXT NOT NULL,
            intervention_at  TEXT,
            resolved_at      TEXT,
            ideal_response_min INTEGER,
            actual_response_min INTEGER,
            response_status  TEXT,
            fix_type         TEXT,
            outcome          TEXT DEFAULT 'OPEN',
            damage           TEXT,
            root_cause       TEXT,
            fix_applied      TEXT,
            recurring_risk   TEXT,
            reported_by      TEXT,
            created_at       TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def _get_conn():
    conn = sqlite3.connect(CHRONICLE_DB)
    _ensure_schema(conn)
    return conn


def _now_utc():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_lag_minutes(start_iso, end_iso):
    """Compute minutes between two ISO timestamps."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        start = datetime.datetime.strptime(start_iso[:19].replace(" ", "T") + "Z", fmt)
        end   = datetime.datetime.strptime(end_iso[:19].replace(" ", "T") + "Z", fmt)
        delta = (end - start).total_seconds() / 60
        return max(0, round(delta))
    except Exception:
        return None


def log_incident(
    agent: str,
    component: str,
    failure_type: str,
    root_cause: str = None,
    damage: str = None,
    reported_by: str = None,
) -> str:
    """
    Log a new failure incident. Call IMMEDIATELY when failure is detected.
    Returns the incident_id for subsequent updates.

    Args:
        agent:        Which agent detected this (e.g. "omni", "cipher")
        component:    What failed (e.g. "alpha-execution", "axiom", "prime-buffer")
        failure_type: One of FAILURE_* constants (or custom string)
        root_cause:   Root cause description if already known
        damage:       Damage assessment (e.g. "$200 PnL lost", "3 orders missed")
        reported_by:  Override reporter name

    Returns:
        incident_id (str) — use this to update/resolve the incident
    """
    incident_id = f"INC-{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{str(uuid.uuid4())[:6].upper()}"
    detected_at = _now_utc()
    ideal_min   = IDEAL_RESPONSE_MIN.get(failure_type, IDEAL_RESPONSE_MIN[FAILURE_DEFAULT])

    conn = _get_conn()
    try:
        conn.execute("""
            INSERT INTO mandate_compliance
              (incident_id, agent, component, failure_type, detected_at,
               ideal_response_min, outcome, root_cause, damage, reported_by)
            VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)
        """, (incident_id, agent, component, failure_type, detected_at,
              ideal_min, root_cause, damage, reported_by or agent))
        conn.commit()
        print(f"[mandate_logger] Incident logged: {incident_id} | {agent} | {component} | {failure_type}")
    except Exception as e:
        print(f"[mandate_logger] ERROR logging incident: {e}")
    finally:
        conn.close()

    return incident_id


def update_incident(
    incident_id: str,
    intervention_at: str = None,
    root_cause: str = None,
    fix_applied: str = None,
    damage: str = None,
):
    """
    Update an incident when intervention begins or mid-fix.
    Call as soon as you start working on the problem.

    Args:
        incident_id:     From log_incident()
        intervention_at: ISO timestamp when you started fixing (defaults to now)
        root_cause:      Root cause if now known
        fix_applied:     Description of fix being applied
        damage:          Updated damage assessment
    """
    conn = _get_conn()
    try:
        if not intervention_at:
            intervention_at = _now_utc()

        # Compute actual response lag
        row = conn.execute(
            "SELECT detected_at, ideal_response_min FROM mandate_compliance WHERE incident_id=?",
            (incident_id,)
        ).fetchone()

        actual_lag = None
        response_status = None
        if row:
            detected_at, ideal_min = row
            actual_lag = _compute_lag_minutes(detected_at, intervention_at)
            if actual_lag is not None and ideal_min is not None:
                response_status = "ON_TIME" if actual_lag <= ideal_min else f"DELAYED_{actual_lag - ideal_min}m"

        conn.execute("""
            UPDATE mandate_compliance
            SET intervention_at=?, root_cause=COALESCE(?, root_cause),
                fix_applied=COALESCE(?, fix_applied), damage=COALESCE(?, damage),
                actual_response_min=?, response_status=?
            WHERE incident_id=?
        """, (intervention_at, root_cause, fix_applied, damage,
              actual_lag, response_status, incident_id))
        conn.commit()
        print(f"[mandate_logger] Incident updated: {incident_id} | lag={actual_lag}m | status={response_status}")
    except Exception as e:
        print(f"[mandate_logger] ERROR updating incident: {e}")
    finally:
        conn.close()


def resolve_incident(
    incident_id: str,
    outcome: str,
    fix_type: str,
    fix_applied: str = None,
    root_cause: str = None,
    damage: str = None,
    recurring_risk: str = None,
):
    """
    Mark an incident resolved. Call when fix is confirmed.

    Args:
        incident_id:    From log_incident()
        outcome:        OUTCOME_RESOLVED / OUTCOME_PARTIAL / OUTCOME_ESCALATED / OUTCOME_UNRESOLVED
        fix_type:       FIX_ROOT_CAUSE / FIX_SYMPTOMATIC / FIX_PARTIAL / FIX_UNKNOWN
        fix_applied:    What was done to fix it
        root_cause:     Root cause (if not already set)
        damage:         Final damage assessment
        recurring_risk: "Yes — [description]" or "No"
    """
    conn = _get_conn()
    try:
        resolved_at = _now_utc()

        # If intervention_at not yet set, set it now (late intervention = all lag to resolution)
        row = conn.execute(
            "SELECT intervention_at FROM mandate_compliance WHERE incident_id=?",
            (incident_id,)
        ).fetchone()
        if row and not row[0]:
            update_incident(incident_id, intervention_at=resolved_at)

        conn.execute("""
            UPDATE mandate_compliance
            SET resolved_at=?, outcome=?, fix_type=?,
                fix_applied=COALESCE(?, fix_applied),
                root_cause=COALESCE(?, root_cause),
                damage=COALESCE(?, damage),
                recurring_risk=COALESCE(?, recurring_risk)
            WHERE incident_id=?
        """, (resolved_at, outcome, fix_type, fix_applied, root_cause,
              damage, recurring_risk, incident_id))
        conn.commit()
        print(f"[mandate_logger] Incident resolved: {incident_id} | {outcome} | {fix_type}")
    except Exception as e:
        print(f"[mandate_logger] ERROR resolving incident: {e}")
    finally:
        conn.close()


def get_open_incidents(agent: str = None):
    """Get all open incidents, optionally filtered by agent."""
    conn = _get_conn()
    try:
        if agent:
            rows = conn.execute("""
                SELECT incident_id, agent, component, failure_type, detected_at, damage
                FROM mandate_compliance
                WHERE outcome IN ('OPEN', 'PARTIAL') AND agent=?
                ORDER BY detected_at DESC
            """, (agent,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT incident_id, agent, component, failure_type, detected_at, damage
                FROM mandate_compliance
                WHERE outcome IN ('OPEN', 'PARTIAL')
                ORDER BY detected_at DESC
            """).fetchall()
        return rows
    finally:
        conn.close()


if __name__ == "__main__":
    # Test
    print("Testing mandate_logger...")
    inc_id = log_incident(
        agent="omni",
        component="test-component",
        failure_type=FAILURE_DEFAULT,
        root_cause="Test incident — mandate_logger integration test",
        damage="None (test)"
    )
    update_incident(inc_id, fix_applied="Test fix applied")
    resolve_incident(
        inc_id,
        outcome=OUTCOME_RESOLVED,
        fix_type=FIX_ROOT_CAUSE,
        fix_applied="Test resolution confirmed",
        recurring_risk="No"
    )
    print(f"Test incident {inc_id} logged + resolved. CHRONICLE schema ready.")
