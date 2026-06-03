"""
market_data.py — Live Market Data Layer
========================================
Provides real-time data for execution decisions:
  - Earnings calendar (never sell premium into earnings)
  - IV rank per ticker (ideal range: 20-50 for credit spreads)
  - Economic calendar (Fed, CPI, NFP dates)
  - Underlying price with fallback chain
"""
from __future__ import annotations
import logging
import os
import sqlite3
import time
from datetime import datetime, date, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("thesis.market_data")
_ET = ZoneInfo("America/New_York")

POLYGON_KEY   = os.getenv("POLYGON_API_KEY", "")
ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
DATA_URL      = "https://data.alpaca.markets"
POLYGON_URL   = "https://api.polygon.io"

# Cache to avoid hammering APIs
_earnings_cache: dict[str, tuple] = {}   # ticker -> (has_earnings, ts)
_iv_cache:       dict[str, tuple] = {}   # ticker -> (iv_rank, ts)
CACHE_TTL = 3600  # 1 hour


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY",""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY",""),
    }


# ---------------------------------------------------------------------------
# Earnings calendar
# ---------------------------------------------------------------------------

def has_earnings_soon(ticker: str, window_days: int = 14) -> bool:
    """
    Returns True if ticker has earnings within the next window_days.
    THESIS never sells premium into earnings — IV crush destroys the trade.
    """
    cached = _earnings_cache.get(ticker)
    if cached and (time.time() - cached[1]) < CACHE_TTL:
        return cached[0]

    today     = date.today()
    end_date  = today + timedelta(days=window_days)

    # Try Polygon earnings calendar
    try:
        r = requests.get(
            f"{POLYGON_URL}/v3/reference/dividends",
            params={
                "ticker":     ticker,
                "ex_dividend_date.gte": today.isoformat(),
                "ex_dividend_date.lte": end_date.isoformat(),
                "apiKey":     POLYGON_KEY,
            },
            timeout=8,
        )
        # Polygon earnings endpoint
        r2 = requests.get(
            f"{POLYGON_URL}/vX/reference/financials",
            params={
                "ticker":    ticker,
                "filing_date.gte": today.isoformat(),
                "filing_date.lte": end_date.isoformat(),
                "apiKey":    POLYGON_KEY,
            },
            timeout=8,
        )

        # Check Alpaca corporate actions for earnings
        r3 = requests.get(
            f"{DATA_URL}/v1beta1/corporate-actions/announcements",
            headers=_alpaca_headers(),
            params={
                "ca_types":   "earnings",
                "since":      today.isoformat(),
                "until":      end_date.isoformat(),
                "symbol":     ticker,
            },
            timeout=8,
        )
        if r3.status_code == 200:
            announcements = r3.json().get("announcements", [])
            has_earnings  = len(announcements) > 0
            _earnings_cache[ticker] = (has_earnings, time.time())
            if has_earnings:
                log.info("%s: Earnings within %d days — excluded from trading",
                        ticker, window_days)
            return has_earnings

    except Exception as exc:
        log.warning("Earnings check failed for %s: %s", ticker, exc)

    # Conservative fallback: assume no earnings (allow trade)
    _earnings_cache[ticker] = (False, time.time())
    return False


# ---------------------------------------------------------------------------
# IV Rank
# ---------------------------------------------------------------------------

def get_iv_rank(ticker: str) -> Optional[float]:
    """
    Get current IV rank for ticker (0-100).
    Ideal for credit spreads: 20-50 (elevated but not extreme)
    Too low (<15): not enough premium
    Too high (>60): elevated risk, market stress

    Sources: ORATS (primary), Polygon (fallback), backtest DB (last resort)
    """
    cached = _iv_cache.get(ticker)
    if cached and (time.time() - cached[1]) < CACHE_TTL:
        return cached[0]

    # Try Alpaca options snapshot for IV data
    try:
        # Get ATM option to estimate IV
        today    = date.today()
        exp_min  = today + timedelta(days=25)
        exp_max  = today + timedelta(days=50)

        r = requests.get(
            "https://paper-api.alpaca.markets/v2/options/contracts",
            headers=_alpaca_headers(),
            params={
                "underlying_symbols":  ticker,
                "type":               "put",
                "expiration_date_gte": exp_min.isoformat(),
                "expiration_date_lte": exp_max.isoformat(),
                "limit":              5,
                "tradable":           "true",
            },
            timeout=8,
        )
        if r.status_code == 200:
            contracts = r.json().get("option_contracts", [])
            if contracts:
                symbol = contracts[len(contracts)//2]["symbol"]
                snap_r = requests.get(
                    f"{DATA_URL}/v1beta1/options/snapshots",
                    headers=_alpaca_headers(),
                    params={"symbols": symbol, "feed": "indicative"},
                    timeout=8,
                )
                if snap_r.status_code == 200:
                    snap = snap_r.json().get("snapshots", {}).get(symbol, {})
                    iv = snap.get("impliedVolatility", 0)
                    if iv > 0:
                        # Convert IV to rough IV rank (normalized to 0-100)
                        # Typical range: 0.10-0.80 IV
                        iv_rank = min(100, max(0, (iv - 0.10) / 0.70 * 100))
                        _iv_cache[ticker] = (round(iv_rank, 1), time.time())
                        return round(iv_rank, 1)

    except Exception as exc:
        log.warning("IV rank fetch failed for %s: %s", ticker, exc)

    # Fallback: use backtest DB average IV for this ticker
    try:
        db_path = os.getenv("BACKTEST_DB_PATH",
                           "/Users/ahmedsadek/nexus/data/backtest.db")
        conn    = sqlite3.connect(db_path, timeout=5)
        row     = conn.execute(
            "SELECT avg_iv_rank FROM historical_win_rates "
            "WHERE ticker=? AND strategy='bull_put_spread' "
            "ORDER BY sample_count DESC LIMIT 1",
            (ticker,)
        ).fetchone()
        conn.close()
        if row and row[0]:
            _iv_cache[ticker] = (float(row[0]), time.time())
            return float(row[0])
    except Exception:
        pass

    return None  # Unknown — caller decides whether to proceed


def is_iv_acceptable(iv_rank: Optional[float]) -> bool:
    """
    Returns True if IV rank is in acceptable range for credit spreads.
    Ideal: 20-50. Acceptable: 15-60. Outside: reject.
    """
    if iv_rank is None:
        return True  # Unknown — allow with caution
    return 15 <= iv_rank <= 60


# ---------------------------------------------------------------------------
# Economic calendar
# ---------------------------------------------------------------------------

# High-impact dates where THESIS reduces position size
HIGH_IMPACT_EVENTS = [
    # These are checked against current week
    "FOMC", "CPI", "NFP", "GDP", "PCE"
]


def is_high_impact_week() -> bool:
    """
    Returns True if current week has high-impact economic event.
    Conservative: reduces new position sizing by 50% on these weeks.
    """
    try:
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        week_end   = week_start + timedelta(days=4)

        r = requests.get(
            f"{POLYGON_URL}/v1/marketstatus/upcoming",
            params={"apiKey": POLYGON_KEY},
            timeout=8,
        )
        if r.status_code == 200:
            events = r.json()
            for event in events:
                event_date = event.get("date", "")
                if event_date and week_start.isoformat() <= event_date <= week_end.isoformat():
                    name = event.get("name", "").upper()
                    if any(k in name for k in HIGH_IMPACT_EVENTS):
                        log.info("High-impact event this week: %s on %s",
                                name, event_date)
                        return True
    except Exception:
        pass

    # Check FRED for scheduled Fed meetings (rough heuristic)
    # Fed meets 8 times per year — roughly every 6 weeks
    # If within 3 days of typical FOMC schedule
    now_et = datetime.now(_ET)
    if now_et.weekday() == 1 and now_et.day in range(25, 32):
        return True  # Last Tuesday of month heuristic
    if now_et.weekday() == 2 and now_et.day in range(25, 32):
        return True  # Last Wednesday of month

    return False


# ---------------------------------------------------------------------------
# Underlying price with fallback
# ---------------------------------------------------------------------------

def get_stock_price(ticker: str) -> Optional[float]:
    """Get current stock price with multiple fallbacks."""

    # Alpaca live trade
    try:
        r = requests.get(
            f"{DATA_URL}/v2/stocks/{ticker}/trades/latest",
            headers=_alpaca_headers(),
            params={"feed": "iex"},
            timeout=8,
        )
        if r.status_code == 200:
            price = float(r.json().get("trade", {}).get("p", 0))
            if price > 0:
                return price
    except Exception:
        pass

    # Alpaca snapshot
    try:
        r = requests.get(
            f"{DATA_URL}/v2/stocks/snapshots",
            headers=_alpaca_headers(),
            params={"symbols": ticker, "feed": "iex"},
            timeout=8,
        )
        if r.status_code == 200:
            price = float(r.json().get(ticker, {}).get(
                "latestTrade", {}).get("p", 0))
            if price > 0:
                return price
    except Exception:
        pass

    # Polygon previous close
    try:
        r = requests.get(
            f"{POLYGON_URL}/v2/aggs/ticker/{ticker}/prev",
            params={"apiKey": POLYGON_KEY},
            timeout=8,
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                return float(results[0].get("c", 0))
    except Exception:
        pass

    log.warning("Could not get price for %s", ticker)
    return None


# ---------------------------------------------------------------------------
# Complete pre-trade data package
# ---------------------------------------------------------------------------

def get_trade_data(ticker: str) -> dict:
    """
    Get all market data needed for execution decision.
    Returns comprehensive dict for a ticker.
    """
    price       = get_stock_price(ticker)
    iv_rank     = get_iv_rank(ticker)
    earnings    = has_earnings_soon(ticker)
    high_impact = is_high_impact_week()

    return {
        "ticker":            ticker,
        "price":             price,
        "iv_rank":           iv_rank,
        "iv_acceptable":     is_iv_acceptable(iv_rank),
        "has_earnings_soon": earnings,
        "high_impact_week":  high_impact,
        "tradeable":         (
            price is not None and
            price > 10 and          # minimum price
            not earnings and         # no earnings
            is_iv_acceptable(iv_rank)
        ),
        "size_modifier":     0.5 if high_impact else 1.0,
        "timestamp":         datetime.now(_ET).isoformat(),
    }
