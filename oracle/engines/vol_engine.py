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
from clients import market_chameleon_client, orats_client
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

    # Fetch from both sources (stubs return None values currently)
    mc_data = market_chameleon_client.get_iv_rank(ticker) or {}
    orats_data = orats_client.get_vol_surface(ticker) or {}

    iv_rank = mc_data.get("iv_rank")
    iv_pct = mc_data.get("iv_percentile")
    hv30_mc = mc_data.get("hv30")
    hv30_orats = orats_data.get("hv30")
    hv30 = hv30_mc or hv30_orats

    iv_hv = mc_data.get("iv_hv_spread")

    vol = VolData(
        iv_rank=iv_rank,
        iv_percentile=iv_pct,
        hv30=hv30,
        hv60=orats_data.get("hv60") or orats_data.get("hv20"),
        iv_hv_spread=iv_hv,
    )

    # Cipher Pass 3 P3-8: AND logic meant one-stub + one-real was cached as LIVE.
    # iv_rank=None / iv_percentile=None silently mixed into LIVE-tagged packets.
    # Fix: OR logic — any stub source → DEGRADED freshness + shorter TTL.
    both_stub = mc_data.get("_stub") and orats_data.get("_stub")
    any_stub  = mc_data.get("_stub") or orats_data.get("_stub")

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
