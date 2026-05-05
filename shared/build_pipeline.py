"""
build_pipeline.py — Stream A Build Pipeline Orchestrator

GENESIS uses this module to track components through the full build pipeline:
  QUEUED → SPEC_DRAFTING → SPEC_QA_REVIEW → SPEC_REFINEMENT →
  AWAITING_SPEC_APPROVAL → TESTS_WRITING → TESTS_REVIEW →
  IMPLEMENTATION → CODE_QA_REVIEW → ADDRESSING_FINDINGS →
  REAL_ENV_TESTS → AWAITING_CIPHER → AWAITING_AHMED →
  STAGING_SOAK → DEPLOYED

SOVEREIGN uses this module to:
  - Monitor which component is in active implementation
  - Know when GENESIS is at a natural break point (for Stream B engagement)
  - Track overall build queue state for Ahmed reporting

Key constraint: only ONE component in IMPLEMENTATION state at a time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("nexus.build_pipeline")

CHRONICLE_DB = "/Users/ahmedsadek/nexus/data/chronicle.db"

# All valid pipeline states in order
PIPELINE_STATES = [
    "QUEUED",
    "SPEC_DRAFTING",
    "SPEC_QA_REVIEW",
    "SPEC_REFINEMENT",
    "AWAITING_SPEC_APPROVAL",
    "TESTS_WRITING",
    "TESTS_REVIEW",
    "IMPLEMENTATION",
    "CODE_QA_REVIEW",
    "ADDRESSING_FINDINGS",
    "REAL_ENV_TESTS",
    "AWAITING_CICD_GATE",  # v2: mechanical gate before Cipher
    "AWAITING_CIPHER",
    "AWAITING_AHMED",
    "STAGING_SOAK",
    "DEPLOYED",
]

# States where GENESIS is NOT in active implementation — safe Stream B switch points
GENESIS_BREAK_STATES = {
    "AWAITING_SPEC_APPROVAL",   # spec submitted, waiting for Ahmed
    "STAGING_SOAK",             # deployed to staging, no active work
    "DEPLOYED",                 # complete, before next queue item
    "QUEUED",                   # not yet started
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CHRONICLE_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Queue queries ─────────────────────────────────────────────────────────────

def get_active_item() -> Optional[dict]:
    """
    Return the current active build item (lowest queue_position not DEPLOYED).

    Returns:
        Dict with full build_queue row, or None if queue is empty.
    """
    conn = _connect()
    try:
        row = conn.execute("""
            SELECT * FROM build_queue
            WHERE build_state != 'DEPLOYED'
            ORDER BY queue_position ASC
            LIMIT 1
        """).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_queue() -> list[dict]:
    """Return all items in build_queue ordered by position."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM build_queue ORDER BY queue_position ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_dashboard() -> list[dict]:
    """Return build_dashboard view — all items with human-readable state."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM build_dashboard").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_summary() -> dict:
    """
    Return a concise summary for SOVEREIGN reporting.

    Returns:
        Dict with active_component, active_state, is_genesis_break_point,
        queued_count, deployed_count.
    """
    conn = _connect()
    try:
        active = conn.execute("""
            SELECT component_name, build_state FROM build_queue
            WHERE build_state != 'DEPLOYED'
            ORDER BY queue_position ASC LIMIT 1
        """).fetchone()
        queued = conn.execute(
            "SELECT COUNT(*) FROM build_queue WHERE build_state = 'QUEUED'"
        ).fetchone()[0]
        deployed = conn.execute(
            "SELECT COUNT(*) FROM build_queue WHERE build_state = 'DEPLOYED'"
        ).fetchone()[0]

        active_state = active["build_state"] if active else None
        return {
            "active_component":       active["component_name"] if active else None,
            "active_state":           active_state,
            "is_genesis_break_point": active_state in GENESIS_BREAK_STATES if active_state else True,
            "queued_count":           queued,
            "deployed_count":         deployed,
            "total_items":            queued + deployed + (1 if active and active_state not in ("QUEUED","DEPLOYED") else 0),
        }
    finally:
        conn.close()


# ── State advancement ─────────────────────────────────────────────────────────

def advance_state(component_name: str, new_state: str, notes: str = "") -> None:
    """
    Advance a build queue item to the next pipeline state.

    Validates that IMPLEMENTATION state is only entered when no other
    component is currently in IMPLEMENTATION.

    Args:
        component_name: Component to advance.
        new_state:      Target state (must be in PIPELINE_STATES).
        notes:          Optional notes appended to the record.

    Raises:
        ValueError: If state is invalid or IMPLEMENTATION constraint violated.
    """
    if new_state not in PIPELINE_STATES:
        raise ValueError(f"Invalid state '{new_state}'. Valid: {PIPELINE_STATES}")

    # Enforce single-active-implementation constraint
    if new_state == "IMPLEMENTATION":
        conn = _connect()
        try:
            other_impl = conn.execute("""
                SELECT component_name FROM build_queue
                WHERE build_state = 'IMPLEMENTATION'
                  AND component_name != ?
            """, (component_name,)).fetchone()
            if other_impl:
                raise ValueError(
                    f"Cannot enter IMPLEMENTATION: '{other_impl['component_name']}' "
                    f"is already in IMPLEMENTATION state. Only one component can be "
                    f"in active implementation at a time."
                )
        finally:
            conn.close()

    now = datetime.now(timezone.utc).isoformat()

    # Map states to their timestamp columns
    timestamp_map = {
        "SPEC_DRAFTING":           "spec_drafted_at",
        "SPEC_QA_REVIEW":          "spec_qa_started_at",
        "SPEC_REFINEMENT":         None,
        "AWAITING_SPEC_APPROVAL":  "spec_qa_complete_at",
        "TESTS_WRITING":           "spec_approved_at",
        "TESTS_REVIEW":            "tests_written_at",
        "IMPLEMENTATION":          "tests_reviewed_at",
        "CODE_QA_REVIEW":          "impl_started_at",
        "ADDRESSING_FINDINGS":     "code_qa_complete_at",
        "REAL_ENV_TESTS":          "findings_addressed_at",
        "AWAITING_CICD_GATE":      "real_env_tests_at",   # v2: gate before Cipher
        "AWAITING_CIPHER":         "real_env_tests_at",   # keeps backward compat
        "AWAITING_AHMED":          "cipher_approved_at",
        "STAGING_SOAK":            "ahmed_approved_at",
        "DEPLOYED":                "deployed_at",
    }

    col = timestamp_map.get(new_state)
    conn = _connect()
    try:
        if col:
            conn.execute(f"""
                UPDATE build_queue
                SET build_state      = ?,
                    {col}            = COALESCE({col}, ?),
                    state_updated_at = ?,
                    notes            = CASE WHEN ? != '' THEN ? ELSE notes END
                WHERE component_name = ?
            """, (new_state, now, now, notes, notes, component_name))
        else:
            conn.execute("""
                UPDATE build_queue
                SET build_state      = ?,
                    state_updated_at = ?,
                    notes            = CASE WHEN ? != '' THEN ? ELSE notes END
                WHERE component_name = ?
            """, (new_state, now, notes, notes, component_name))
        conn.commit()
        logger.info("Stream A: '%s' → %s", component_name, new_state)
    finally:
        conn.close()

    # Discord mirror — post notable state changes to #findings-stream-a
    _DISCORD_NOTABLE_STATES = {
        "AWAITING_SPEC_APPROVAL": ("📋 Spec Ready for Approval", "yellow"),
        "IMPLEMENTATION":         ("🔨 Build Started",           "blue"),
        "REAL_ENV_TESTS":         ("🧪 Real-Env Tests Running",  "orange"),
        "AWAITING_CIPHER":        ("👁️ Awaiting Cipher Review",  "purple"),
        "AWAITING_AHMED":         ("✋ Awaiting Ahmed Approval", "gold"),
        "DEPLOYED":               ("✅ Deployed",                "green"),
    }
    if new_state in _DISCORD_NOTABLE_STATES:
        _title, _color = _DISCORD_NOTABLE_STATES[new_state]
        try:
            import sys as _sys, os as _os
            _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
            from discord_client import post_to_discord as _disc
            _disc(
                channel     = "findings-stream-a",
                title       = f"{_title} — {component_name}",
                description = notes or f"Pipeline state: {new_state}",
                color       = _color,
                agent       = "genesis",
                footer      = "GENESIS Build Pipeline",
            )
        except Exception as _de:
            logger.debug(f"Discord pipeline push skipped: {_de}")


def add_to_queue(
    component_name: str,
    component_type: str,
    purpose:        str,
    dependencies:   list[str] = None,
    spec_file:      str = None,
    notes:          str = "",
) -> int:
    """
    Add a new component to the build queue.

    Assigns the next available queue_position (after current last item).

    Args:
        component_name: Unique component name.
        component_type: SERVICE / GOVERNANCE / VALIDATION / AUDIT / INTEGRATION
        purpose:        One-sentence purpose statement.
        dependencies:   List of component_names that must be DEPLOYED first.
        spec_file:      Expected spec file path (can be set later).
        notes:          Optional notes.

    Returns:
        Assigned queue_position.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(queue_position), 0) FROM build_queue"
        ).fetchone()[0]
        new_pos = max_pos + 1

        conn.execute("""
            INSERT INTO build_queue
                (queue_position, component_name, component_type, purpose,
                 dependencies, build_state, spec_file, entered_at, state_updated_at, notes)
            VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?, ?)
        """, (
            new_pos, component_name, component_type, purpose,
            json.dumps(dependencies or []),
            spec_file, now, now, notes,
        ))
        conn.commit()
        logger.info("Stream A: '%s' added to queue at position %d", component_name, new_pos)
        return new_pos
    finally:
        conn.close()


# ── Dependency checking ───────────────────────────────────────────────────────

def dependencies_met(component_name: str) -> tuple[bool, list[str]]:
    """
    Check whether all dependencies for a component are DEPLOYED.

    Args:
        component_name: Component to check.

    Returns:
        Tuple of (all_met: bool, unmet_names: list[str]).
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT dependencies FROM build_queue WHERE component_name=?",
            (component_name,),
        ).fetchone()
        if not row:
            return True, []
        deps = json.loads(row["dependencies"] or "[]")
        if not deps:
            return True, []
        unmet = []
        for dep in deps:
            dep_row = conn.execute(
                "SELECT build_state FROM build_queue WHERE component_name=?",
                (dep,),
            ).fetchone()
            if not dep_row or dep_row["build_state"] != "DEPLOYED":
                unmet.append(dep)
        return len(unmet) == 0, unmet
    finally:
        conn.close()
