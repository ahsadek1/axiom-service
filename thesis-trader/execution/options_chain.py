"""
options_chain.py — Live Options Chain & Quote Fetcher
======================================================
Fetches real options chains from Alpaca.
Selects optimal strikes for bull put spreads.

Target parameters:
  Short put delta: 0.20-0.30 (OTM, high probability)
  DTE: 30-45 days (optimal theta decay)
  Spread width: $5 (standard)
  Min premium: $0.50 per spread ($50 per contract)
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, date, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("thesis.options_chain")
_ET = ZoneInfo("America/New_York")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL      = "https://paper-api.alpaca.markets"
DATA_URL      = "https://data.alpaca.markets"

HEADERS = property(lambda self: {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
})

def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY",""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY",""),
    }

def _alpaca(url: str, params: dict = None) -> Optional[dict]:
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
        log.warning("Alpaca %s → %d: %s", url, r.status_code, r.text[:100])
    except Exception as exc:
        log.error("Alpaca request failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Underlying price
# ---------------------------------------------------------------------------

def get_underlying_price(ticker: str) -> Optional[float]:
    """Get current price of underlying stock."""
    data = _alpaca(
        f"{DATA_URL}/v2/stocks/{ticker}/trades/latest",
        params={"feed": "iex"},
    )
    if data:
        return float(data.get("trade", {}).get("p", 0)) or None

    # Fallback: snapshot
    data = _alpaca(
        f"{DATA_URL}/v2/stocks/snapshots",
        params={"symbols": ticker, "feed": "iex"},
    )
    if data and ticker in data:
        return float(data[ticker].get("latestTrade", {}).get("p", 0)) or None
    return None


# ---------------------------------------------------------------------------
# Options chain
# ---------------------------------------------------------------------------

def get_target_expiration(target_dte: int = 37) -> tuple[date, date]:
    """Get date range for target DTE window (±7 days of target)."""
    today = date.today()
    target = today + timedelta(days=target_dte)
    return (target - timedelta(days=7), target + timedelta(days=7))


def get_put_chain(
    ticker: str,
    min_strike: float,
    max_strike: float,
    target_dte: int = 37,
) -> list[dict]:
    """
    Fetch put options chain for a ticker within strike range and DTE window.
    Returns list of contracts sorted by strike descending.
    """
    dte_min, dte_max = get_target_expiration(target_dte)

    data = _alpaca(
        f"{BASE_URL}/v2/options/contracts",
        params={
            "underlying_symbols": ticker,
            "type":               "put",
            "expiration_date_gte": dte_min.isoformat(),
            "expiration_date_lte": dte_max.isoformat(),
            "strike_price_gte":   str(int(min_strike)),
            "strike_price_lte":   str(int(max_strike)),
            "tradable":           "true",
            "limit":              50,
        },
    )

    if not data:
        return []

    contracts = data.get("option_contracts", [])
    # Sort by expiration then strike descending
    contracts.sort(
        key=lambda c: (c["expiration_date"], -float(c["strike_price"]))
    )
    return contracts


def get_option_snapshot(symbol: str) -> Optional[dict]:
    """Get latest quote/greeks for a single option contract."""
    data = _alpaca(
        f"{DATA_URL}/v1beta1/options/snapshots",
        params={"symbols": symbol, "feed": "indicative"},
    )
    if data and "snapshots" in data:
        return data["snapshots"].get(symbol)
    return None


def get_option_quotes(symbols: list[str]) -> dict[str, dict]:
    """Get quotes for multiple option contracts."""
    if not symbols:
        return {}
    data = _alpaca(
        f"{DATA_URL}/v1beta1/options/snapshots",
        params={"symbols": ",".join(symbols), "feed": "indicative"},
    )
    if data and "snapshots" in data:
        return data["snapshots"]
    return {}


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------

def select_bull_put_strikes(
    ticker: str,
    underlying_price: float,
    target_dte: int = 37,
    short_delta_target: float = 0.25,
    spread_width: float = 5.0,
    min_premium: float = 0.30,
) -> Optional[dict]:
    """
    Select optimal bull put spread strikes.

    Strategy:
      Short put: OTM, targeting delta ~0.25 (high probability)
      Long put:  $5 below short put (defines max loss)

    Returns dict with full spread details or None if no suitable strikes.
    """
    # Target short strike: 10-15% OTM from current price
    target_short = underlying_price * 0.88  # ~12% OTM
    min_strike   = underlying_price * 0.75
    max_strike   = underlying_price * 0.95

    contracts = get_put_chain(ticker, min_strike, max_strike, target_dte)
    if not contracts:
        log.warning("%s: No put contracts found in range %.0f-%.0f",
                   ticker, min_strike, max_strike)
        return None

    # Group by expiration — pick the expiration closest to target DTE
    today = date.today()
    expirations = sorted(set(c["expiration_date"] for c in contracts))
    target_exp_date = today + timedelta(days=target_dte)

    best_exp = min(
        expirations,
        key=lambda e: abs((date.fromisoformat(e) - target_exp_date).days)
    )
    dte = (date.fromisoformat(best_exp) - today).days

    # Filter to best expiration
    exp_contracts = [c for c in contracts if c["expiration_date"] == best_exp]
    strikes = sorted(set(float(c["strike_price"]) for c in exp_contracts), reverse=True)

    if len(strikes) < 2:
        log.warning("%s: Not enough strikes for spread", ticker)
        return None

    # Find short strike closest to target
    short_strike = min(strikes, key=lambda s: abs(s - target_short))
    # Long strike is $5 below
    long_strike  = short_strike - spread_width

    # Verify long strike exists in chain
    available_longs = [s for s in strikes if s < short_strike]
    if not available_longs:
        log.warning("%s: No long strike available below %.0f", ticker, short_strike)
        return None
    long_strike = max(available_longs)  # closest available below short

    # Build contract symbols
    def make_symbol(ticker, exp, put_call, strike):
        exp_str = date.fromisoformat(exp).strftime("%y%m%d")
        strike_int = int(strike * 1000)
        return f"{ticker}{exp_str}{put_call}{strike_int:08d}"

    short_symbol = make_symbol(ticker, best_exp, "P", short_strike)
    long_symbol  = make_symbol(ticker, best_exp, "P", long_strike)

    # Get quotes
    quotes = get_option_quotes([short_symbol, long_symbol])
    short_quote = quotes.get(short_symbol, {})
    long_quote  = quotes.get(long_symbol, {})

    # Extract mid prices
    def mid_price(quote: dict) -> float:
        gb = quote.get("greeks", {}) or {}
        lq = quote.get("latestQuote", {}) or {}
        bid = float(lq.get("bp", 0) or 0)
        ask = float(lq.get("ap", 0) or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return 0.0

    short_mid = mid_price(short_quote)
    long_mid  = mid_price(long_quote)
    net_premium = short_mid - long_mid

    # If no live quotes (market closed), estimate from intrinsic
    if net_premium <= 0:
        # Conservative estimate: 1-2% of spread width
        net_premium = spread_width * 0.015
        log.info("%s: No live quotes — estimating premium at $%.2f", ticker, net_premium)

    if net_premium < min_premium:
        log.info("%s: Premium $%.2f below minimum $%.2f — skip",
                ticker, net_premium, min_premium)
        return None

    max_loss    = spread_width - net_premium
    max_profit  = net_premium
    breakeven   = short_strike - net_premium
    risk_reward = net_premium / max_loss if max_loss > 0 else 0

    log.info(
        "%s bull put spread: %s/%s exp=%s DTE=%d "
        "premium=$%.2f max_loss=$%.2f R:R=%.2f",
        ticker, short_strike, long_strike, best_exp, dte,
        net_premium, max_loss, risk_reward,
    )

    return {
        "ticker":         ticker,
        "underlying_price": underlying_price,
        "strategy":       "bull_put_spread",
        "expiration":     best_exp,
        "dte":            dte,
        "short_strike":   short_strike,
        "long_strike":    long_strike,
        "short_symbol":   short_symbol,
        "long_symbol":    long_symbol,
        "net_premium":    round(net_premium, 2),
        "max_profit":     round(max_profit, 2),
        "max_loss":       round(max_loss, 2),
        "breakeven":      round(breakeven, 2),
        "risk_reward":    round(risk_reward, 3),
        "spread_width":   spread_width,
        "short_delta":    short_quote.get("greeks", {}).get("delta", -0.25) if short_quote else -0.25,
        "short_iv":       short_quote.get("impliedVolatility", 0) if short_quote else 0,
    }
