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
    hiro = spotgamma_client.get_hiro(ticker) or {}

    gamma = GammaData(
        gex_level=levels.get("gex_level"),
        gamma_flip=levels.get("gamma_flip"),
        call_wall=levels.get("call_wall"),
        put_wall=levels.get("put_wall"),
        net_gex=levels.get("net_gex"),
        hiro_signal=hiro.get("hiro_signal"),
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
