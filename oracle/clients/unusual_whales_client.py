"""
ORACLE — Unusual Whales Client (stub)
Provides: options sweeps, dark pool prints, Congress trades, net flow bias.

TODO: Implement with Unusual Whales API once endpoint docs confirmed.
      Reference: https://unusualwhales.com/api
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def get_flow(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Fetch options flow, sweeps, and dark pool data for a ticker.

    TODO: Implement real HTTP call to Unusual Whales flow endpoint.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        Dict with sweeps_today, dark_pool_prints, net_flow_bias, unusual_activity.
    """
    logger.debug("Unusual Whales get_flow called for %s (stub)", ticker)
    return {
        "sweeps_today": [],
        "dark_pool_prints": [],
        "net_flow_bias": "NEUTRAL",
        "unusual_activity": False,
        "source": "unusual_whales_stub",
        "_stub": True,
    }


def get_congress_trades(ticker: str, days: int = 90) -> List[Dict[str, Any]]:
    """
    Fetch recent Congress trades on a ticker.

    TODO: Implement with Unusual Whales Congress trades endpoint.

    Args:
        ticker: Stock ticker symbol.
        days:   Lookback window in days.

    Returns:
        List of Congress trade dicts.
    """
    logger.debug("Unusual Whales get_congress_trades called for %s (stub)", ticker)
    return []
