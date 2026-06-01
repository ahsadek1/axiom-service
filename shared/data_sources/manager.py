"""
manager.py — Universal Data Source Manager Entry Point
=======================================================
Single import that starts everything:
  - All source monitors (5-min probes)
  - Fallback manager (auto-switch on failure)
  - Status API (port 8097)

Usage:
    from shared.data_sources.manager import start_data_source_manager
    start_data_source_manager()

Then anywhere in the codebase:
    from shared.data_sources.manager import get_price_data, get_iv_rank
"""
from __future__ import annotations
import logging
import os
from typing import Optional

log = logging.getLogger("nexus.data_sources")

_started = False


def start_data_source_manager(port: int = 8097) -> None:
    global _started
    if _started:
        return
    _started = True

    from .fallback_manager import init_active_sources, on_source_failed, on_source_recovered
    from .health_monitor import start_all_monitors
    from .status_api import start_status_api

    class FallbackHandler:
        def on_source_failed(self, source):
            on_source_failed(source)
        def on_source_recovered(self, source):
            on_source_recovered(source)

    init_active_sources()
    start_all_monitors(fallback_manager=FallbackHandler())
    start_status_api(port=port)
    log.info("Universal Data Source Manager started — %d sources monitored", 
             len(__import__('shared.data_sources.registry',
                           fromlist=['ALL_SOURCES']).ALL_SOURCES))


# ---------------------------------------------------------------------------
# Convenience fetchers — use active source automatically
# ---------------------------------------------------------------------------

POLYGON_KEY = os.getenv("POLYGON_API_KEY", "ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl")
ALPACA_KEY  = os.getenv("ALPACA_API_KEY", "")
ALPACA_SEC  = os.getenv("ALPACA_SECRET_KEY", "")

import requests as _req


def get_price_bars(ticker: str, days: int = 60) -> Optional[list]:
    """Get price bars using active source with automatic fallback."""
    from .fallback_manager import get_active_source
    from .registry import SourceCategory
    from datetime import date, timedelta

    source = get_active_source(SourceCategory.PRICE)
    end    = date.today()
    start  = end - timedelta(days=days)

    # Try Polygon (primary or secondary)
    if not source or "polygon" in source.source_id:
        try:
            r = _req.get(
                f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
                params={"adjusted":"true","sort":"asc","limit":days,"apiKey":POLYGON_KEY},
                timeout=8,
            )
            if r.status_code == 200:
                return r.json().get("results", [])
        except Exception:
            pass

    # Try Alpaca data (secondary)
    try:
        r = _req.get(
            f"https://data.alpaca.markets/v2/stocks/{ticker}/bars",
            headers={"APCA-API-KEY-ID":ALPACA_KEY,"APCA-API-SECRET-KEY":ALPACA_SEC},
            params={"timeframe":"1Day","start":str(start),"end":str(end),"limit":days},
            timeout=8,
        )
        if r.status_code == 200:
            bars = r.json().get("bars", [])
            # Normalize to Polygon format
            return [{"o":b["o"],"h":b["h"],"l":b["l"],"c":b["c"],"v":b["v"]} for b in bars]
    except Exception:
        pass

    return None


def get_iv_rank(ticker: str) -> Optional[float]:
    """Get IV rank using active source with automatic fallback."""
    from .fallback_manager import get_active_source
    from .registry import SourceCategory, SourceTier
    from datetime import date, timedelta

    source = get_active_source(SourceCategory.OPTIONS_IV)
    ORATS_TOKEN = os.getenv("ORATS_TOKEN", "")

    # Try ORATS (primary)
    if ORATS_TOKEN and (not source or source.tier == SourceTier.PRIMARY):
        try:
            today = date.today()
            r = _req.get(
                "https://api.orats.io/datav2/hist/dailies",
                params={
                    "token": ORATS_TOKEN, "ticker": ticker,
                    "fields": "ticker,tradeDate,ivRank",
                    "startDate": (today - timedelta(days=5)).isoformat(),
                    "endDate":   today.isoformat(),
                },
                timeout=8,
            )
            if r.status_code == 200:
                rows = r.json().get("data", [])
                for row in reversed(rows):
                    iv = row.get("ivRank")
                    if iv is not None:
                        return float(iv)
        except Exception:
            pass

    # Fallback: Polygon historical vol proxy
    bars = get_price_bars(ticker, days=252)
    if bars and len(bars) >= 25:
        closes = [b["c"] for b in bars]
        def rvol(p):
            if len(p) < 2: return 0.0
            rets = [(p[i]-p[i-1])/p[i-1] for i in range(1,len(p))]
            m = sum(rets)/len(rets)
            return ((sum((r-m)**2 for r in rets)/len(rets))**0.5) * (252**0.5) * 100
        cur = rvol(closes[-21:])
        vols = [rvol(closes[i-20:i]) for i in range(20, len(closes))]
        if vols:
            lo, hi = min(vols), max(vols)
            if hi > lo:
                return round(min(100.0, max(0.0, (cur-lo)/(hi-lo)*100)), 1)

    return None


def get_vix() -> Optional[float]:
    """Get VIX using active source."""
    from .fallback_manager import get_active_source
    from .registry import SourceCategory

    # Try Axiom first (fastest, already computed)
    AXIOM_SECRET = os.getenv("AXIOM_SECRET", "")
    try:
        r = _req.get(
            "http://localhost:8001/pool",
            headers={"X-Axiom-Secret": AXIOM_SECRET},
            timeout=4,
        )
        if r.status_code == 200:
            vix = r.json().get("regime", {}).get("vix")
            if vix:
                return float(vix)
    except Exception:
        pass

    # Polygon VXX as proxy
    try:
        r = _req.get(
            "https://api.polygon.io/v2/aggs/ticker/VXX/prev",
            params={"apiKey": POLYGON_KEY},
            timeout=8,
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                return float(results[0]["c"])
    except Exception:
        pass

    return None
