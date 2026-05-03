"""
VECTOR 30% Resilience Layer — agent-specific hardening.
Spec: vector-resilience-v1.md v1.1 (Cipher-reviewed, Ahmed-approved 2026-05-03)

Blocks:
  V1 — Diagnostic crash guard (tombstone + emergency Telegram)
  V2 — Typed RestartVerification contract (body status, not just HTTP 200)
  V3 — Log scan staleness guard (persistent lock dir)
  V4 — Credential watchdog liveness (the watcher has a watcher)
  V5 — Bus directive schema validation + consumed_ids dedup
  V6 — Poison bus message quarantine (handled inside V5)
  V7 — @audit_action decorator for structured action audit log
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from .crash_guard import run_with_crash_guard, is_tombstone_active, clear_tombstone, send_emergency_alert
from .verify import verify_service_recovery, RestartVerification, SERVICE_VERIFY_TIMEOUT
from .staleness import intervention_lock, check_log_staleness, InterventionInProgress
from .watchdog import check_watchdog_liveness, record_watchdog_run, days_since
from .directives import validate_and_process_directives, load_consumed_ids, save_consumed_id
from .audit import audit_action

__all__ = [
    "run_with_crash_guard", "is_tombstone_active", "clear_tombstone", "send_emergency_alert",
    "verify_service_recovery", "RestartVerification", "SERVICE_VERIFY_TIMEOUT",
    "intervention_lock", "check_log_staleness", "InterventionInProgress",
    "check_watchdog_liveness", "record_watchdog_run", "days_since",
    "validate_and_process_directives", "load_consumed_ids", "save_consumed_id",
    "audit_action",
]
