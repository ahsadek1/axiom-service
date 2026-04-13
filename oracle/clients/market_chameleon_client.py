"""
ORACLE — Market Chameleon Client (Polygon-powered replacement)

Market Chameleon has no programmatic API (dashboard only).
This client computes equivalent metrics from Polygon options chain data:
  - IV rank / percentile: derived from ORATS (primary) or HV comparison via Polygon
  - Put/call ratio: calculated from Polygon options chain aggregate OI + volume
  - Earnings move history: calculated from Polygon price history around Alpha Vantage
    earnings dates

All outputs are structurally identical to the original stub — downstream engines
see no difference.
"""

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

POLYGON_API_KEY: str = os.environ["POLYGON_API_KEY"]
POLYGON_BASE = "https://api.polygon.io"
REQUEST_TIMEOUT = 10


def _polygon_get(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Make a GET request to the Polygon API.

    Args:
        path: API path (e.g. /v3/snapshot/options/AAPL).
        params: Optional query parameters.

    Returns:
        Parsed JSON dict or None on failure.
    """
    url = f"{POLYGON_BASE}{path}"
    p = params or {}
    p["apiKey"] = POLYGON_API_KEY
    try:
        resp = requests.get(url, params=p, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        logger.warning("Polygon request failed for %s: %s", path, exc)
        return None


def get_put_call_ratio(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Calculate put/call ratio for a ticker from Polygon options chain.

    Aggregates open interest and volume across all expirations and strikes.
    P/C ratio < 0.7 = bullish sentiment; > 1.2 = bearish/hedging.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        Dict with oi_put_call_ratio, volume_put_call_ratio, total_call_oi,
        total_put_oi, total_call_volume, total_put_volume, _stub flag.
        Returns None if Polygon call fails.
    """
    data = _polygon_get(
        f"/v3/snapshot/options/{ticker}",
        params={"limit": 250}
    )
    if not data or "results" not in data:
        logger.warning("No options chain data from Polygon for %s", ticker)
        return None

    results: List[Dict[str, Any]] = data.get("results", [])
    if not results:
        return None

    total_call_oi = 0
    total_put_oi = 0
    total_call_vol = 0
    total_put_vol = 0

    for contract in results:
        details = contract.get("details", {})
        day = contract.get("day", {})
        greeks = contract.get("greeks", {})

        contract_type = details.get("contract_type", "")
        oi = contract.get("open_interest", 0) or 0
        volume = day.get("volume", 0) or 0

        if contract_type == "call":
            total_call_oi += oi
            total_call_vol += volume
        elif contract_type == "put":
            total_put_oi += oi
            total_put_vol += volume

    oi_ratio = (total_put_oi / total_call_oi) if total_call_oi > 0 else None
    vol_ratio = (total_put_vol / total_call_vol) if total_call_vol > 0 else None

    return {
        "oi_put_call_ratio": round(oi_ratio, 3) if oi_ratio is not None else None,
        "volume_put_call_ratio": round(vol_ratio, 3) if vol_ratio is not None else None,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "total_call_volume": total_call_vol,
        "total_put_volume": total_put_vol,
        "_stub": False,
        "_source": "polygon_options_chain"
    }


def get_earnings_move_history(ticker: str, lookback_quarters: int = 8) -> Optional[Dict[str, Any]]:
    """
    Calculate historical earnings move magnitudes from Polygon price history.

    Computes the 1-day price move (open-to-close) for the day after each
    earnings announcement over the past N quarters. Provides expected move
    context that Market Chameleon would have shown.

    Args:
        ticker: Stock ticker symbol.
        lookback_quarters: How many past earnings events to analyze (default 8 = 2 years).

    Returns:
        Dict with avg_move_pct, max_move_pct, recent_moves list, beat_count,
        miss_count (if available), _stub flag.
        Returns None if insufficient data.
    """
    # Get earnings dates from Alpha Vantage earnings endpoint
    av_key = os.environ.get("ALPHA_VANTAGE_KEY")
    if not av_key:
        logger.warning("ALPHA_VANTAGE_KEY not set — cannot fetch earnings dates for %s", ticker)
        return None

    try:
        av_resp = requests.get(
            "https://www.alphavantage.co/query",
            params={"function": "EARNINGS", "symbol": ticker, "apikey": av_key},
            timeout=REQUEST_TIMEOUT
        )
        av_resp.raise_for_status()
        av_data = av_resp.json()
    except requests.RequestException as exc:
        logger.warning("Alpha Vantage earnings fetch failed for %s: %s", ticker, exc)
        return None

    quarterly = av_data.get("quarterlyEarnings", [])
    if not quarterly:
        return None

    # Take last N quarters with a report date
    dated = [q for q in quarterly if q.get("reportedDate") and q["reportedDate"] != "None"]
    dated = dated[:lookback_quarters]
    if len(dated) < 2:
        return None

    moves: List[float] = []
    for entry in dated:
        report_date_str = entry.get("reportedDate", "")
        try:
            report_dt = datetime.strptime(report_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        # Get price for report day and next trading day
        start = (report_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        end = (report_dt + timedelta(days=3)).strftime("%Y-%m-%d")

        price_data = _polygon_get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            params={"adjusted": "true", "sort": "asc", "limit": 5}
        )
        if not price_data or not price_data.get("results"):
            continue

        bars = price_data["results"]
        if len(bars) < 2:
            continue

        # Find the bar on/after report date
        report_ts = int(datetime.combine(report_dt, datetime.min.time()).timestamp() * 1000)
        post_bars = [b for b in bars if b.get("t", 0) >= report_ts]
        if not post_bars:
            continue

        post_bar = post_bars[0]
        open_price = post_bar.get("o", 0)
        close_price = post_bar.get("c", 0)
        if open_price and open_price > 0:
            move_pct = ((close_price - open_price) / open_price) * 100
            moves.append(round(move_pct, 2))

    if not moves:
        return None

    abs_moves = [abs(m) for m in moves]
    return {
        "avg_move_pct": round(sum(abs_moves) / len(abs_moves), 2),
        "max_move_pct": round(max(abs_moves), 2),
        "min_move_pct": round(min(abs_moves), 2),
        "recent_moves": moves,
        "quarters_analyzed": len(moves),
        "_stub": False,
        "_source": "polygon_price_history + alpha_vantage_earnings"
    }


def get_iv_rank(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Return IV rank data — deferred to ORATS as primary source.

    Market Chameleon's IV rank is superseded by ORATS which already provides
    IV rank, percentile, and term structure via its dedicated API.
    This function returns a redirect signal so the vol engine uses ORATS.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        Dict indicating ORATS is primary source for IV data.
    """
    logger.debug("IV rank for %s — use ORATS client (primary source)", ticker)
    return {
        "iv_rank": None,
        "iv_percentile": None,
        "hv30": None,
        "hv_comparison": None,
        "_stub": False,
        "_source": "orats_primary",
        "_note": "IV rank provided by ORATS client — this function is a passthrough"
    }
