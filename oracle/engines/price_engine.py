"""
ORACLE — Engine 1: Price Engine
Primary: Polygon.io  |  Fallback: yfinance
Cache TTL: 30s (market hours), 86400s (after close)
"""

import logging
import time
from typing import Any, Dict, Optional

import cache
import config
from clients import polygon_client
from models import PriceData

logger = logging.getLogger(__name__)

ENGINE = "price"
PLATFORM_PRIMARY = "polygon"
PLATFORM_FALLBACK = "yfinance"


def fetch(ticker: str, card_type: str = "full") -> tuple[Optional[PriceData], str]:
    """
    Fetch price data for a ticker. Checks cache first, then live.

    Args:
        ticker:    Stock ticker symbol.
        card_type: "preliminary" or "full".

    Returns:
        Tuple of (PriceData or None, freshness string).
    """
    start = time.monotonic()

    # Cache check
    cached = cache.get(ticker, ENGINE, card_type)
    if cached is not None:
        cache.log_api_call(ENGINE, PLATFORM_PRIMARY, ticker,
                           int((time.monotonic() - start) * 1000),
                           True, cache_hit=True)
        return PriceData(**cached), config.FRESHNESS_LIVE

    # Live fetch — Polygon primary
    raw = polygon_client.get_snapshot(ticker)
    if raw is not None:
        data = _parse_polygon(raw, ticker=ticker)
        cache.set(ticker, ENGINE, data.model_dump(), config.PRICE_TTL_MARKET_HOURS, card_type)
        cache.log_api_call(ENGINE, PLATFORM_PRIMARY, ticker,
                           raw.get("latency_ms", 0), True)
        return data, config.FRESHNESS_LIVE

    # Fallback — yfinance
    cache.log_failover(ENGINE, PLATFORM_PRIMARY, PLATFORM_FALLBACK)
    raw_yf = _fetch_yfinance(ticker)
    if raw_yf is not None:
        cache.set(ticker, ENGINE, raw_yf.model_dump(),
                  config.PRICE_TTL_MARKET_HOURS, card_type)
        cache.log_api_call(ENGINE, PLATFORM_FALLBACK, ticker,
                           int((time.monotonic() - start) * 1000), True)
        return raw_yf, config.FRESHNESS_LIVE

    # Both failed — try stale cache
    stale = _get_stale(ticker, card_type)
    if stale:
        return stale, config.FRESHNESS_STALE

    cache.log_api_call(ENGINE, PLATFORM_PRIMARY, ticker,
                       int((time.monotonic() - start) * 1000),
                       False, "Both Polygon and yfinance failed")
    return None, config.FRESHNESS_UNAVAILABLE


def _parse_polygon(raw: Dict[str, Any], ticker: str = "") -> PriceData:
    """Parse Polygon snapshot + compute technicals from daily bars."""
    # Fetch RSI / MA / volume ratio from daily bars (cached by Oracle TTL)
    tech = {}
    if ticker:
        try:
            tech = polygon_client.get_technicals(ticker) or {}
        except Exception as e:
            logger.warning("Technicals fetch failed for %s: %s", ticker, e)

    return PriceData(
        last=raw.get("last"),
        bid=raw.get("bid"),
        ask=raw.get("ask"),
        day_high=raw.get("day_high"),
        day_low=raw.get("day_low"),
        volume=raw.get("volume"),
        avg_volume_30d=tech.get("avg_volume_30d"),
        volume_ratio=tech.get("volume_ratio"),
        rsi_14=tech.get("rsi_14"),
        price_vs_20d_ma=tech.get("price_vs_20d_ma"),
        price_vs_50d_ma=tech.get("price_vs_50d_ma"),
    )


def _fetch_yfinance(ticker: str) -> Optional[PriceData]:
    """Fetch price data from yfinance as fallback."""
    try:
        import yfinance as yf  # type: ignore
        t = yf.Ticker(ticker)
        info = t.fast_info
        return PriceData(
            last=getattr(info, "last_price", None),
            day_high=getattr(info, "day_high", None),
            day_low=getattr(info, "day_low", None),
            volume=getattr(info, "three_month_average_volume", None),
        )
    except Exception as e:
        logger.error("yfinance fallback failed for %s: %s", ticker, e)
        return None


def _get_stale(ticker: str, card_type: str) -> Optional[PriceData]:
    """Return stale cached data if available (beyond TTL but still present in L2)."""
    import sqlite3
    try:
        conn = sqlite3.connect(config.ORACLE_DB_PATH)
        import json
        row = conn.execute(
            "SELECT data_json FROM cache_entries WHERE ticker=? AND engine=? AND card_type=?",
            (ticker.upper(), ENGINE, card_type)
        ).fetchone()
        conn.close()
        if row:
            return PriceData(**json.loads(row[0]))
    except Exception as e:
        logger.error("Stale cache read error for %s: %s", ticker, e)
    return None
