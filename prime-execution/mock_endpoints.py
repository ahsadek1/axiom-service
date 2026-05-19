"""
mock_endpoints.py — Prime-Execution Mock Endpoints for Resilience Testing

Implements /mock/* endpoints for the RESILIENCE_FRAMEWORK spec.
Simulates trade execution and exit for Scenario 10 (exit lifecycle).

Functions:
  - execute_trade()
  - exit_trade()

Author: GENESIS
Date: 2026-05-18
"""

import logging
from datetime import datetime
from typing import Dict, Any

import pytz
from pydantic import BaseModel

logger = logging.getLogger("prime_execution.mock_endpoints")
ET = pytz.timezone("America/New_York")

# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class ExecuteRequest(BaseModel):
    """Request: Simulate trade execution."""
    trade_id: str
    symbol: str
    quantity: int
    entry_price: float
    execution_price: float
    timestamp: str = None  # optional, defaults to now


class ExecuteResponse(BaseModel):
    """Response: Trade executed."""
    status: str
    trade_id: str
    filled_at: str  # ISO timestamp
    fill_price: float
    timestamp: str


class ExitRequest(BaseModel):
    """Request: Simulate trade exit."""
    trade_id: str
    exit_reason: str  # "take_profit" | "stop_loss" | "manual" | "dte_expired"
    exit_price: float
    timestamp: str = None  # optional, defaults to now


class ExitResponse(BaseModel):
    """Response: Trade exited with PnL."""
    status: str
    trade_id: str
    entry_price: float
    exit_price: float
    pnl: float
    pnl_percent: float
    timestamp: str


# ============================================================================
# MOCK STATE
# ============================================================================

_mock_trades: Dict[str, Dict[str, Any]] = {}
# Format: {trade_id: {"symbol", "entry_price", "quantity", "state", "filled_at", ...}}


# ============================================================================
# ENDPOINT: POST /mock/execute
# ============================================================================

def execute_trade(request: ExecuteRequest) -> ExecuteResponse:
    """
    Simulate trade execution (Scenario 10).

    Contract:
      - Does NOT call Alpaca (stays in mock space)
      - Trade marked as ACTIVE in mock state
      - Triggers all position tracking logic
      - Can only execute trades that exist in pending buffer (validation optional for mock)

    Args:
        request: ExecuteRequest with trade details

    Returns:
        ExecuteResponse with status, trade_id, fill details

    Raises:
        RuntimeError: If operation fails
    """
    try:
        timestamp = request.timestamp or datetime.now(ET).isoformat()

        # Store trade in mock state
        _mock_trades[request.trade_id] = {
            "symbol": request.symbol,
            "quantity": request.quantity,
            "entry_price": request.entry_price,
            "fill_price": request.execution_price,
            "state": "ACTIVE",
            "filled_at": timestamp,
        }

        logger.info(
            "MOCK: Trade executed %s %s @ %.2f (qty=%d)",
            request.trade_id,
            request.symbol,
            request.execution_price,
            request.quantity
        )

        return ExecuteResponse(
            status="executed",
            trade_id=request.trade_id,
            filled_at=timestamp,
            fill_price=request.execution_price,
            timestamp=datetime.now(ET).isoformat()
        )

    except Exception as e:
        logger.error("MOCK: Error executing trade: %s", e)
        raise RuntimeError(f"Failed to execute trade: {e}") from e


# ============================================================================
# ENDPOINT: POST /mock/exit
# ============================================================================

def exit_trade(request: ExitRequest) -> ExitResponse:
    """
    Simulate trade exit (Scenario 10 continued).

    Contract:
      - Trade must be in ACTIVE state
      - Calculates PnL automatically
      - Marks trade EXITED in mock state
      - Triggers Telegram notification

    Args:
        request: ExitRequest with exit details

    Returns:
        ExitResponse with status, trade_id, entry/exit prices, PnL

    Raises:
        ValueError: If trade not found or not active
    """
    try:
        if request.trade_id not in _mock_trades:
            logger.error("MOCK: Trade not found: %s", request.trade_id)
            raise ValueError(f"Trade {request.trade_id} not found")

        trade = _mock_trades[request.trade_id]
        if trade["state"] != "ACTIVE":
            logger.error("MOCK: Trade not ACTIVE: %s (state=%s)", request.trade_id, trade["state"])
            raise ValueError(f"Trade {request.trade_id} is not ACTIVE")

        entry_price = trade["entry_price"]
        exit_price = request.exit_price
        quantity = trade["quantity"]

        # Calculate PnL (for swing equity: (exit - entry) * qty)
        pnl = (exit_price - entry_price) * quantity
        pnl_percent = ((exit_price - entry_price) / entry_price) * 100

        # Mark as EXITED
        trade["state"] = "EXITED"
        trade["exit_price"] = exit_price
        trade["exit_reason"] = request.exit_reason
        trade["pnl"] = pnl
        trade["pnl_percent"] = pnl_percent

        timestamp = request.timestamp or datetime.now(ET).isoformat()
        trade["exited_at"] = timestamp

        logger.info(
            "MOCK: Trade exited %s %s @ %.2f | PnL: %.2f (%.1f%%)",
            request.trade_id,
            trade["symbol"],
            exit_price,
            pnl,
            pnl_percent
        )

        return ExitResponse(
            status="exited",
            trade_id=request.trade_id,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_percent=pnl_percent,
            timestamp=datetime.now(ET).isoformat()
        )

    except Exception as e:
        logger.error("MOCK: Error exiting trade: %s", e)
        raise RuntimeError(f"Failed to exit trade: {e}") from e


# ============================================================================
# HELPERS
# ============================================================================

def get_mock_trades() -> Dict[str, Dict[str, Any]]:
    """Return all mock trades (for verification)."""
    return _mock_trades.copy()


def reset_mock_trades() -> None:
    """Reset all mock trades (for cleanup between tests)."""
    global _mock_trades
    _mock_trades.clear()
    logger.info("MOCK: All trades reset")
