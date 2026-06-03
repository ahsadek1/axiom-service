"""
ORACLE — Engine 2: Vol Engine
Sources: ORATS (vol surface + spread math) + Market Chameleon (IV rank + historical context)
Cache TTL: 15 minutes
"""

import logging
import time
from typing import Optional

import cache
import config
from clients import orats_client
from models import VolData

logger = logging.getLogger(__name__)

ENGINE = "vol"


def fetch(ticker: str, card_type: str = "full") -> tuple[Optional[VolData], str]:
    """
    Fetch volatility intelligence for a ticker.

    Args:
        ticker:    Stock ticker symbol.
        card_type: "preliminary" or "full".

    Returns:
        Tuple of (VolData or None, freshness string).
    """
    start = time.monotonic()

    cached = cache.get(ticker, ENGINE, card_type)
    if cached is not None:
        cache.log_api_call(ENGINE, "market_chameleon", ticker,
                           int((time.monotonic() - start) * 1000),
                           True, cache_hit=True)
        return VolData(**cached), config.FRESHNESS_LIVE

    # ORATS is primary source for all vol data (rip = IV rank percentile, live API).
    # MC get_iv_rank() intentionally returns nulls — it is a passthrough to ORATS.
    orats_data = orats_client.get_vol_surface(ticker) or {}

    iv_rank = orats_data.get("iv_rank")        # rip field (0-100)
    iv_pct  = orats_data.get("iv_percentile")  # same as rip
    hv30    = orats_data.get("hv30")
    hv60    = orats_data.get("hv60")
    iv_hv   = orats_data.get("iv_hv_spread")

    vol = VolData(
        iv_rank=iv_rank,
        iv_percentile=iv_pct,
        hv30=hv30,
        hv60=hv60,
        iv_hv_spread=iv_hv,
    )

    # ORATS is the sole source. Stub/unavailable check from ORATS response only.
    both_stub = orats_data.get("_stub", True)
    any_stub  = orats_data.get("_stub", True)

    if both_stub:
        freshness = config.FRESHNESS_UNAVAILABLE
    elif any_stub:
        freshness = config.FRESHNESS_DEGRADED
        cache.set(ticker, ENGINE, vol.model_dump(), config.VOL_TTL // 2, card_type)
    else:
        freshness = config.FRESHNESS_LIVE
        cache.set(ticker, ENGINE, vol.model_dump(), config.VOL_TTL, card_type)

    cache.log_api_call(ENGINE, "market_chameleon", ticker,
                       int((time.monotonic() - start) * 1000), True)
    return vol, freshness
