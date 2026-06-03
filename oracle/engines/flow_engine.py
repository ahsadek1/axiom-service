"""
ORACLE — Engine 3: Flow Engine
Sources: Unusual Whales (sweeps, dark pool) + Market Chameleon (put/call ratios)
Cache TTL: 5 minutes (market hours)
"""

import logging
import time
from typing import Optional

import cache
import config
from clients import market_chameleon_client, unusual_whales_client
from models import FlowData

logger = logging.getLogger(__name__)

ENGINE = "flow"


def fetch(ticker: str, card_type: str = "full") -> tuple[Optional[FlowData], str]:
    """
    Fetch options flow intelligence for a ticker.

    Args:
        ticker:    Stock ticker symbol.
        card_type: "preliminary" or "full".

    Returns:
        Tuple of (FlowData or None, freshness string).
    """
    start = time.monotonic()

    cached = cache.get(ticker, ENGINE, card_type)
    if cached is not None:
        return FlowData(**cached), config.FRESHNESS_LIVE

    uw_data = unusual_whales_client.get_flow(ticker) or {}
    mc_flow = market_chameleon_client.get_put_call_ratio(ticker) or {}

    # MC returns oi_put_call_ratio / volume_put_call_ratio — prefer volume PCR
    mc_pcr = mc_flow.get("volume_put_call_ratio") or mc_flow.get("oi_put_call_ratio")
    # Volume vs avg: approximate from call+put volume vs typical (~500k/day)
    total_vol = (mc_flow.get("total_call_volume") or 0) + (mc_flow.get("total_put_volume") or 0)
    vol_vs_avg = round(total_vol / 500000, 2) if total_vol > 0 else None

    flow = FlowData(
        sweeps_today=uw_data.get("sweeps_today", []),
        dark_pool_prints=uw_data.get("dark_pool_prints", []),
        net_flow_bias=uw_data.get("net_flow_bias", "NEUTRAL"),
        unusual_activity=uw_data.get("unusual_activity", False),
        put_call_ratio=mc_pcr,
        options_volume_vs_avg=vol_vs_avg,
    )

    # Cipher Pass 3 P3-8: OR logic for partial stub detection
    both_stub = uw_data.get("_stub") and mc_flow.get("_stub")
    any_stub  = uw_data.get("_stub") or mc_flow.get("_stub")

    if both_stub:
        freshness = config.FRESHNESS_UNAVAILABLE
    elif any_stub:
        freshness = config.FRESHNESS_DEGRADED
        cache.set(ticker, ENGINE, flow.model_dump(), config.FLOW_TTL_MARKET_HOURS // 2, card_type)
    else:
        freshness = config.FRESHNESS_LIVE
        cache.set(ticker, ENGINE, flow.model_dump(), config.FLOW_TTL_MARKET_HOURS, card_type)

    cache.log_api_call(ENGINE, "unusual_whales", ticker,
                       int((time.monotonic() - start) * 1000), True)
    return flow, freshness
