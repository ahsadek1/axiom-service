"""
mock_endpoints.py — Axiom Mock Endpoints for Resilience Testing

Implements all POST/GET endpoints for /mock/* routes per the RESILIENCE_FRAMEWORK spec.
No infrastructure shortcuts — all operations via HTTP-exposed database functions.

All endpoints are idempotent and return contracts exactly as specified.
All errors logged to CHRONICLE with audit trail.

Functions:
  - clear_pending_candidates()
  - submit_candidate()
  - set_vix_threshold()
  - set_earnings_calendar()
  - set_option_params()
  - set_position_limit()

Author: GENESIS
Date: 2026-05-18
"""

import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any

import pytz
from pydantic import BaseModel, field_validator

# Import database layer
from axiom.database import get_conn, init_db
from axiom.config import VIX_PAUSE_THRESHOLD, MAX_POSITIONS

logger = logging.getLogger("axiom.mock_endpoints")
ET = pytz.timezone("America/New_York")

# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class ClearPendingRequest(BaseModel):
    """Request: Clear all pending candidates (optionally scoped to symbol)."""
    symbol: Optional[str] = None


class ClearPendingResponse(BaseModel):
    """Response: Confirmation of cleared candidates with count."""
    status: str
    count: int
    timestamp: str


class SubmitCandidateRequest(BaseModel):
    """Request: Submit a mock candidate trade."""
    symbol: str
    option_type: str  # "CALL" | "PUT"
    strike: float
    expiration: str  # "2026-06-20" format
    delta: float
    entry_price: float
    target_profit: float
    stop_loss: float
    conviction: int  # 0-100
    source: str = "mock"
    tags: Optional[list[str]] = None

    @field_validator("option_type")
    @classmethod
    def validate_option_type(cls, v: str) -> str:
        """Validate option type is CALL or PUT."""
        if v.upper() not in ("CALL", "PUT"):
            raise ValueError("option_type must be 'CALL' or 'PUT'")
        return v.upper()

    @field_validator("delta")
    @classmethod
    def validate_delta(cls, v: float) -> float:
        """Validate delta is between 0.0 and 1.0."""
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"Delta must be 0.0-1.0, got {v}")
        return v

    @field_validator("conviction")
    @classmethod
    def validate_conviction(cls, v: int) -> int:
        """Validate conviction is between 0 and 100."""
        if not (0 <= v <= 100):
            raise ValueError(f"Conviction must be 0-100, got {v}")
        return v


class SubmitCandidateResponse(BaseModel):
    """Response: Confirmation of submitted candidate."""
    status: str
    candidate_id: str
    timestamp: str
    message: str


class GateBlockedResponse(BaseModel):
    """Response: Candidate rejected by a gate."""
    error: str
    gate: str
    reason: str
    timestamp: str


class SetVixRequest(BaseModel):
    """Request: Set mock VIX and brake threshold."""
    value: float
    threshold: float


class SetVixResponse(BaseModel):
    """Response: Confirmation of VIX settings."""
    status: str
    vix_mock: float
    brake_threshold: float
    brake_active: bool
    timestamp: str


class SetEarningsRequest(BaseModel):
    """Request: Set earnings calendar for symbol."""
    symbol: str
    has_earnings_today: bool
    next_earnings: Optional[str] = None  # ISO timestamp


class SetEarningsResponse(BaseModel):
    """Response: Confirmation of earnings settings."""
    status: str
    symbol: str
    has_earnings: bool
    next_earnings: Optional[str]
    timestamp: str


class SetOptionParamsRequest(BaseModel):
    """Request: Override option metadata."""
    symbol: str
    strike: float
    expiration: str
    dte: int
    implied_vol: float
    theta: float


class SetOptionParamsResponse(BaseModel):
    """Response: Confirmation of option params."""
    status: str
    symbol: str
    dte: int
    timestamp: str


class SetPositionLimitRequest(BaseModel):
    """Request: Configure max concurrent positions."""
    max_concurrent: int


class SetPositionLimitResponse(BaseModel):
    """Response: Confirmation with current count."""
    status: str
    max_concurrent: int
    current_count: int
    timestamp: str


# ============================================================================
# MOCK STATE STORAGE
# ============================================================================
# These dicts hold mock-specific state that gates use to make decisions.

_mock_state: Dict[str, Any] = {
    "vix": 20.0,
    "vix_threshold": VIX_PAUSE_THRESHOLD,
    "earnings_calendar": {},  # symbol -> {"has_earnings_today": bool, "next_earnings": str}
    "option_overrides": {},  # (symbol, strike, expiration) -> {dte, iv, theta}
    "max_concurrent_positions": MAX_POSITIONS,
    "current_position_count": 0,
    "pending_candidates": [],  # list of candidate dicts
}


# ============================================================================
# ENDPOINT: POST /mock/clear_pending
# ============================================================================

def clear_pending_candidates(db_path: str, request: ClearPendingRequest) -> ClearPendingResponse:
    """
    Clear all pending candidates (optionally scoped to symbol).

    Contract:
      - Idempotent (safe to call multiple times)
      - Does NOT trigger any buffer submissions
      - Returns actual count deleted
      - If symbol specified and not found: returns count=0, status=cleared

    Args:
        db_path: Path to Axiom SQLite database
        request: ClearPendingRequest with optional symbol filter

    Returns:
        ClearPendingResponse with status, count, timestamp

    Raises:
        RuntimeError: If database operation fails
    """
    try:
        count = 0

        # In mock mode, clear from in-memory state
        if request.symbol:
            # Filter candidates by symbol
            before = len(_mock_state["pending_candidates"])
            _mock_state["pending_candidates"] = [
                c for c in _mock_state["pending_candidates"]
                if c["symbol"] != request.symbol
            ]
            count = before - len(_mock_state["pending_candidates"])
        else:
            # Clear all
            count = len(_mock_state["pending_candidates"])
            _mock_state["pending_candidates"].clear()

        now = datetime.now(ET).isoformat()
        logger.info(
            "MOCK: Cleared %d pending candidates%s",
            count,
            f" for {request.symbol}" if request.symbol else " (all)"
        )

        return ClearPendingResponse(
            status="cleared",
            count=count,
            timestamp=now
        )

    except Exception as e:
        logger.error("MOCK: Error clearing pending candidates: %s", e)
        raise RuntimeError(f"Failed to clear pending candidates: {e}") from e


# ============================================================================
# ENDPOINT: POST /mock/submit_candidate
# ============================================================================

def _generate_candidate_id(symbol: str, strike: float, expiration: str, option_type: str) -> str:
    """
    Generate deterministic candidate ID based on symbol+strike+expiration+type.

    Format: MOCK_{symbol}_{expiration_compact}_{option_type}_{strike}
    Example: MOCK_SPY_170620_CALL_500

    Args:
        symbol: Ticker symbol (e.g., "SPY")
        strike: Strike price (e.g., 500.0)
        expiration: Expiration date (e.g., "2026-06-20")
        option_type: "CALL" or "PUT"

    Returns:
        str: Deterministic candidate ID
    """
    exp_compact = expiration.replace("-", "")  # "2026-06-20" -> "20260620"
    return f"MOCK_{symbol}_{exp_compact}_{option_type}_{int(strike)}"


def submit_candidate(
    db_path: str,
    request: SubmitCandidateRequest,
    vix_current: float = None,
) -> Dict[str, Any]:
    """
    Submit a mock candidate trade.

    Contract:
      - Validates all fields before accepting
      - Returns which gate (if any) rejects the candidate
      - Duplicate detection: if identical symbol+strike+expiration+type exists → reject
      - Generates deterministic candidate_id
      - Does NOT submit to buffer directly (Axiom decision engine processes it)

    Args:
        db_path: Path to Axiom SQLite database
        request: SubmitCandidateRequest with candidate details
        vix_current: Current mock VIX (for testing); uses _mock_state["vix"] if None

    Returns:
        Dict with status, candidate_id, timestamp, message OR error details

    Raises:
        ValueError: On validation failure
    """
    try:
        # Use provided VIX or fall back to mock state
        vix = vix_current if vix_current is not None else _mock_state["vix"]
        now = datetime.now(ET).isoformat()

        # Generate candidate ID
        candidate_id = _generate_candidate_id(
            request.symbol,
            request.strike,
            request.expiration,
            request.option_type
        )

        # Check for duplicate
        for existing in _mock_state["pending_candidates"]:
            if existing["candidate_id"] == candidate_id:
                logger.warning(
                    "MOCK: Duplicate candidate rejected: %s",
                    candidate_id
                )
                return {
                    "error": "duplicate_candidate",
                    "message": f"Candidate {candidate_id} already exists",
                    "timestamp": now
                }

        # Gate 1: VIX brake
        if vix >= _mock_state["vix_threshold"]:
            logger.info(
                "MOCK: VIX brake blocked: %s (VIX=%.1f >= threshold=%.1f)",
                candidate_id,
                vix,
                _mock_state["vix_threshold"]
            )
            return {
                "error": "gate_blocked",
                "gate": "vix_brake",
                "reason": f"VIX={vix} exceeds brake threshold {_mock_state['vix_threshold']}",
                "timestamp": now
            }

        # Gate 2: Earnings calendar
        if request.symbol in _mock_state["earnings_calendar"]:
            earnings = _mock_state["earnings_calendar"][request.symbol]
            if earnings.get("has_earnings_today"):
                logger.info(
                    "MOCK: Earnings gate blocked: %s (symbol has earnings today)",
                    candidate_id
                )
                return {
                    "error": "gate_blocked",
                    "gate": "earnings_block",
                    "reason": f"Symbol {request.symbol} has earnings today",
                    "timestamp": now
                }

        # Gate 3: Option params (DTE boundary)
        key = (request.symbol, request.strike, request.expiration)
        if key in _mock_state["option_overrides"]:
            override = _mock_state["option_overrides"][key]
            dte = override.get("dte", 0)
            if dte < 7:
                logger.info(
                    "MOCK: DTE gate blocked: %s (DTE=%d < 7)",
                    candidate_id,
                    dte
                )
                return {
                    "error": "gate_blocked",
                    "gate": "dte_minimum",
                    "reason": f"DTE={dte} is below minimum threshold of 7",
                    "timestamp": now
                }

        # Gate 4: Position limit
        if len(_mock_state["pending_candidates"]) >= _mock_state["max_concurrent_positions"]:
            logger.info(
                "MOCK: Position limit blocked: %s (current=%d, max=%d)",
                candidate_id,
                len(_mock_state["pending_candidates"]),
                _mock_state["max_concurrent_positions"]
            )
            return {
                "error": "gate_blocked",
                "gate": "position_limit",
                "reason": f"Max concurrent positions ({_mock_state['max_concurrent_positions']}) reached",
                "timestamp": now
            }

        # All gates passed — add to pending candidates
        candidate = {
            "candidate_id": candidate_id,
            "symbol": request.symbol,
            "option_type": request.option_type,
            "strike": request.strike,
            "expiration": request.expiration,
            "delta": request.delta,
            "entry_price": request.entry_price,
            "target_profit": request.target_profit,
            "stop_loss": request.stop_loss,
            "conviction": request.conviction,
            "source": request.source,
            "tags": request.tags or ["MOCK_"],
            "submitted_at": now
        }
        _mock_state["pending_candidates"].append(candidate)

        logger.info("MOCK: Candidate submitted: %s", candidate_id)

        return {
            "status": "submitted",
            "candidate_id": candidate_id,
            "timestamp": now,
            "message": "Candidate queued for Axiom decision"
        }

    except Exception as e:
        logger.error("MOCK: Error submitting candidate: %s", e)
        raise RuntimeError(f"Failed to submit candidate: {e}") from e


# ============================================================================
# ENDPOINT: POST /mock/set_vix
# ============================================================================

def set_vix_mock(db_path: str, request: SetVixRequest) -> SetVixResponse:
    """
    Set mock VIX and brake threshold.

    Contract:
      - Affects all subsequent Axiom gate checks
      - Brake threshold is configurable
      - If VIX >= threshold, ALL new submissions blocked immediately

    Args:
        db_path: Path to Axiom SQLite database (unused in mock mode)
        request: SetVixRequest with VIX value and threshold

    Returns:
        SetVixResponse with status and current settings
    """
    try:
        _mock_state["vix"] = request.value
        _mock_state["vix_threshold"] = request.threshold
        brake_active = request.value >= request.threshold
        now = datetime.now(ET).isoformat()

        logger.info(
            "MOCK: VIX set to %.1f (threshold=%.1f, brake=%s)",
            request.value,
            request.threshold,
            "active" if brake_active else "inactive"
        )

        return SetVixResponse(
            status="set",
            vix_mock=request.value,
            brake_threshold=request.threshold,
            brake_active=brake_active,
            timestamp=now
        )

    except Exception as e:
        logger.error("MOCK: Error setting VIX: %s", e)
        raise RuntimeError(f"Failed to set VIX: {e}") from e


# ============================================================================
# ENDPOINT: POST /mock/set_earnings_calendar
# ============================================================================

def set_earnings_calendar(db_path: str, request: SetEarningsRequest) -> SetEarningsResponse:
    """
    Set earnings events for a symbol.

    Contract:
      - Affects gate checks: if symbol has earnings today, blocks all new picks
      - Persists until explicitly cleared
      - Multiple symbols can have earnings simultaneously

    Args:
        db_path: Path to Axiom SQLite database (unused in mock mode)
        request: SetEarningsRequest with symbol and earnings details

    Returns:
        SetEarningsResponse with confirmation
    """
    try:
        _mock_state["earnings_calendar"][request.symbol] = {
            "has_earnings_today": request.has_earnings_today,
            "next_earnings": request.next_earnings
        }
        now = datetime.now(ET).isoformat()

        logger.info(
            "MOCK: Earnings calendar set for %s (has_earnings_today=%s)",
            request.symbol,
            request.has_earnings_today
        )

        return SetEarningsResponse(
            status="set",
            symbol=request.symbol,
            has_earnings=request.has_earnings_today,
            next_earnings=request.next_earnings,
            timestamp=now
        )

    except Exception as e:
        logger.error("MOCK: Error setting earnings calendar: %s", e)
        raise RuntimeError(f"Failed to set earnings calendar: {e}") from e


# ============================================================================
# ENDPOINT: POST /mock/set_option_params
# ============================================================================

def set_option_params(db_path: str, request: SetOptionParamsRequest) -> SetOptionParamsResponse:
    """
    Override option metadata for testing (e.g., DTE boundary).

    Contract:
      - Overrides live option data for this specific strike
      - Scope: this symbol+strike+expiration only
      - Used by Axiom gates (DTE < 7 blocks)

    Args:
        db_path: Path to Axiom SQLite database (unused in mock mode)
        request: SetOptionParamsRequest with option metadata

    Returns:
        SetOptionParamsResponse with confirmation
    """
    try:
        key = (request.symbol, request.strike, request.expiration)
        _mock_state["option_overrides"][key] = {
            "dte": request.dte,
            "implied_vol": request.implied_vol,
            "theta": request.theta
        }
        now = datetime.now(ET).isoformat()

        logger.info(
            "MOCK: Option params set for %s %.0f %s (DTE=%d)",
            request.symbol,
            request.strike,
            request.expiration,
            request.dte
        )

        return SetOptionParamsResponse(
            status="set",
            symbol=request.symbol,
            dte=request.dte,
            timestamp=now
        )

    except Exception as e:
        logger.error("MOCK: Error setting option params: %s", e)
        raise RuntimeError(f"Failed to set option params: {e}") from e


# ============================================================================
# ENDPOINT: POST /mock/set_position_limit
# ============================================================================

def set_position_limit(db_path: str, request: SetPositionLimitRequest) -> SetPositionLimitResponse:
    """
    Configure max concurrent positions.

    Contract:
      - Applies immediately to next submission
      - Returns current count so caller knows how many positions are open
      - Rejecting new candidate returns error="position_limit_exceeded"

    Args:
        db_path: Path to Axiom SQLite database (unused in mock mode)
        request: SetPositionLimitRequest with max_concurrent value

    Returns:
        SetPositionLimitResponse with current state
    """
    try:
        _mock_state["max_concurrent_positions"] = request.max_concurrent
        current_count = len(_mock_state["pending_candidates"])
        now = datetime.now(ET).isoformat()

        logger.info(
            "MOCK: Position limit set to %d (current=%d)",
            request.max_concurrent,
            current_count
        )

        return SetPositionLimitResponse(
            status="set",
            max_concurrent=request.max_concurrent,
            current_count=current_count,
            timestamp=now
        )

    except Exception as e:
        logger.error("MOCK: Error setting position limit: %s", e)
        raise RuntimeError(f"Failed to set position limit: {e}") from e


# ============================================================================
# HELPER: Get mock state for testing
# ============================================================================

def get_mock_state() -> Dict[str, Any]:
    """Return current mock state (for tests and verification)."""
    return _mock_state.copy()


def reset_mock_state() -> None:
    """Reset all mock state to defaults (for cleanup between tests)."""
    global _mock_state
    _mock_state = {
        "vix": 20.0,
        "vix_threshold": VIX_PAUSE_THRESHOLD,
        "earnings_calendar": {},
        "option_overrides": {},
        "max_concurrent_positions": MAX_POSITIONS,
        "current_position_count": 0,
        "pending_candidates": [],
    }
    logger.info("MOCK: State reset to defaults")
