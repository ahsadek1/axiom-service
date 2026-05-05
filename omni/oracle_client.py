"""
oracle_client.py — OMNI Oracle Intelligence Client

Fetches full ORACLE context for a ticker before quad-intelligence synthesis.
OMNI brains require real market data to make informed GO/NO_GO decisions.
Without ORACLE context, brains operate blind and correctly default to NO_GO.

This client is non-blocking: if ORACLE is unavailable, synthesis proceeds
with degraded context rather than failing. Brains are informed of the gap.
"""

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger("omni.oracle_client")

ORACLE_URL    = os.environ.get("ORACLE_URL", "http://localhost:8007")
ORACLE_SECRET = os.environ.get("ORACLE_SECRET", "")
TIMEOUT_S     = 25  # options chain fetch can take 15-20s during market hours


def get_context(ticker: str) -> Optional[dict]:
    """
    Fetch full ORACLE intelligence context for a ticker.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        ORACLE context dict with price, vol, flow, gamma, macro, fundamental,
        historical, news engines. Returns None if ORACLE is unavailable.
    """
    try:
        resp = requests.get(
            f"{ORACLE_URL}/oracle/context/{ticker}",
            headers={"X-Oracle-Secret": ORACLE_SECRET},
            timeout=TIMEOUT_S,
        )
        if resp.status_code == 200:
            data = resp.json()
            logger.info("ORACLE context fetched for %s (flow=%s, gamma=%s)",
                        ticker,
                        data.get("flow_freshness", "?"),
                        data.get("gamma_freshness", "?"))
            return data
        else:
            logger.warning("ORACLE context returned %d for %s", resp.status_code, ticker)
            return None
    except requests.exceptions.Timeout:
        logger.warning("ORACLE context timeout for %s (>%ds)", ticker, TIMEOUT_S)
        return None
    except Exception as e:
        logger.warning("ORACLE context fetch failed for %s: %s", ticker, e)
        return None
