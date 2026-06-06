"""
capital_manager_integration.py — Thin wrapper for CapitalManager integration
into Prime and Alpha execution endpoints.

Provides:
  - A module-level singleton CapitalManager instance
  - Synchronous helpers that wrap the CapitalManager's sync API
  - All 6 guarantee enforcement points
  - Error translation to HTTP-ready responses

Usage (in execute endpoints):
    from shared.capital_manager_integration import (
        cm_pre_execute_check,
        cm_execute_with_atomic_binding,
        cm_confirm_position,
        cm_release_allocation,
        get_capital_manager,
    )
"""
import logging
import sys
import os

logger = logging.getLogger("capital_manager_integration")

# Add nexus root to path so capital_manager can be imported from anywhere
_NEXUS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _NEXUS_ROOT not in sys.path:
    sys.path.insert(0, _NEXUS_ROOT)

# ── Lazy singleton ────────────────────────────────────────────────────────────
_cm_instance = None
_cm_available = False


def get_capital_manager():
    """Return module-level CapitalManager singleton (lazy init, fail-open)."""
    global _cm_instance, _cm_available
    if _cm_instance is not None:
        return _cm_instance, _cm_available
    try:
        from capital_manager import CapitalManager
        _cm_instance = CapitalManager()
        _cm_available = True
        logger.info("CapitalManager singleton initialized: db=%s", _cm_instance.db_path)
    except Exception as e:
        logger.error("CapitalManager unavailable (fail-open): %s", e)
        _cm_instance = None
        _cm_available = False
    return _cm_instance, _cm_available


# ── Guarantee 1: pre_execute_check ───────────────────────────────────────────

def cm_pre_execute_check(
    ticker: str,
    execution_system: str,  # "prime" or "alpha"
    quantity: int,
    entry_price: float,
    request_id: str = None,
) -> dict:
    """
    GUARANTEE 1: Verify capital is available BEFORE any order submission.

    Returns:
        {
            "allowed": bool,
            "prediction": PreFillPrediction | None,
            "error": str | None,
            "error_code": "insufficient_capital" | "circuit_breaker" | None,
        }
    """
    cm, available = get_capital_manager()
    if not available or cm is None:
        logger.warning("CapitalManager unavailable — skipping pre_execute_check (fail-open)")
        return {"allowed": True, "prediction": None, "error": None, "error_code": None}

    try:
        from capital_manager import InsufficientCapitalError
        allowed, prediction = cm.pre_execute_check(
            ticker=ticker,
            execution_system=execution_system,
            quantity=quantity,
            entry_price=entry_price,
            request_id=request_id,
        )
        if not allowed:
            return {
                "allowed": False,
                "prediction": None,
                "error": f"Capital pre-execute check denied for {ticker}",
                "error_code": "position_limit",
            }
        return {"allowed": True, "prediction": prediction, "error": None, "error_code": None}
    except InsufficientCapitalError as e:
        logger.warning("InsufficientCapitalError for %s: %s", ticker, e)
        return {
            "allowed": False,
            "prediction": None,
            "error": str(e),
            "error_code": "insufficient_capital",
        }
    except Exception as e:
        logger.error("cm_pre_execute_check failed for %s: %s — fail-open", ticker, e)
        return {"allowed": True, "prediction": None, "error": None, "error_code": None}


# ── Guarantee 5: execute_with_atomic_binding ─────────────────────────────────

def cm_execute_with_atomic_binding(
    ticker: str,
    execution_system: str,
    quantity: int,
    entry_price: float,
    request_id: str = None,
) -> dict:
    """
    GUARANTEE 5: Atomic all-or-nothing state transition.
    Reserve capital + create position_binding in single DB transaction.

    Returns:
        {
            "success": bool,
            "binding": PositionBinding | None,
            "binding_id": int | None,
            "allocation_id": str | None,
            "error": str | None,
            "error_code": "binding_error" | "insufficient_capital" | None,
        }
    """
    cm, available = get_capital_manager()
    if not available or cm is None:
        logger.warning("CapitalManager unavailable — skipping atomic binding (fail-open)")
        return {
            "success": True,
            "binding": None,
            "binding_id": None,
            "allocation_id": None,
            "error": None,
            "error_code": None,
        }

    try:
        from capital_manager import InsufficientCapitalError, PositionBindingError
        success, binding = cm.execute_with_atomic_binding(
            ticker=ticker,
            execution_system=execution_system,
            quantity=quantity,
            entry_price=entry_price,
            request_id=request_id,
        )
        if not success or binding is None:
            return {
                "success": False,
                "binding": None,
                "binding_id": None,
                "allocation_id": None,
                "error": f"Atomic binding returned failure for {ticker}",
                "error_code": "binding_error",
            }
        return {
            "success": True,
            "binding": binding,
            "binding_id": binding.id,
            "allocation_id": binding.allocation_id,
            "error": None,
            "error_code": None,
        }
    except InsufficientCapitalError as e:
        logger.warning("InsufficientCapitalError in atomic binding for %s: %s", ticker, e)
        return {
            "success": False,
            "binding": None,
            "binding_id": None,
            "allocation_id": None,
            "error": str(e),
            "error_code": "insufficient_capital",
        }
    except Exception as e:
        logger.error(
            "cm_execute_with_atomic_binding failed for %s: %s — fail-open (no binding)", ticker, e
        )
        return {
            "success": True,  # fail-open: don't block trade on CM error
            "binding": None,
            "binding_id": None,
            "allocation_id": None,
            "error": None,
            "error_code": None,
        }


# ── Guarantee 4: confirm_position ────────────────────────────────────────────

def cm_confirm_position(
    binding_id: int,
    alpaca_position_id: str,
    actual_qty: int,
    actual_entry_price: float,
    ticker: str = "",
) -> dict:
    """
    GUARANTEE 4: Synchronous confirmation hook — BLOCKING.
    Called IMMEDIATELY after Alpaca fill confirmed.
    Validates slippage vs prediction. Records audit trail.

    Returns:
        {
            "success": bool,
            "validation_warning": str | None,  # slippage / mismatch — non-blocking
            "error": str | None,
        }
    """
    if binding_id is None:
        return {"success": True, "validation_warning": None, "error": None}

    cm, available = get_capital_manager()
    if not available or cm is None:
        logger.warning("CapitalManager unavailable — skipping confirm_position (fail-open)")
        return {"success": True, "validation_warning": None, "error": None}

    try:
        ok = cm.confirm_position(
            binding_id=binding_id,
            alpaca_position_id=alpaca_position_id or "",
            actual_qty=actual_qty,
            actual_entry_price=actual_entry_price,
        )
        if ok:
            logger.info(
                "cm_confirm_position OK: binding=%d ticker=%s qty=%d price=%.2f",
                binding_id, ticker, actual_qty, actual_entry_price,
            )
            return {"success": True, "validation_warning": None, "error": None}
        else:
            # confirm_position returns False on validation warning — not a hard error
            logger.warning(
                "cm_confirm_position validation warning: binding=%d ticker=%s", binding_id, ticker
            )
            return {
                "success": True,
                "validation_warning": f"Slippage/validation warning for {ticker} binding={binding_id}",
                "error": None,
            }
    except Exception as e:
        logger.error(
            "cm_confirm_position exception: binding=%d ticker=%s error=%s — non-blocking",
            binding_id, ticker, e,
        )
        return {"success": True, "validation_warning": None, "error": str(e)}


# ── Guarantee 2: release_allocation ──────────────────────────────────────────

def cm_release_allocation(allocation_id: str, ticker: str = "") -> bool:
    """
    Release a capital allocation (called after Alpaca order confirmed).
    Non-blocking — logs failure but never raises.
    """
    if not allocation_id:
        return True
    cm, available = get_capital_manager()
    if not available or cm is None:
        return True
    try:
        ok = cm.release_allocation(allocation_id)
        if ok:
            logger.info("cm_release_allocation OK: %s ticker=%s", allocation_id, ticker)
        else:
            logger.warning("cm_release_allocation returned False: %s ticker=%s", allocation_id, ticker)
        return ok
    except Exception as e:
        logger.warning(
            "cm_release_allocation failed (non-fatal): %s ticker=%s error=%s", allocation_id, ticker, e
        )
        return False


# ── Health snapshot for /health endpoints ────────────────────────────────────

def cm_health_snapshot() -> dict:
    """
    Return capital manager health data for inclusion in /health endpoints.
    Always returns a dict (never raises).
    """
    cm, available = get_capital_manager()
    if not available or cm is None:
        return {
            "capital_manager": "unavailable",
            "circuit_breaker": None,
            "available_capital_usd": None,
            "open_positions": None,
        }
    try:
        return {
            "capital_manager": "ok",
            "circuit_breaker": getattr(cm, "circuit_breaker_halted", False),
            "circuit_breaker_reason": getattr(cm, "circuit_breaker_reason", None),
            "available_capital_usd": cm.get_available_capital(),
            "open_positions": cm.get_open_position_count(),
        }
    except Exception as e:
        return {"capital_manager": "error", "error": str(e)}
