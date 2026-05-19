"""
mock_endpoints.py — Alpha-Buffer Mock Endpoints for Resilience Testing

Implements /mock/* endpoints for the RESILIENCE_FRAMEWORK spec.
No infrastructure shortcuts — all operations via HTTP-exposed database functions.

Functions:
  - clear_pending_picks()

Author: GENESIS
Date: 2026-05-18
"""

import logging
from datetime import datetime
from typing import Optional

import pytz
from pydantic import BaseModel

logger = logging.getLogger("alpha_buffer.mock_endpoints")
ET = pytz.timezone("America/New_York")

# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class ClearPendingRequest(BaseModel):
    """Request: Clear all pending picks (optionally scoped to symbol)."""
    symbol: Optional[str] = None


class ClearPendingResponse(BaseModel):
    """Response: Confirmation of cleared picks with count."""
    status: str
    count: int
    timestamp: str


# ============================================================================
# ENDPOINT: POST /mock/clear_pending
# ============================================================================

def clear_pending_picks(db_connection, request: ClearPendingRequest) -> ClearPendingResponse:
    """
    Clear all pending picks from buffer (Scenario 1 setup).

    Contract:
      - Idempotent (safe to call multiple times)
      - Returns actual count cleared
      - Does NOT execute any picks
      - Does NOT affect CHRONICLE logs (audit trail stays)

    Args:
        db_connection: Database connection object
        request: ClearPendingRequest with optional symbol filter

    Returns:
        ClearPendingResponse with status, count, timestamp

    Raises:
        RuntimeError: If database operation fails
    """
    try:
        now = datetime.now(ET).isoformat()
        count = 0

        # Query pending picks
        cursor = db_connection.cursor()

        if request.symbol:
            # Clear picks for specific symbol
            cursor.execute(
                "SELECT COUNT(*) FROM pending_picks WHERE symbol = ?",
                (request.symbol,)
            )
            count = cursor.fetchone()[0]
            cursor.execute(
                "DELETE FROM pending_picks WHERE symbol = ?",
                (request.symbol,)
            )
        else:
            # Clear all picks
            cursor.execute("SELECT COUNT(*) FROM pending_picks")
            count = cursor.fetchone()[0]
            cursor.execute("DELETE FROM pending_picks")

        db_connection.commit()

        logger.info(
            "MOCK: Cleared %d pending picks%s",
            count,
            f" for {request.symbol}" if request.symbol else " (all)"
        )

        return ClearPendingResponse(
            status="cleared",
            count=count,
            timestamp=now
        )

    except Exception as e:
        db_connection.rollback()
        logger.error("MOCK: Error clearing pending picks: %s", e)
        raise RuntimeError(f"Failed to clear pending picks: {e}") from e
