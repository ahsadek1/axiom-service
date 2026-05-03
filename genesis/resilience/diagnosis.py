"""
diagnosis.py — GENESIS Automated Diagnosis Engine (G3).
Spec: genesis-resilience-v1.md v1.2

Provides a CHRONICLE-backed playbook of error signatures → remediation actions.
Falls back to DEFAULT_DIAGNOSIS_TREE if CHRONICLE is unavailable.
Phase 2 (failure pattern recognition) is explicitly NOT in this sprint.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

logger = logging.getLogger("genesis.resilience.diagnosis")

# ── Default diagnosis tree ────────────────────────────────────────────────────
# Loaded from CHRONICLE at startup; this is the bundled fallback.
# Per-failure-class retry windows (Cipher G2B finding — no single "3 times" rule).

DEFAULT_DIAGNOSIS_TREE: dict[str, dict[str, Any]] = {
    "service_unreachable": {
        "description": "Service not responding to HTTP requests (timeout/connection refused)",
        "retry_count": 3,
        "retry_window_s": 90,
        "action": "restart_then_escalate",
    },
    "service_degraded": {
        "description": "Service responds but body status is not healthy/active",
        "retry_count": 2,
        "retry_window_s": 60,
        "action": "restart_then_escalate",
    },
    "db_locked": {
        "description": "SQLite OperationalError: database is locked — WAL contention",
        "retry_count": 1,
        "retry_window_s": 30,
        "action": "wal_flush_then_escalate",
    },
    "log_stale": {
        "description": "Log file has not been written to in >20 minutes — possible deadlock",
        "retry_count": 1,
        "retry_window_s": 300,
        "action": "check_process_then_escalate",
    },
    "oracle_crash_loop": {
        "description": "Oracle service restarted >5 times in 30 minutes — crash loop",
        "retry_count": 0,
        "retry_window_s": 0,
        "action": "escalate_only",
    },
    "brain_hang": {
        "description": "AI brain synthesis exceeded timeout — thread/semaphore hung",
        "retry_count": 1,
        "retry_window_s": 120,
        "action": "semaphore_release_then_escalate",
    },
}

# Fallback entry for unrecognized signatures
_ESCALATE_ONLY = {
    "description": "Unknown error signature — escalate immediately",
    "retry_count": 0,
    "retry_window_s": 0,
    "action": "escalate_only",
}


def load_diagnosis_tree() -> dict[str, dict[str, Any]]:
    """
    Load the diagnosis playbook from CHRONICLE.

    CHRONICLE is the authoritative source; DEFAULT_DIAGNOSIS_TREE is the
    bundled fallback used when CHRONICLE is unavailable.
    Refreshed every 30 minutes in production (caller's responsibility).

    Returns:
        Dict mapping error_signature → playbook entry dict.
        Never raises — always returns a usable tree.
    """
    try:
        sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
        from chronicle_reader import chronicle_read  # type: ignore
        data = chronicle_read("genesis_diagnosis_tree")
        if data and isinstance(data, dict):
            logger.info("load_diagnosis_tree: loaded %d entries from CHRONICLE", len(data))
            return data
        logger.info("load_diagnosis_tree: CHRONICLE returned empty — using default tree")
        return DEFAULT_DIAGNOSIS_TREE
    except Exception as e:
        logger.warning(
            "load_diagnosis_tree: CHRONICLE unavailable — using bundled default: %s", e
        )
        return DEFAULT_DIAGNOSIS_TREE


def diagnose(error_signature: str, context: dict) -> dict[str, Any]:
    """
    Look up an error signature in the diagnosis tree and return the playbook.

    Args:
        error_signature: Normalized error key (e.g. "service_unreachable", "brain_hang").
        context:         Additional context dict for logging/audit (service, timestamp, etc.).

    Returns:
        Playbook entry dict with keys: description, retry_count, retry_window_s, action.
        Returns escalate_only entry if signature not found.
    """
    tree = load_diagnosis_tree()
    entry = tree.get(error_signature)

    if entry is None:
        logger.warning(
            "diagnose: unknown signature %r — defaulting to escalate_only. context=%s",
            error_signature, context
        )
        return {**_ESCALATE_ONLY, "matched_signature": error_signature}

    logger.info(
        "diagnose: matched %r → action=%s retry=%d/%ds context=%s",
        error_signature, entry.get("action"), entry.get("retry_count", 0),
        entry.get("retry_window_s", 0), context
    )
    return {**entry, "matched_signature": error_signature}
