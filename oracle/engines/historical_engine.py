"""
ORACLE — Engine 7: Historical Engine
Source: AILS (internal service)
Non-blocking: if AILS is down, returns (None, UNAVAILABLE) without crashing.
Cache TTL: 24 hours
"""

import logging
import time
from typing import Optional

import cache
import config
from clients import ails_client
from models import HistoricalData

logger = logging.getLogger(__name__)

ENGINE = "historical"


def fetch(ticker: str, regime: str, strategy: str = "any",
          card_type: str = "full") -> tuple[Optional[HistoricalData], str]:
    """
    Fetch historical performance data for a ticker/regime/strategy from AILS.
    Non-blocking: AILS failures return (None, UNAVAILABLE).

    Args:
        ticker:    Stock ticker symbol.
        regime:    Current market regime.
        strategy:  Strategy type.
        card_type: "preliminary" or "full".

    Returns:
        Tuple of (HistoricalData or None, freshness string).
    """
    start = time.monotonic()

    cache_key = f"{ticker}:{regime}:{strategy}"
    cached = cache.get(cache_key, ENGINE, card_type)
    if cached is not None:
        return HistoricalData(**cached), config.FRESHNESS_LIVE

    raw = ails_client.get_historical(ticker, regime, strategy)
    if raw is None:
        # AILS unavailable — non-blocking, return None
        cache.log_api_call(ENGINE, "ails", ticker,
                           int((time.monotonic() - start) * 1000),
                           False, "AILS unavailable")
        return None, config.FRESHNESS_UNAVAILABLE

    historical = HistoricalData(
        instances=raw.get("instances", 0),
        win_rate=raw.get("win_rate"),
        avg_win_pct=raw.get("avg_win_pct"),
        avg_loss_pct=raw.get("avg_loss_pct"),
        expected_value=raw.get("expected_value"),
        most_common_failure=raw.get("most_common_failure"),
        regime_match=regime,
        data_confidence=_confidence(raw.get("instances", 0)),
    )

    cache.set(cache_key, ENGINE, historical.model_dump(),
              config.HISTORICAL_TTL, card_type)
    cache.log_api_call(ENGINE, "ails", ticker,
                       int((time.monotonic() - start) * 1000), True)
    return historical, config.FRESHNESS_LIVE


def _confidence(instances: int) -> str:
    """Determine data confidence level based on sample size."""
    if instances >= 30:
        return "HIGH"
    elif instances >= 10:
        return "MEDIUM"
    return "LOW"
