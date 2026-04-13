"""
ORACLE — FRED Client (Federal Reserve Economic Data)
Provides: VIX, Fed Funds Rate, Yield Curve (2Y/10Y), HY Credit Spread.
Free API — no rate limit concerns at our usage level.
"""

import logging
import time
from typing import Any, Dict, Optional

import requests

import config
import rate_limiter

logger = logging.getLogger(__name__)

BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
TIMEOUT = (5, 20)


def _fetch_series(series_id: str) -> Optional[float]:
    """
    Fetch the most recent observation for a FRED series.

    Args:
        series_id: FRED series identifier (e.g. "VIXCLS").

    Returns:
        Most recent numeric value, or None on failure.
    """
    if not rate_limiter.acquire("fred"):
        logger.warning("FRED rate limit timeout for %s", series_id)
        return None

    params = {
        "series_id": series_id,
        "api_key": config.FRED_API_KEY,
        "file_type": "json",
        "limit": 5,
        "sort_order": "desc",
    }

    try:
        resp = requests.get(BASE_URL, params=params, timeout=TIMEOUT)
        if resp.status_code == 200:
            observations = resp.json().get("observations", [])
            for obs in observations:
                val = obs.get("value", ".")
                if val != ".":
                    return float(val)
            return None
        else:
            logger.error("FRED error %s for %s: %s",
                         resp.status_code, series_id, resp.text[:200])
            return None
    except requests.Timeout:
        logger.error("FRED timeout for series %s", series_id)
        return None
    except requests.ConnectionError as e:
        logger.error("FRED connection error for %s: %s", series_id, e)
        return None
    except (ValueError, KeyError) as e:
        logger.error("FRED parse error for %s: %s", series_id, e)
        return None


def get_macro_data() -> Dict[str, Any]:
    """
    Fetch all macro data points needed for regime classification.

    Returns:
        Dict with vix, fed_funds_rate, yield_2y, yield_10y, hy_spread.
        Values may be None if individual series fail.
    """
    start = time.monotonic()

    vix = _fetch_series(config.FRED_SERIES["VIX"])
    ffr = _fetch_series(config.FRED_SERIES["FFR"])
    y10 = _fetch_series(config.FRED_SERIES["YIELD_10Y"])
    y2 = _fetch_series(config.FRED_SERIES["YIELD_2Y"])
    hy = _fetch_series(config.FRED_SERIES["HY_SPREAD"])

    yield_spread = None
    if y10 is not None and y2 is not None:
        yield_spread = round((y10 - y2) * 100, 1)  # in basis points

    latency_ms = int((time.monotonic() - start) * 1000)
    logger.debug("FRED macro data fetched in %dms", latency_ms)

    return {
        "vix": vix,
        "fed_funds_rate": ffr,
        "yield_2y": y2,
        "yield_10y": y10,
        "yield_spread_bps": yield_spread,
        "hy_spread_bps": hy,
        "latency_ms": latency_ms,
    }
