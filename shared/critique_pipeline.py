"""
critique_pipeline.py — Stream B Critique Pipeline Orchestrator

SOVEREIGN uses this module to:
  - Enter services into the critique pipeline
  - Advance pipeline state as agents complete reviews
  - Query accumulation for GENESIS engagement decisions
  - Record GENESIS engagement, implementation, verification, and deployment

GENESIS uses this module to:
  - Fetch all completed reviews for holistic engagement
  - Mark services as GENESIS_ENGAGED → IMPLEMENTATION_IN_PROGRESS

QA Council agents use this module to:
  - Record individual review completion
  - Mark specific findings as VERIFIED or REJECTED

Pipeline states (in order):
  REVIEW_REQUESTED → REVIEW_IN_PROGRESS → READY_FOR_GENESIS →
  GENESIS_ENGAGED → IMPLEMENTATION_IN_PROGRESS → VERIFICATION_IN_PROGRESS →
  AWAITING_CIPHER → AWAITING_AHMED → STAGING_SOAK → DEPLOYED
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("nexus.critique_pipeline")

CHRONICLE_DB = "/Users/ahmedsadek/nexus/data/chronicle.db"

# All ten QA Council agents (v2 — two independent agents per lens)
# A service is READY_FOR_GENESIS only when all ten have submitted findings.
ALL_AGENTS = {
    "PROBE-1", "PROBE-2",       # Lens 1: Security & Logic
    "EDGE-1",  "EDGE-2",         # Lens 2: Edge Cases & Boundaries
    "CONTRACT-1", "CONTRACT-2", # Lens 3: Integration & API Contracts
    "DEPLOY-1",  "DEPLOY-2",    # Lens 4: Operational Reality
    "ARCHITECT-1", "ARCHITECT-2", # Lens 5: Architectural Coherence
}

# Service catalogue — grouping, criticality (audit priority), host, port
# Groupings per Initial Hardening Scope spec (Ahmed Sadek, 2026-04-24):
#   EXECUTION:      services that directly handle trade execution and capital deployment
#   INTELLIGENCE:   services that produce analysis, synthesis, strategic context
#   INFRASTRUCTURE: governance, storage, communication, operational backbone
# Criticality = audit sequence number (1=first reviewed, lower=sooner)
SERVICE_CATALOGUE: dict[str, dict] = {
    # GROUP 1 — EXECUTION
    "alpha-execution":   {"grouping": "EXECUTION",      "criticality": 1,  "port": 8005, "host": "192.168.1.141"},
    "prime-execution":   {"grouping": "EXECUTION",      "criticality": 2,  "port": 8006, "host": "192.168.1.141"},
    "capital-router":    {"grouping": "EXECUTION",      "criticality": 4,  "port": 8000, "host": "192.168.1.141"},
    "atm-multiweek":     {"grouping": "EXECUTION",      "criticality": 14, "port": 9004, "host": "192.168.1.141"},
    "atm-0dte":          {"grouping": "EXECUTION",      "criticality": 15, "port": 9005, "host": "192.168.1.141"},
    "atg-swing":         {"grouping": "EXECUTION",      "criticality": 16, "port": 9006, "host": "192.168.1.141"},
    "atg-intraday":      {"grouping": "EXECUTION",      "criticality": 17, "port": 9007, "host": "192.168.1.141"},
    "alpha-buffer":      {"grouping": "EXECUTION",      "criticality": 18, "port": 8002, "host": "192.168.1.141"},
    "prime-buffer":      {"grouping": "EXECUTION",      "criticality": 19, "port": 8003, "host": "192.168.1.141"},

    # GROUP 2 — INTELLIGENCE
    "omni":              {"grouping": "INTELLIGENCE",   "criticality": 3,  "port": 8004, "host": "192.168.1.141"},
    "ails":              {"grouping": "INTELLIGENCE",   "criticality": 5,  "port": 8008, "host": "192.168.1.141"},
    "axiom":             {"grouping": "INTELLIGENCE",   "criticality": 6,  "port": 8001, "host": "192.168.1.141"},
    "thesis":            {"grouping": "INTELLIGENCE",   "criticality": 10, "port": 8060, "host": "192.168.1.141"},
    "oracle":            {"grouping": "INTELLIGENCE",   "criticality": 12, "port": 8007, "host": "192.168.1.141"},
    "pipeline-sentinel": {"grouping": "INTELLIGENCE",   "criticality": 13, "port": 8010, "host": "192.168.1.141"},

    # GROUP 3 — INFRASTRUCTURE
    "sovereign":         {"grouping": "INFRASTRUCTURE", "criticality": 7,  "port": None, "host": "192.168.1.42"},
    "chronicle":         {"grouping": "INFRASTRUCTURE", "criticality": 8,  "port": 8020, "host": "192.168.1.42"},
    "vector":            {"grouping": "INFRASTRUCTURE", "criticality": 9,  "port": 8030, "host": "192.168.1.42"},
    "sovereign-comms":   {"grouping": "INFRASTRUCTURE", "criticality": 11, "port": 9999, "host": "192.168.1.141"},
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CHRONICLE_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── SOVEREIGN: Enter service into pipeline ────────────────────────────────────

def enter_service(service_name: str, notes: str = "") -> str:
    """
    Enter a service into Stream B critique pipeline.

    Creates a review_cycle_id, registers in pipeline_services with
    state=REVIEW_REQUESTED, and returns the cycle ID for agent dispatch.

    Args:
        service_name: Canonical service name (must match SERVICE_CATALOGUE).
        notes:        Optional notes for this review cycle.

    Returns:
        review_cycle_id string (UUID).

    Raises:
        ValueError: If service_name not in SERVICE_CATALOGUE.
    """
    if service_name not in SERVICE_CATALOGUE:
        raise ValueError(
            f"Service '{service_name}' not in SERVICE_CATALOGUE. "
            f"Valid: {sorted(SERVICE_CATALOGUE.keys())}"
        )

    meta     = SERVICE_CATALOGUE[service_name]
    cycle_id = str(uuid.uuid4())
    now      = datetime.now(timezone.utc).isoformat()

    conn = _connect()
    try:
        conn.execute("""
            INSERT INTO pipeline_services
                (service_name, review_cycle_id, pipeline_state, grouping, criticality,
                 entered_at, state_updated_at, agents_complete, notes)
            VALUES (?, ?, 'REVIEW_REQUESTED', ?, ?, ?, ?, '[]', ?)
            ON CONFLICT(service_name) DO UPDATE SET
                review_cycle_id  = excluded.review_cycle_id,
                pipeline_state   = 'REVIEW_REQUESTED',
                entered_at       = excluded.entered_at,
                state_updated_at = excluded.state_updated_at,
                agents_complete  = '[]',
                all_reviews_done = 0,
                genesis_engaged_at = NULL,
                impl_complete_at   = NULL,
                cipher_approved_at = NULL,
                ahmed_approved_at  = NULL,
                soak_started_at    = NULL,
                deployed_at        = NULL,
                notes            = excluded.notes
        """, (
            service_name, cycle_id,
            meta["grouping"], meta["criticality"],
            now, now, notes,
        ))
        conn.commit()
        logger.info(
            "Stream B: service '%s' entered pipeline (cycle=%s grouping=%s crit=%d)",
            service_name, cycle_id[:8], meta["grouping"], meta["criticality"],
        )
        return cycle_id
    finally:
        conn.close()


# ── SOVEREIGN: Batch enter services ──────────────────────────────────────────

def enter_all_services() -> dict[str, str]:
    """
    Enter all services in SERVICE_CATALOGUE into the pipeline.

    Returns:
        Dict mapping service_name → review_cycle_id.
    """
    results = {}
    for service_name in sorted(SERVICE_CATALOGUE.keys()):
        try:
            cycle_id = enter_service(service_name)
            results[service_name] = cycle_id
        except Exception as e:
            logger.error("Failed to enter service '%s': %s", service_name, e)
    return results


# ── QA COUNCIL: Record agent review completion ────────────────────────────────

# Lens mapping for the ten v2 agents
_AGENT_LENS_MAP: dict[str, str] = {
    "PROBE-1":     "security",
    "PROBE-2":     "security",
    "EDGE-1":      "edge",
    "EDGE-2":      "edge",
    "CONTRACT-1":  "contract",
    "CONTRACT-2":  "contract",
    "DEPLOY-1":    "deploy",
    "DEPLOY-2":    "deploy",
    "ARCHITECT-1": "architect",
    "ARCHITECT-2": "architect",
}

# Brain mapping for the ten v2 agents
_AGENT_BRAIN_MAP: dict[str, str] = {
    "PROBE-1":     "DeepSeek",
    "PROBE-2":     "Claude Opus",
    "EDGE-1":      "GPT-5",
    "EDGE-2":      "Gemini 3 Pro",
    "CONTRACT-1":  "Gemini 3 Pro",
    "CONTRACT-2":  "DeepSeek",
    "DEPLOY-1":    "Cognition Devin",
    "DEPLOY-2":    "GPT-5",
    "ARCHITECT-1": "Claude Opus",
    "ARCHITECT-2": "Cognition Devin",
}


def record_agent_review(
    service_name:      str,
    reviewer_agent:    str,
    reviewer_brain:    str,
    overall_assessment: str,
    p0_count:          int,
    p1_count:          int,
    p2_count:          int,
    p3_count:          int,
    full_report:       str,
    confidence_level:  str,
) -> bool:
    """
    Record a QA Council agent's completed review in critique_bank.

    Advances pipeline_state:
    - On first agent: REVIEW_REQUESTED → REVIEW_IN_PROGRESS
    - When all five complete: REVIEW_IN_PROGRESS → READY_FOR_GENESIS

    Args:
        service_name:       Service being reviewed.
        reviewer_agent:     PROBE-1/PROBE-2/EDGE-1/EDGE-2/CONTRACT-1/CONTRACT-2/DEPLOY-1/DEPLOY-2/ARCHITECT-1/ARCHITECT-2
        reviewer_brain:     Auto-resolved from _AGENT_BRAIN_MAP if empty string passed; else use provided value
        overall_assessment: BLOCK / CONCERN / PASS_WITH_FINDINGS / PASS_CLEAN
        p0_count:           Count of P0 findings.
        p1_count:           Count of P1 findings.
        p2_count:           Count of P2 findings.
        p3_count:           Count of P3 findings.
        full_report:        Complete report text.
        confidence_level:   HIGH / MEDIUM / LOW

    Returns:
        True if all five agents are now complete (service is READY_FOR_GENESIS).
    """
    now = datetime.now(timezone.utc).isoformat()

    conn = _connect()
    try:
        # Get current pipeline record
        row = conn.execute(
            "SELECT review_cycle_id, agents_complete, pipeline_state "
            "FROM pipeline_services WHERE service_name=?",
            (service_name,),
        ).fetchone()

        if not row:
            logger.error("record_agent_review: service '%s' not in pipeline", service_name)
            return False

        cycle_id       = row["review_cycle_id"]
        agents_done    = set(json.loads(row["agents_complete"]))
        current_state  = row["pipeline_state"]

        # Resolve lens and brain from maps if not explicitly provided
        resolved_lens  = _AGENT_LENS_MAP.get(reviewer_agent, "unknown")
        resolved_brain = reviewer_brain or _AGENT_BRAIN_MAP.get(reviewer_agent, reviewer_brain)

        # Write critique_bank record
        conn.execute("""
            INSERT OR REPLACE INTO critique_bank
                (service_name, service_version, review_cycle_id, reviewer_agent,
                 reviewer_lens, reviewer_brain, review_status, overall_assessment,
                 p0_count, p1_count, p2_count, p3_count,
                 full_report, confidence_level, review_started_at, review_completed_at)
            VALUES (?, 'current', ?, ?, ?, ?, 'COMPLETE', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            service_name, cycle_id, reviewer_agent,
            resolved_lens, resolved_brain,
            overall_assessment, p0_count, p1_count, p2_count, p3_count,
            full_report, confidence_level, now, now,
        ))

        # Advance agents_complete
        agents_done.add(reviewer_agent)
        all_done = agents_done >= ALL_AGENTS

        # Determine new pipeline state
        new_state = current_state
        if current_state == "REVIEW_REQUESTED":
            new_state = "REVIEW_IN_PROGRESS"
        if all_done and current_state in ("REVIEW_REQUESTED", "REVIEW_IN_PROGRESS"):
            new_state = "READY_FOR_GENESIS"

        conn.execute("""
            UPDATE pipeline_services
            SET agents_complete  = ?,
                all_reviews_done = ?,
                pipeline_state   = ?,
                state_updated_at = ?
            WHERE service_name = ?
        """, (
            json.dumps(sorted(agents_done)),
            1 if all_done else 0,
            new_state,
            now,
            service_name,
        ))
        conn.commit()

        logger.info(
            "Stream B: %s review complete for '%s' — assessment=%s p0=%d p1=%d | "
            "agents_done=%s state=%s",
            reviewer_agent, service_name, overall_assessment,
            p0_count, p1_count, sorted(agents_done), new_state,
        )
        return all_done
    finally:
        conn.close()


# ── SOVEREIGN: Query accumulation for GENESIS trigger ─────────────────────────

def get_ready_for_genesis() -> list[dict]:
    """
    Return all services with state=READY_FOR_GENESIS.

    Used by SOVEREIGN to determine when to trigger GENESIS engagement.

    Returns:
        List of service dicts sorted by criticality then grouping.
    """
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT ps.service_name, ps.grouping, ps.criticality,
                   ps.review_cycle_id, ps.state_updated_at,
                   COUNT(cb.id) as review_count,
                   SUM(cb.p0_count) as total_p0,
                   SUM(cb.p1_count) as total_p1
            FROM pipeline_services ps
            LEFT JOIN critique_bank cb
                ON cb.service_name = ps.service_name
               AND cb.review_cycle_id = ps.review_cycle_id
            WHERE ps.pipeline_state = 'READY_FOR_GENESIS'
            GROUP BY ps.service_name
            ORDER BY ps.criticality ASC, ps.grouping ASC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_critical_p0_count() -> int:
    """
    Return total P0 findings across all services in READY_FOR_GENESIS state.
    Used by SOVEREIGN to signal urgency.
    """
    conn = _connect()
    try:
        row = conn.execute("""
            SELECT COALESCE(SUM(cb.p0_count), 0) as total_p0
            FROM critique_bank cb
            JOIN pipeline_services ps
                ON cb.service_name = ps.service_name
               AND cb.review_cycle_id = ps.review_cycle_id
            WHERE ps.pipeline_state = 'READY_FOR_GENESIS'
        """).fetchone()
        return row["total_p0"] if row else 0
    finally:
        conn.close()


# ── GENESIS: Engage with pipeline ─────────────────────────────────────────────

def genesis_engage(service_names: list[str]) -> list[dict]:
    """
    Mark services as GENESIS_ENGAGED and return all findings for holistic review.

    GENESIS calls this when beginning a Critique Bank engagement session.

    Args:
        service_names: List of service names to engage with.

    Returns:
        List of all critique_bank records for these services, with full reports.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        for service_name in service_names:
            conn.execute("""
                UPDATE pipeline_services
                SET pipeline_state   = 'GENESIS_ENGAGED',
                    genesis_engaged_at = ?,
                    state_updated_at = ?
                WHERE service_name = ? AND pipeline_state = 'READY_FOR_GENESIS'
            """, (now, now, service_name))
        conn.commit()

        # Fetch all findings
        placeholders = ",".join("?" for _ in service_names)
        rows = conn.execute(f"""
            SELECT cb.*, ps.grouping, ps.criticality
            FROM critique_bank cb
            JOIN pipeline_services ps
                ON cb.service_name = ps.service_name
               AND cb.review_cycle_id = ps.review_cycle_id
            WHERE cb.service_name IN ({placeholders})
            ORDER BY ps.criticality ASC, cb.service_name ASC, cb.reviewer_agent ASC
        """, service_names).fetchall()

        logger.info(
            "Stream B: GENESIS engaged with %d services, %d total findings records",
            len(service_names), len(rows),
        )
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── State advancement helpers ─────────────────────────────────────────────────

def advance_state(service_name: str, new_state: str, notes: str = "") -> None:
    """
    Advance a service to a new pipeline state.

    Valid states: IMPLEMENTATION_IN_PROGRESS, VERIFICATION_IN_PROGRESS,
    AWAITING_CIPHER, AWAITING_AHMED, STAGING_SOAK, DEPLOYED

    Args:
        service_name: Service to advance.
        new_state:    Target pipeline state.
        notes:        Optional notes appended to service record.
    """
    valid = {
        "IMPLEMENTATION_IN_PROGRESS", "VERIFICATION_IN_PROGRESS",
        "AWAITING_CICD_GATE",  # v2: mechanical gate between verification and Cipher
        "AWAITING_CIPHER", "AWAITING_AHMED", "STAGING_SOAK", "DEPLOYED",
        "GENESIS_ENGAGED",  # also callable directly
    }
    if new_state not in valid:
        raise ValueError(f"Invalid state: {new_state}. Valid: {valid}")

    now = datetime.now(timezone.utc).isoformat()
    timestamp_col_map = {
        "IMPLEMENTATION_IN_PROGRESS": "genesis_engaged_at",
        "VERIFICATION_IN_PROGRESS":   "impl_complete_at",
        "AWAITING_CICD_GATE":         "impl_complete_at",   # v2: gate before Cipher
        "AWAITING_CIPHER":            "impl_complete_at",
        "AWAITING_AHMED":             "cipher_approved_at",
        "STAGING_SOAK":               "ahmed_approved_at",
        "DEPLOYED":                   "deployed_at",
    }

    conn = _connect()
    try:
        extra_col = timestamp_col_map.get(new_state)
        if extra_col:
            conn.execute(f"""
                UPDATE pipeline_services
                SET pipeline_state   = ?,
                    {extra_col}      = COALESCE({extra_col}, ?),
                    state_updated_at = ?,
                    notes            = CASE WHEN ? != '' THEN ? ELSE notes END
                WHERE service_name = ?
            """, (new_state, now, now, notes, notes, service_name))
        else:
            conn.execute("""
                UPDATE pipeline_services
                SET pipeline_state   = ?,
                    state_updated_at = ?
                WHERE service_name = ?
            """, (new_state, now, service_name))
        conn.commit()
        logger.info("Stream B: '%s' → %s", service_name, new_state)
    finally:
        conn.close()


def mark_finding_verified(critique_bank_id: int, verified_by: str, status: str) -> None:
    """
    Mark a critique_bank finding as VERIFIED or REJECTED.

    Args:
        critique_bank_id: The critique_bank.id of the finding.
        verified_by:      Agent that verified (e.g. "PROBE").
        status:           "VERIFIED" or "REJECTED".
    """
    if status not in ("VERIFIED", "REJECTED"):
        raise ValueError("status must be VERIFIED or REJECTED")
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute("""
            UPDATE critique_bank
            SET verification_status = ?,
                verified_by_agent   = ?,
                findings_addressed_at = ?
            WHERE id = ?
        """, (status, verified_by, now, critique_bank_id))
        conn.commit()
        logger.info("Critique bank id=%d → %s (verified by %s)", critique_bank_id, status, verified_by)
    finally:
        conn.close()


# ── Dashboard query ───────────────────────────────────────────────────────────

def get_dashboard() -> list[dict]:
    """
    Return current pipeline state for all services.
    SOVEREIGN calls this to produce status reports for Ahmed.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM pipeline_dashboard"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_summary() -> dict:
    """
    Return a concise summary of pipeline state distribution.
    """
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT pipeline_state, COUNT(*) as count
            FROM pipeline_services
            GROUP BY pipeline_state
            ORDER BY count DESC
        """).fetchall()
        ready = conn.execute(
            "SELECT COUNT(*) as c FROM pipeline_services WHERE pipeline_state='READY_FOR_GENESIS'"
        ).fetchone()["c"]
        p0_urgent = get_critical_p0_count()
        conn.close()
        return {
            "state_distribution": {r["pipeline_state"]: r["count"] for r in rows},
            "ready_for_genesis":  ready,
            "p0_findings_pending": p0_urgent,
            "genesis_should_engage": ready >= 2 or p0_urgent > 0,
        }
    except Exception as e:
        logger.error("get_summary failed: %s", e)
        try:
            conn.close()
        except Exception:
            pass
        return {}


# ── CI/CD Gate (Principle 6 — Mechanical enforcement floor) ──────────────────

# Services authored by Cipher — Cipher cannot review these pre-deployment.
# ARCHITECT-1 (Claude Opus) substitutes.
CIPHER_AUTHORED_SERVICES: set[str] = set()  # populated as known; start empty

# 11-state pipeline sequence for v2
PIPELINE_STATES_V2 = [
    "REVIEW_REQUESTED",
    "REVIEW_IN_PROGRESS",
    "READY_FOR_GENESIS",
    "GENESIS_ENGAGED",
    "IMPLEMENTATION_IN_PROGRESS",
    "VERIFICATION_IN_PROGRESS",
    "AWAITING_CICD_GATE",     # NEW in v2 — mechanical gate before Cipher
    "AWAITING_CIPHER",
    "AWAITING_AHMED",
    "STAGING_SOAK",
    "DEPLOYED",
]


def record_cicd_result(
    service_name:           str,
    review_cycle_id:        str,
    unit_tests_pass:        bool,
    unit_tests_count:       int,
    unit_tests_failed:      int,
    integration_tests_pass: bool,
    regression_suite_pass:  bool,
    linting_pass:           bool,
    type_check_pass:        bool,
    health_endpoint_pass:   bool,
    test_output:            str = "",
    run_by:                 str = "GENESIS",
) -> bool:
    """
    Record the result of the CI/CD mechanical gate for a service.

    If all checks pass, advances pipeline state to AWAITING_CIPHER.
    If any check fails, sets state to AWAITING_CICD_GATE with failure reason
    recorded — service is blocked from advancing until gate passes.

    Args:
        service_name:           Service name.
        review_cycle_id:        Active review cycle ID.
        unit_tests_pass:        All unit tests passed.
        unit_tests_count:       Total unit tests run.
        unit_tests_failed:      Count of failing unit tests.
        integration_tests_pass: Integration tests against real Alpaca paper passed.
        regression_suite_pass:  Regression suite (every prior bug) passed.
        linting_pass:           Static analysis / linting passed.
        type_check_pass:        Type checking passed.
        health_endpoint_pass:   /health returns 200 after clean restart.
        test_output:            Captured test run output for audit.
        run_by:                 Who ran the gate (defaults to GENESIS).

    Returns:
        True if all checks passed (gate open), False if any failed (gate closed).
    """
    now = datetime.now(timezone.utc).isoformat()

    all_pass = all([
        unit_tests_pass,
        integration_tests_pass,
        regression_suite_pass,
        linting_pass,
        type_check_pass,
        health_endpoint_pass,
    ])

    failed_checks = []
    if not unit_tests_pass:
        failed_checks.append(f"unit_tests ({unit_tests_failed}/{unit_tests_count} failed)")
    if not integration_tests_pass:
        failed_checks.append("integration_tests")
    if not regression_suite_pass:
        failed_checks.append("regression_suite")
    if not linting_pass:
        failed_checks.append("linting")
    if not type_check_pass:
        failed_checks.append("type_check")
    if not health_endpoint_pass:
        failed_checks.append("health_endpoint")

    failure_reason = f"Failed checks: {', '.join(failed_checks)}" if failed_checks else None

    conn = _connect()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO cicd_gate
                (service_name, review_cycle_id, gate_status,
                 unit_tests_pass, unit_tests_count, unit_tests_failed,
                 integration_tests_pass, regression_suite_pass,
                 linting_pass, type_check_pass, health_endpoint_pass,
                 test_output, gate_run_at, gate_passed_at, gate_failed_at,
                 failure_reason, run_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            service_name, review_cycle_id,
            "PASS" if all_pass else "FAIL",
            1 if unit_tests_pass else 0,
            unit_tests_count, unit_tests_failed,
            1 if integration_tests_pass else 0,
            1 if regression_suite_pass else 0,
            1 if linting_pass else 0,
            1 if type_check_pass else 0,
            1 if health_endpoint_pass else 0,
            test_output[:10000] if test_output else "",  # cap output size
            now,
            now if all_pass else None,
            now if not all_pass else None,
            failure_reason,
            run_by,
        ))
        conn.commit()

        if all_pass:
            # Advance pipeline: AWAITING_CICD_GATE → AWAITING_CIPHER
            # Also resolve Cipher substitute if this is a Cipher-authored service
            is_cipher_authored = service_name in CIPHER_AUTHORED_SERVICES
            substitute = "ARCHITECT-1" if is_cipher_authored else None

            conn.execute("""
                UPDATE pipeline_services
                SET pipeline_state     = 'AWAITING_CIPHER',
                    state_updated_at   = ?,
                    cicd_gate_passed_at = ?,
                    cipher_authored    = ?,
                    cipher_substitute  = ?
                WHERE service_name = ?
            """, (now, now, 1 if is_cipher_authored else 0, substitute, service_name))
            conn.commit()
            logger.info(
                "CI/CD gate PASSED for '%s' — advancing to AWAITING_CIPHER%s",
                service_name,
                f" (Cipher authored — substitute: {substitute})" if is_cipher_authored else "",
            )
        else:
            # Gate failed — leave in AWAITING_CICD_GATE, log failure
            conn.execute("""
                UPDATE pipeline_services
                SET pipeline_state    = 'AWAITING_CICD_GATE',
                    state_updated_at  = ?,
                    cicd_gate_failed_at = ?
                WHERE service_name = ?
            """, (now, now, service_name))
            conn.commit()
            logger.error(
                "CI/CD gate FAILED for '%s' — blocked at AWAITING_CICD_GATE. %s",
                service_name, failure_reason,
            )

        return all_pass
    finally:
        conn.close()


def get_cicd_status(service_name: str) -> Optional[dict]:
    """
    Return the current CI/CD gate status for a service.

    Returns:
        Dict with gate_status, failed_checks, last_run_at, or None if not run yet.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM cicd_gate WHERE service_name=? ORDER BY gate_run_at DESC LIMIT 1",
            (service_name,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_cipher_authored(service_name: str) -> None:
    """
    Mark a service as authored by Cipher.

    Cipher will not review this service pre-deployment.
    ARCHITECT-1 (Claude Opus) will substitute.

    Args:
        service_name: Service name to mark.
    """
    CIPHER_AUTHORED_SERVICES.add(service_name)
    conn = _connect()
    try:
        conn.execute(
            "UPDATE pipeline_services SET cipher_authored=1, cipher_substitute='ARCHITECT-1' WHERE service_name=?",
            (service_name,),
        )
        conn.commit()
        logger.info("Service '%s' marked as Cipher-authored — ARCHITECT-1 will substitute for pre-deploy review", service_name)
    finally:
        conn.close()


def get_cipher_substitute(service_name: str) -> str:
    """
    Return the pre-deployment reviewer for a service.

    Returns 'Cipher' unless the service was authored by Cipher,
    in which case returns 'ARCHITECT-1'.

    Args:
        service_name: Service to check.

    Returns:
        'Cipher' or 'ARCHITECT-1'.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT cipher_authored, cipher_substitute FROM pipeline_services WHERE service_name=?",
            (service_name,),
        ).fetchone()
        if row and row["cipher_authored"]:
            return row["cipher_substitute"] or "ARCHITECT-1"
        return "Cipher"
    finally:
        conn.close()
