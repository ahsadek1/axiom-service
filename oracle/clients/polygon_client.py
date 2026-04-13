"""
ORACLE — Polygon.io Client
Primary source for real-time price snapshots and historical OHLCV.
Fallback: yfinance (see price_engine.py).
"""

import logging
import time
from typing import Any, Dict, Optional

import requests

import config
import rate_limiter

logger = logging.getLogger(__name__)

SNAPSHOT_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
TIMEOUT = (5, 15)  # (connect, read) seconds


def get_snapshot(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Fetch real-time snapshot for a single ticker from Polygon.

    Args:
        ticker: Stock ticker symbol (e.g. "NVDA").

    Returns:
        Dict with price, volume, day range data, or None on failure.
    """
    if not rate_limiter.acquire("polygon"):
        logger.warning("Polygon rate limit timeout for %s", ticker)
        return None

    url = SNAPSHOT_URL.format(ticker=ticker.upper())
    params = {"apiKey": config.POLYGON_API_KEY}
    start = time.monotonic()

    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
        latency_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code == 200:
            body = resp.json()
            ticker_data = body.get("ticker", {})
            day = ticker_data.get("day", {})
            prev_day = ticker_data.get("prevDay", {})
            last_quote = ticker_data.get("lastQuote", {})
            last_trade = ticker_data.get("lastTrade", {})

            return {
                "last": last_trade.get("p") or ticker_data.get("lastTrade", {}).get("p"),
                "bid": last_quote.get("P"),
                "ask": last_quote.get("p"),
                "day_high": day.get("h"),
                "day_low": day.get("l"),
                "day_open": day.get("o"),
                "day_close": day.get("c"),
                "volume": day.get("v"),
                "vwap": day.get("vw"),
                "prev_close": prev_day.get("c"),
                "latency_ms": latency_ms,
            }
        else:
            logger.error("Polygon snapshot error %s for %s: %s",
                         resp.status_code, ticker, resp.text[:200])
            return None

    except requests.Timeout:
        logger.error("Polygon snapshot timeout for %s", ticker)
        return None
    except requests.ConnectionError as e:
        logger.error("Polygon connection error for %s: %s", ticker, e)
        return None
    except (ValueError, KeyError) as e:
        logger.error("Polygon response parse error for %s: %s", ticker, e)
        return None
