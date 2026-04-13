"""
ORACLE — ORATS Client (stub)
Provides: vol surface, spread pricing, Greeks, historical volatility.

TODO: Implement with actual ORATS API endpoints once documentation is confirmed.
      Reference: https://docs.orats.io
      Auth: API key in query param or header — check docs.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def get_vol_surface(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Fetch volatility surface for a ticker from ORATS.

    TODO: Implement real HTTP call to ORATS endpoint.
          Expected response: IV across strikes and expirations.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        Dict with vol surface data, or None on failure.
    """
    logger.debug("ORATS get_vol_surface called for %s (stub)", ticker)
    # STUB — returns mock data matching expected schema
    return {
        "iv_atm_30d": None,
        "iv_atm_60d": None,
        "hv10": None,
        "hv20": None,
        "hv30": None,
        "hv60": None,
        "source": "orats_stub",
        "_stub": True,
    }


def get_spread_pricing(ticker: str, strategy: str, expiry_days: int,
                       strike_entry: float, strike_target: float) -> Optional[Dict[str, Any]]:
    """
    Fetch theoretical spread pricing and Greeks from ORATS.

    TODO: Implement with ORATS spread pricing endpoint.

    Args:
        ticker:        Stock ticker symbol.
        strategy:      e.g. "bull_put_spread", "bear_call_spread".
        expiry_days:   Target DTE.
        strike_entry:  Entry strike price.
        strike_target: Target/protection strike price.

    Returns:
        Dict with credit/debit, Greeks, max profit/loss. None on failure.
    """
    logger.debug("ORATS get_spread_pricing called for %s (stub)", ticker)
    return {
        "theoretical_credit": None,
        "delta": None,
        "gamma": None,
        "theta": None,
        "vega": None,
        "source": "orats_stub",
        "_stub": True,
    }
