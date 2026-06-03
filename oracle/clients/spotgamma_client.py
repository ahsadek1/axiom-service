"""
ORACLE — SpotGamma Client (Polygon-powered replacement)

SpotGamma has no programmatic API (dashboard only).
This client computes equivalent gamma exposure metrics from Polygon options chain data.

GEX Formula (industry standard):
    GEX = sum over all contracts of: gamma × open_interest × 100 × spot_price²  / 1,000,000
    Net GEX = Call GEX - Put GEX
    Gamma Flip = strike level where net GEX crosses zero (dealers switch long→short gamma)
    Call Wall = strike with highest call GEX (resistance)
    Put Wall = strike with highest put GEX (support)

All outputs are structurally identical to the original stub — downstream engines
see no difference.
"""

import logging
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

POLYGON_API_KEY: str = os.environ["POLYGON_API_KEY"]
POLYGON_BASE = "https://api.polygon.io"
REQUEST_TIMEOUT = 15  # options chains can be large


def _polygon_get(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Make a GET request to the Polygon API.

    Args:
        path: API path.
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


def _fetch_full_options_chain(ticker: str) -> List[Dict[str, Any]]:
    """
    Fetch the full options chain for a ticker from Polygon, paginating if needed.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        List of option contract snapshot dicts.
    """
    results: List[Dict[str, Any]] = []
    params: Dict[str, Any] = {"limit": 250}
    path = f"/v3/snapshot/options/{ticker}"

    while True:
        data = _polygon_get(path, params)
        if not data:
            break
        batch = data.get("results", [])
        results.extend(batch)

        # Polygon uses next_url for pagination
        next_url = data.get("next_url")
        if not next_url or len(results) >= 2000:
            break

        # Extract cursor from next_url for the next call
        if "cursor=" in next_url:
            cursor = next_url.split("cursor=")[-1].split("&")[0]
            params = {"limit": 250, "cursor": cursor}
        else:
            break

    return results


def _get_spot_price(ticker: str) -> Optional[float]:
    """
    Fetch current spot price for a ticker from Polygon.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        Spot price as float or None on failure.
    """
    data = _polygon_get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
    if not data:
        return None
    ticker_data = data.get("ticker", {})
    day = ticker_data.get("day", {})
    prev = ticker_data.get("prevDay", {})
    return day.get("c") or prev.get("c") or ticker_data.get("lastTrade", {}).get("p")


def _compute_gex_by_strike(
    contracts: List[Dict[str, Any]],
    spot: float
) -> Tuple[Dict[float, float], Dict[float, float], Dict[float, float]]:
    """
    Compute gamma exposure per strike, separated into call/put GEX.

    GEX per contract = gamma × open_interest × 100 × spot²  / 1,000,000

    Args:
        contracts: List of Polygon options contract snapshots.
        spot: Current spot price of the underlying.

    Returns:
        Tuple of (call_gex_by_strike, put_gex_by_strike, net_gex_by_strike) dicts.
        All values in $ millions.
    """
    call_gex: Dict[float, float] = defaultdict(float)
    put_gex: Dict[float, float] = defaultdict(float)

    for contract in contracts:
        details = contract.get("details", {})
        greeks = contract.get("greeks", {})

        contract_type = details.get("contract_type", "")
        strike = details.get("strike_price")
        gamma = greeks.get("gamma")
        oi = contract.get("open_interest", 0) or 0

        if strike is None or gamma is None or oi == 0:
            continue

        try:
            strike_f = float(strike)
            gamma_f = float(gamma)
            oi_f = float(oi)
        except (TypeError, ValueError):
            continue

        # GEX in $ millions
        gex_value = (gamma_f * oi_f * 100 * spot * spot) / 1_000_000

        if contract_type == "call":
            call_gex[strike_f] += gex_value
        elif contract_type == "put":
            # Puts flip sign — dealers are short puts = short gamma
            put_gex[strike_f] += gex_value

    net_gex: Dict[float, float] = {}
    all_strikes = set(call_gex.keys()) | set(put_gex.keys())
    for s in all_strikes:
        net_gex[s] = call_gex.get(s, 0.0) - put_gex.get(s, 0.0)

    return dict(call_gex), dict(put_gex), net_gex


def get_gamma_levels(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Compute key gamma exposure levels for a ticker from Polygon options chain.

    Calculates:
    - Net GEX: total dealer gamma exposure in $ millions
    - Gamma Flip: strike where net GEX crosses zero (long→short gamma transition)
    - Call Wall: strike with highest call GEX (resistance level)
    - Put Wall: strike with highest put GEX (support level)

    Args:
        ticker: Stock ticker symbol.

    Returns:
        Dict with gex_level, gamma_flip, call_wall, put_wall, net_gex,
        call_gex_total, put_gex_total, top_strikes list, _stub flag.
        Returns None if options data unavailable.
    """
    spot = _get_spot_price(ticker)
    if not spot or spot <= 0:
        logger.warning("Could not fetch spot price for %s — cannot compute GEX", ticker)
        return None

    contracts = _fetch_full_options_chain(ticker)
    if not contracts:
        logger.warning("No options chain data from Polygon for %s", ticker)
        return None

    call_gex, put_gex, net_gex = _compute_gex_by_strike(contracts, spot)

    if not net_gex:
        return None

    # Total GEX
    call_gex_total = sum(call_gex.values())
    put_gex_total = sum(put_gex.values())
    net_gex_total = call_gex_total - put_gex_total

    # Call Wall: strike with highest call GEX (dealers most long gamma above spot)
    call_wall_strike = max(call_gex, key=lambda s: call_gex[s]) if call_gex else None

    # Put Wall: strike with highest put GEX (dealers most short gamma below spot)
    put_wall_strike = max(put_gex, key=lambda s: put_gex[s]) if put_gex else None

    # Gamma Flip: nearest strike where net_gex crosses zero
    # Find sorted strikes where the sign of net_gex changes
    sorted_strikes = sorted(net_gex.keys())
    gamma_flip: Optional[float] = None
    for i in range(len(sorted_strikes) - 1):
        s1, s2 = sorted_strikes[i], sorted_strikes[i + 1]
        v1, v2 = net_gex[s1], net_gex[s2]
        if v1 * v2 < 0:  # sign change
            # Linear interpolation for more precise flip level
            gamma_flip = round(s1 + (s2 - s1) * abs(v1) / (abs(v1) + abs(v2)), 2)
            break

    # Top 10 strikes by absolute net GEX for context
    top_strikes = sorted(
        [{"strike": s, "net_gex": round(net_gex[s], 2)} for s in sorted_strikes],
        key=lambda x: abs(x["net_gex"]),
        reverse=True
    )[:10]

    return {
        "gex_level": round(net_gex_total, 2),
        "gamma_flip": gamma_flip,
        "call_wall": call_wall_strike,
        "put_wall": put_wall_strike,
        "net_gex": round(net_gex_total, 2),
        "call_gex_total": round(call_gex_total, 2),
        "put_gex_total": round(put_gex_total, 2),
        "spot_price": spot,
        "contracts_analyzed": len(contracts),
        "top_strikes": top_strikes,
        "_stub": False,
        "_source": "polygon_options_chain_gex"
    }


def get_hiro_flow(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Approximate real-time dealer flow from Polygon options volume data.

    HIRO (SpotGamma's real-time indicator) tracks delta-adjusted options flow.
    This provides a simplified equivalent using intraday options volume.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        Dict with call_volume, put_volume, flow_bias, delta_adjusted_flow, _stub flag.
        Returns None if data unavailable.
    """
    data = _polygon_get(
        f"/v3/snapshot/options/{ticker}",
        params={"limit": 250}
    )
    if not data or "results" not in data:
        return None

    contracts: List[Dict[str, Any]] = data.get("results", [])
    if not contracts:
        return None

    call_vol = 0.0
    put_vol = 0.0
    call_delta_flow = 0.0
    put_delta_flow = 0.0

    for contract in contracts:
        details = contract.get("details", {})
        day = contract.get("day", {})
        greeks = contract.get("greeks", {})

        contract_type = details.get("contract_type", "")
        volume = day.get("volume", 0) or 0
        delta = greeks.get("delta", 0) or 0

        if contract_type == "call":
            call_vol += volume
            call_delta_flow += volume * abs(float(delta))
        elif contract_type == "put":
            put_vol += volume
            put_delta_flow += volume * abs(float(delta))

    total_vol = call_vol + put_vol
    if total_vol == 0:
        return None

    # Flow bias: positive = bullish call flow dominant
    flow_bias = (call_delta_flow - put_delta_flow) / (call_delta_flow + put_delta_flow + 1e-9)

    return {
        "call_volume": int(call_vol),
        "put_volume": int(put_vol),
        "flow_bias": round(flow_bias, 3),
        "delta_adjusted_call_flow": round(call_delta_flow, 0),
        "delta_adjusted_put_flow": round(put_delta_flow, 0),
        "_stub": False,
        "_source": "polygon_options_volume_flow"
    }
