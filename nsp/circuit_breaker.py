"""
circuit_breaker.py — NSP Circuit Breaker (hardcoded, not configurable).

Rules (from spec — cannot be changed by config):
  • Max 3 restarts per service per hour
  • Max 10 total interventions per hour across all services
  • Either limit hit → FREEZE mode (zero autonomous actions)
  • FREEZE lifted ONLY by SOVEREIGN or Ahmed via explicit API call

All state is persisted in SQLite via state.py before every write so the
circuit breaker survives NSP restarts cleanly.

Public API:
    record_intervention(service)   → raises CircuitBreakerTripped if limit hit
    check_limits()                 → raises CircuitBreakerTripped if already in limit
    is_frozen()                    → True if FREEZE is active
"""

import logging
import time
from typing import Optional

from state import (
    get_freeze_mode,
    set_freeze_mode,
    get_intervention_count_last_hour,
    get_total_intervention_count_last_hour,
    write_intervention_state,
)

logger = logging.getLogger("nsp.circuit_breaker")

# Hardcoded limits — spec says these cannot be made configurable
MAX_RESTARTS_PER_SERVICE_PER_HOUR: int = 3
MAX_TOTAL_INTERVENTIONS_PER_HOUR: int = 10


class CircuitBreakerTripped(Exception):
    """Raised when an intervention would violate circuit breaker limits."""

    def __init__(self, reason: str, service: Optional[str] = None) -> None:
        """Initialise with reason string and optionally the triggering service."""
        self.reason = reason
        self.service = service
        super().__init__(reason)


def check_limits(service: str) -> None:
    """
    Check circuit breaker limits before allowing an intervention.

    Raises CircuitBreakerTripped if FREEZE is active or a limit would be exceeded.
    Does NOT record an intervention — call record_intervention() after the check.
    """
    # 1. Already in FREEZE?
    if get_freeze_mode():
        raise CircuitBreakerTripped(
            "NSP is in FREEZE mode — all autonomous actions suspended. "
            "Lift via /lift-freeze (SOVEREIGN or Ahmed only).",
            service=service,
        )

    # 2. Per-service hourly limit
    service_count = get_intervention_count_last_hour(service)
    if service_count >= MAX_RESTARTS_PER_SERVICE_PER_HOUR:
        reason = (
            f"Circuit breaker: {service} has been restarted {service_count}x "
            f"in the last hour (limit={MAX_RESTARTS_PER_SERVICE_PER_HOUR}). "
            "Triggering FREEZE."
        )
        logger.error(reason)
        set_freeze_mode(True, reason=reason)
        raise CircuitBreakerTripped(reason, service=service)

    # 3. Global hourly limit
    total_count = get_total_intervention_count_last_hour()
    if total_count >= MAX_TOTAL_INTERVENTIONS_PER_HOUR:
        reason = (
            f"Circuit breaker: {total_count} total interventions in the last hour "
            f"(limit={MAX_TOTAL_INTERVENTIONS_PER_HOUR}). Triggering FREEZE."
        )
        logger.error(reason)
        set_freeze_mode(True, reason=reason)
        raise CircuitBreakerTripped(reason, service=service)


def record_intervention(service: str) -> None:
    """
    Record that an intervention occurred for a service.

    Call AFTER check_limits() succeeds and BEFORE executing the fix action.
    State is written to SQLite so counts survive NSP restarts.
    """
    # Delegate to state.py which persists to SQLite before action
    write_intervention_state(
        incident_id=f"cb-record-{service}-{int(time.time())}",
        service=service,
        failure_class="RECORDED",
        attempt_number=0,
        action="circuit_breaker_record",
        state_snapshot={"recorded_at": time.time()},
    )
    logger.debug("Circuit breaker: recorded intervention for %s", service)

    # Re-check after recording to catch the boundary case
    try:
        check_limits(service)
    except CircuitBreakerTripped:
        # Limits now exceeded after this recording — FREEZE was already set
        raise


def is_frozen() -> bool:
    """Return True if NSP is in FREEZE mode (all autonomous actions suspended)."""
    return get_freeze_mode()


def lift_freeze(lifted_by: str) -> None:
    """
    Lift FREEZE mode. Only callable by SOVEREIGN or Ahmed via the /lift-freeze API.

    Parameters:
        lifted_by: Identity of the agent/user lifting the freeze (logged).
    """
    logger.warning("FREEZE lifted by: %s", lifted_by)
    set_freeze_mode(False, lifted_by=lifted_by)
