"""
ORACLE — Engine 4: Gamma Engine
Source: SpotGamma (GEX levels, Gamma Flip, Call/Put Walls, HIRO)
Cache TTL: 15 min for levels, 5 min for HIRO
"""

import logging
import time
from typing import Optional

import cache
import config
from clients import spotgamma_client
from models import GammaData

logger = logging.getLogger(__name__)

ENGINE = "gamma"


def fetch(ticker: str, card_type: str = "full") -> tuple[Optional[GammaData], str]:
    """
    Fetch gamma structure intelligence for a ticker.

    Args:
        ticker:    Stock ticker symbol.
        card_type: "preliminary" or "full".

    Returns:
        Tuple of (GammaData or None, freshness string).
    """
    start = time.monotonic()

    cached = cache.get(ticker, ENGINE, card_type)
    if cached is not None:
        return GammaData(**cached), config.FRESHNESS_LIVE

    levels = spotgamma_client.get_gamma_levels(ticker) or {}
    hiro = spotgamma_client.get_hiro_flow(ticker) or {}

    # net_gex from spotgamma_client is a raw float (e.g. 189403.41).
    # GammaData.net_gex expects a string label: POSITIVE | NEGATIVE | NEUTRAL.
    raw_net_gex = levels.get("net_gex")
    if raw_net_gex is None:
        net_gex_label = None
    elif isinstance(raw_net_gex, str):
        net_gex_label = raw_net_gex  # already labelled
    elif raw_net_gex > 0:
        net_gex_label = "POSITIVE"
    elif raw_net_gex < 0:
        net_gex_label = "NEGATIVE"
    else:
        net_gex_label = "NEUTRAL"

    # GENESIS-FIX-GAMMA-PCT-001 2026-05-01: Compute pct_to_call/put_wall from
    # absolute wall prices + spot. Scorer D6 needs percentage distance, not
    # absolute price — without this, wall_pts=0 for all tickers regardless of
    # actual distance, killing D6 signal discrimination.
    spot_price = levels.get("spot_price")
    call_wall_abs = levels.get("call_wall")
    put_wall_abs = levels.get("put_wall")

    pct_to_call_wall: Optional[float] = None
    pct_to_put_wall: Optional[float] = None
    if spot_price and spot_price > 0:
        if call_wall_abs:
            pct_to_call_wall = round(abs(call_wall_abs - spot_price) / spot_price * 100, 2)
        if put_wall_abs:
            pct_to_put_wall = round(abs(spot_price - put_wall_abs) / spot_price * 100, 2)

    gamma = GammaData(
        gex_level=levels.get("gex_level"),
        gamma_flip=levels.get("gamma_flip"),
        call_wall=call_wall_abs,
        put_wall=put_wall_abs,
        net_gex=net_gex_label,
        hiro_signal=hiro.get("hiro_signal"),
        pct_to_call_wall=pct_to_call_wall,
        pct_to_put_wall=pct_to_put_wall,
    )

    # Cipher Pass 3 P3-8: OR logic for partial stub detection
    both_stub = levels.get("_stub") and hiro.get("_stub")
    any_stub  = levels.get("_stub") or hiro.get("_stub")

    if both_stub:
        freshness = config.FRESHNESS_UNAVAILABLE
    elif any_stub:
        freshness = config.FRESHNESS_DEGRADED
        cache.set(ticker, ENGINE, gamma.model_dump(), config.GAMMA_TTL_LEVELS // 2, card_type)
    else:
        freshness = config.FRESHNESS_LIVE
        cache.set(ticker, ENGINE, gamma.model_dump(), config.GAMMA_TTL_LEVELS, card_type)

    cache.log_api_call(ENGINE, "spotgamma", ticker,
                       int((time.monotonic() - start) * 1000), True)
    return gamma, freshness
