"""
mock_endpoints.py — Oracle Mock Endpoints for Resilience Testing

Implements /mock/simulate/* endpoints for the RESILIENCE_FRAMEWORK spec.
Simulates service availability for Scenario 5 (fallback data source testing).

Functions:
  - simulate_down()
  - simulate_up()

Author: GENESIS
Date: 2026-05-18
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import pytz
from pydantic import BaseModel

logger = logging.getLogger("oracle.mock_endpoints")
ET = pytz.timezone("America/New_York")

# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class SimulateDownRequest(BaseModel):
    """Request: Simulate Oracle service unavailable."""
    duration_seconds: int = 30  # optional: auto-recover after this time
    reason: str = "planned_test"  # for audit


class SimulateDownResponse(BaseModel):
    """Response: Confirmation Oracle is down."""
    status: str
    will_recover_at: str  # ISO timestamp
    reason: str
    timestamp: str


class SimulateUpRequest(BaseModel):
    """Request: Resume normal Oracle operation."""
    pass


class SimulateUpResponse(BaseModel):
    """Response: Confirmation Oracle is operational."""
    status: str
    uptime: str  # ISO timestamp
    timestamp: str


# ============================================================================
# MOCK STATE
# ============================================================================

_mock_state = {
    "is_down": False,
    "down_until": None,  # datetime when to auto-recover
    "reason": None,
}


# ============================================================================
# ENDPOINT: POST /mock/simulate/down
# ============================================================================

def simulate_down(request: SimulateDownRequest) -> SimulateDownResponse:
    """
    Simulate Oracle service unavailable (Scenario 5).

    Contract:
      - All subsequent calls to Oracle return 503 Service Unavailable
      - Axiom automatically falls back to secondary data source
      - Can specify auto-recovery time or recover manually via /simulate/up

    Args:
        request: SimulateDownRequest with optional duration and reason

    Returns:
        SimulateDownResponse with status, recovery time, reason, timestamp

    Raises:
        RuntimeError: If operation fails
    """
    try:
        now = datetime.now(ET)
        recovery_time = now + timedelta(seconds=request.duration_seconds)

        _mock_state["is_down"] = True
        _mock_state["down_until"] = recovery_time
        _mock_state["reason"] = request.reason

        logger.warning(
            "MOCK: Oracle simulating DOWN for %d seconds (reason: %s)",
            request.duration_seconds,
            request.reason
        )

        return SimulateDownResponse(
            status="simulating_down",
            will_recover_at=recovery_time.isoformat(),
            reason=request.reason,
            timestamp=now.isoformat()
        )

    except Exception as e:
        logger.error("MOCK: Error simulating down: %s", e)
        raise RuntimeError(f"Failed to simulate down: {e}") from e


# ============================================================================
# ENDPOINT: POST /mock/simulate/up
# ============================================================================

def simulate_up() -> SimulateUpResponse:
    """
    Resume normal Oracle operation (Scenario 5 cleanup).

    Contract:
      - Returns to normal data delivery immediately
      - No impact on trades already executed

    Returns:
        SimulateUpResponse with status, uptime timestamp

    Raises:
        RuntimeError: If operation fails
    """
    try:
        now = datetime.now(ET)

        _mock_state["is_down"] = False
        _mock_state["down_until"] = None
        _mock_state["reason"] = None

        logger.info("MOCK: Oracle resuming normal operation")

        return SimulateUpResponse(
            status="operational",
            uptime=now.isoformat(),
            timestamp=now.isoformat()
        )

    except Exception as e:
        logger.error("MOCK: Error simulating up: %s", e)
        raise RuntimeError(f"Failed to simulate up: {e}") from e


# ============================================================================
# HELPER: Check if Oracle should be simulating down
# ============================================================================

def is_simulating_down() -> bool:
    """
    Check if Oracle is currently simulating down.
    Auto-recovers if duration has expired.

    Returns:
        bool: True if Oracle should return 503, False if operational
    """
    if not _mock_state["is_down"]:
        return False

    # Check if auto-recovery time has passed
    now = datetime.now(ET)
    if _mock_state["down_until"] and now >= _mock_state["down_until"]:
        # Auto-recover
        _mock_state["is_down"] = False
        _mock_state["down_until"] = None
        logger.info("MOCK: Oracle auto-recovered from simulated downtime")
        return False

    return True


def get_mock_state():
    """Return current mock state (for tests)."""
    return _mock_state.copy()


def reset_mock_state():
    """Reset all mock state to defaults (for cleanup)."""
    global _mock_state
    _mock_state = {
        "is_down": False,
        "down_until": None,
        "reason": None,
    }
    logger.info("MOCK: Oracle state reset to defaults")
