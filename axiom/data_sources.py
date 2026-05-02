"""
data_sources.py — Axiom External Data Clients

Polygon.io (primary), yfinance (fallback), Alpha Vantage (earnings), FRED (VIX).
Every client handles its own errors and returns None on failure — callers decide.
No silent failures: every exception is logged before returning None.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
import yfinance as yf

logger = logging.getLogger("axiom.data_sources")


# ── Polygon.io ────────────────────────────────────────────────────────────────

def polygon_get_snapshot(ticker: str, api_key: str) -> Optional[dict]:
    """
    Fetch a single-ticker snapshot from Polygon.io.

    Returns price, volume, IV rank (if available), and options data.
    Returns None on any error (caller falls back to yfinance).

    Args:
        ticker:  Stock ticker symbol.
        api_key: Polygon.io API key.

    Returns:
        Dict with price, volume, iv_rank, avg_volume, or None on failure.
    """
    try:
        url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
        resp = requests.get(url, params={"apiKey": api_key}, timeout=8)
        resp.raise_for_status()
        data = resp.json()

        ticker_data = data.get("ticker", {})
        day         = ticker_data.get("day", {})
        prev_day    = ticker_data.get("prevDay", {})

        close  = day.get("c") or prev_day.get("c") or 0.0
        volume = day.get("v") or prev_day.get("v") or 0

        return {
            "ticker":     ticker,
            "price":      float(close),
            "volume":     int(volume),
            "iv_rank":    None,   # Polygon requires separate options call
            "source":     "polygon",
        }
    except Exception as e:
        logger.warning("Polygon snapshot failed for %s: %s", ticker, e)
        return None


def polygon_get_iv_rank(ticker: str, api_key: str) -> Optional[float]:
    """
    Fetch IV rank percentile (rip) from ORATS summaries API.

    ORATS `rip` = IV rank percentile (0-100). This is the same source
    Oracle/scorer uses — ensures Tier 2 filter and scorer agree on IVR.

    Previously used Polygon implied_volatility * 100 which is NOT IV rank
    and caused the filter to pass low-IVR tickers that the scorer rejected.

    Args:
        ticker:  Stock ticker symbol.
        api_key: Unused (kept for signature compatibility). Uses ORATS key.

    Returns:
        IV rank percentile as float 0-100, or None if unavailable.
    """
    try:
        ORATS_TOKEN = "4476e955-241a-4540-b114-ebbf1a3a3b87"
        resp = requests.get(
            "https://api.orats.io/datav2/summaries",
            params={"token": ORATS_TOKEN, "ticker": ticker},
            timeout=8,
        )
        resp.raise_for_status()
        rows = resp.json().get("data", [])
        if rows:
            rip = rows[0].get("rip")
            if rip is not None:
                return float(rip)
        return None
    except Exception as e:
        logger.debug("ORATS IV rank unavailable for %s: %s", ticker, e)
        return None


def polygon_batch_snapshots(
    tickers: list[str],
    api_key: str,
) -> dict[str, Optional[dict]]:
    """
    Fetch snapshots for multiple tickers from Polygon.io.

    Args:
        tickers: List of ticker symbols.
        api_key: Polygon.io API key.

    Returns:
        Dict mapping ticker to snapshot dict or None.
    """
    try:
        tickers_str = ",".join(tickers)
        url = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
        resp = requests.get(
            url,
            params={"tickers": tickers_str, "apiKey": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        results = {}
        for item in data.get("tickers", []):
            t    = item.get("ticker", "")
            day  = item.get("day", {})
            prev = item.get("prevDay", {})
            results[t] = {
                "ticker":  t,
                "price":   float(day.get("c") or prev.get("c") or 0),
                "volume":  int(day.get("v") or prev.get("v") or 0),
                "iv_rank": None,
                "source":  "polygon",
            }
        return results

    except Exception as e:
        logger.warning("Polygon batch snapshot failed: %s — falling back to yfinance", e)
        return {}


# ── yfinance Fallback ─────────────────────────────────────────────────────────

def yfinance_get_data(ticker: str) -> Optional[dict]:
    """
    Fetch stock data from yfinance as fallback when Polygon is unavailable.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        Dict with price, volume, iv_rank (None — yfinance doesn't provide), or None on failure.
    """
    try:
        info = yf.Ticker(ticker).fast_info
        price  = getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", None)
        volume = getattr(info, "three_month_average_volume", None) or 0
        return {
            "ticker":  ticker,
            "price":   float(price or 0),
            "volume":  int(volume),
            "iv_rank": None,
            "source":  "yfinance",
        }
    except Exception as e:
        logger.warning("yfinance failed for %s: %s", ticker, e)
        return None


def yfinance_get_rsi(ticker: str, period: int = 14) -> Optional[float]:
    """
    Calculate RSI for a ticker using yfinance historical data.

    Args:
        ticker: Stock ticker symbol.
        period: RSI period (default 14).

    Returns:
        RSI value 0-100, or None if calculation fails.
    """
    try:
        hist = yf.Ticker(ticker).history(period="3mo")
        if hist.empty or len(hist) < period + 1:
            return None

        delta  = hist["Close"].diff()
        gain   = delta.clip(lower=0).rolling(window=period).mean()
        loss   = (-delta.clip(upper=0)).rolling(window=period).mean()
        rs     = gain / loss.replace(0, float("inf"))
        rsi    = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1])
    except Exception as e:
        logger.warning("RSI calculation failed for %s: %s", ticker, e)
        return None


def yfinance_get_avg_volume(ticker: str) -> Optional[int]:
    """
    Get 3-month average daily volume from yfinance.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        Average daily volume as integer, or None on failure.
    """
    try:
        info = yf.Ticker(ticker).fast_info
        vol = getattr(info, "three_month_average_volume", None)
        return int(vol) if vol else None
    except Exception as e:
        logger.warning("Average volume failed for %s: %s", ticker, e)
        return None


# ── Alpha Vantage — Earnings Calendar ────────────────────────────────────────

def get_earnings_date(ticker: str, api_key: str) -> Optional[datetime]:
    """
    Fetch next earnings date for a ticker from Alpha Vantage.

    Args:
        ticker:  Stock ticker symbol.
        api_key: Alpha Vantage API key.

    Returns:
        Next earnings datetime (UTC), or None if unavailable or API down.
    """
    try:
        url = "https://www.alphavantage.co/query"
        resp = requests.get(
            url,
            params={
                "function": "EARNINGS_CALENDAR",
                "symbol":   ticker,
                "horizon":  "3month",
                "apikey":   api_key,
            },
            timeout=10,
        )
        resp.raise_for_status()

        # Alpha Vantage returns CSV for this endpoint
        lines = resp.text.strip().split("\n")
        if len(lines) < 2:
            return None

        # Header: symbol,name,reportDate,fiscalDateEnding,estimate,currency
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) >= 3 and parts[0].strip().upper() == ticker.upper():
                date_str = parts[2].strip()
                return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        return None
    except Exception as e:
        logger.warning("Alpha Vantage earnings fetch failed for %s: %s", ticker, e)
        return None


def days_to_earnings(ticker: str, api_key: str) -> Optional[int]:
    """
    Return number of days until next earnings for a ticker.

    Args:
        ticker:  Stock ticker symbol.
        api_key: Alpha Vantage API key.

    Returns:
        Days until earnings (0 = today), or None if unavailable.
    """
    earnings_dt = get_earnings_date(ticker, api_key)
    if earnings_dt is None:
        return None
    now  = datetime.now(timezone.utc)
    diff = (earnings_dt - now).days
    return max(0, diff)


# ── FRED — VIX ────────────────────────────────────────────────────────────────

def get_vix(api_key: str) -> Optional[float]:
    """
    Fetch the current VIX level from FRED (St. Louis Fed).

    Uses VIXCLS series. Returns the most recent observation.
    Falls back to None on any error — caller uses last known VIX + safety margin.

    Args:
        api_key: FRED API key.

    Returns:
        Current VIX as float, or None on failure.
    """
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        resp = requests.get(
            url,
            params={
                "series_id":    "VIXCLS",
                "api_key":      api_key,
                "file_type":    "json",
                "sort_order":   "desc",
                "limit":        5,
                "observation_start": (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
            },
            timeout=10,
        )
        resp.raise_for_status()
        observations = resp.json().get("observations", [])

        for obs in observations:
            val = obs.get("value", ".")
            if val != ".":
                return float(val)

        return None
    except Exception as e:
        logger.warning("FRED VIX fetch failed: %s", e)
        return None


def get_vix_with_fallback(api_key: str, last_known_vix: Optional[float]) -> tuple[float, bool]:
    """
    Get VIX with safety fallback if FRED is unavailable.

    Args:
        api_key:         FRED API key.
        last_known_vix:  Last successfully fetched VIX value.

    Returns:
        Tuple of (vix_value, is_estimated).
        is_estimated=True means FRED was unavailable and last_known + 2 was used.
    """
    vix = get_vix(api_key)
    if vix is not None:
        return vix, False

    if last_known_vix is not None:
        estimated = last_known_vix + 2.0
        logger.warning("FRED unavailable — using estimated VIX %.1f (last known %.1f + 2)", estimated, last_known_vix)
        return estimated, True

    # No VIX at all — use conservative default
    logger.error("FRED unavailable and no last known VIX — defaulting to 20.0 (ELEVATED)")
    return 20.0, True
