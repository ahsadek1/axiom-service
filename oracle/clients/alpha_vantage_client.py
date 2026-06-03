"""
ORACLE — Alpha Vantage Client
Provides: earnings calendar, earnings surprise history.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests

import config
import rate_limiter

logger = logging.getLogger(__name__)

BASE_URL = "https://www.alphavantage.co/query"
TIMEOUT = (5, 20)


def get_earnings(ticker: str) -> Dict[str, Any]:
    """
    Fetch earnings history and upcoming earnings date for a ticker.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        Dict with next_earnings_date and last 8 quarterly surprises.
    """
    if not rate_limiter.acquire("alpha_vantage"):
        logger.warning("Alpha Vantage rate limit timeout for %s", ticker)
        return {}

    params = {
        "function": "EARNINGS",
        "symbol": ticker.upper(),
        "apikey": config.ALPHA_VANTAGE_KEY,
    }
    start = time.monotonic()

    try:
        resp = requests.get(BASE_URL, params=params, timeout=TIMEOUT)
        latency_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            logger.error("Alpha Vantage error %s for %s", resp.status_code, ticker)
            return {}

        body = resp.json()
        if "Note" in body or "Information" in body:
            # Rate limit hit
            logger.warning("Alpha Vantage rate limit message for %s", ticker)
            return {}

        quarterly = body.get("quarterlyEarnings", [])
        last_8: List[Dict[str, Any]] = []
        for q in quarterly[:8]:
            reported = q.get("reportedDate", "")
            estimated = q.get("estimatedEPS")
            actual = q.get("reportedEPS")
            surprise_pct = q.get("surprisePercentage")
            last_8.append({
                "date": reported,
                "estimated_eps": _safe_float(estimated),
                "actual_eps": _safe_float(actual),
                "surprise_pct": _safe_float(surprise_pct),
                "beat": _safe_float(surprise_pct) is not None
                        and _safe_float(surprise_pct) > 0,
            })

        return {
            "quarterly_history": last_8,
            "latency_ms": latency_ms,
        }

    except requests.Timeout:
        logger.error("Alpha Vantage timeout for %s", ticker)
        return {}
    except requests.ConnectionError as e:
        logger.error("Alpha Vantage connection error for %s: %s", ticker, e)
        return {}
    except (ValueError, KeyError) as e:
        logger.error("Alpha Vantage parse error for %s: %s", ticker, e)
        return {}


def _safe_float(val: Any) -> Optional[float]:
    """Convert a value to float, returning None on failure."""
    try:
        return float(val) if val not in (None, "None", "N/A", "") else None
    except (TypeError, ValueError):
        return None
