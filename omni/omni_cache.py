"""
omni_cache.py — OMNI Hot-Path Cache Layer
==========================================
Caches Axiom regime and per-ticker risk assessments.
Eliminates redundant HTTP calls from the synthesis hot path.

Cache TTLs:
  Regime classification: 60 seconds (changes slowly)
  Ticker risk assessment: 120 seconds (changes slowly)
  ORACLE context:        300 seconds (5 minutes)

All cache reads are instant SQLite/memory lookups.
All cache misses fall back to live HTTP with hard timeout.
The synthesis worker NEVER waits more than 2 seconds for external data.

Ahmed directive May 2026: Move all cacheable functions to deterministic realm.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

log = logging.getLogger("omni.cache")

# ---------------------------------------------------------------------------
# In-memory cache (process-level, resets on OMNI restart — acceptable)
# ---------------------------------------------------------------------------

_regime_cache: dict[str, Any] = {}
_regime_cache_time: float = 0.0
REGIME_TTL = 60  # seconds

_risk_cache: dict[str, tuple[Any, float]] = {}  # ticker -> (result, timestamp)
RISK_TTL = 120  # seconds

_oracle_cache: dict[str, tuple[Any, float]] = {}  # ticker -> (result, timestamp)
ORACLE_TTL = 300  # seconds

# Hard timeout for all external calls from cache miss path
EXTERNAL_TIMEOUT = 2.0  # seconds — synthesis never waits more than this


# ---------------------------------------------------------------------------
# Cached regime
# ---------------------------------------------------------------------------

def get_regime_cached(axiom_url: str, axiom_secret: str) -> Optional[dict]:
    """
    Return regime from cache if fresh, else fetch with hard 2s timeout.
    Regime changes slowly — 60 second cache is safe.
    """
    global _regime_cache, _regime_cache_time
    now = time.time()

    if _regime_cache and (now - _regime_cache_time) < REGIME_TTL:
        log.debug("Regime cache HIT (age=%.0fs)", now - _regime_cache_time)
        return _regime_cache

    # Cache miss — fetch with hard timeout
    try:
        import requests
        resp = requests.get(
            f"{axiom_url}/health",
            headers={"X-Axiom-Secret": axiom_secret},
            timeout=EXTERNAL_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            regime_str = data.get("regime", "NORMAL")
            result = {"classification": regime_str, "vix": data.get("vix", 18.0)}
            _regime_cache = result
            _regime_cache_time = now
            log.debug("Regime cache MISS → fetched: %s", regime_str)
            return result
    except Exception as exc:
        log.warning("Regime fetch failed (%.1fs timeout): %s", EXTERNAL_TIMEOUT, exc)

    # Return stale cache if available, else default
    if _regime_cache:
        log.warning("Regime fetch failed — using stale cache (age=%.0fs)", now - _regime_cache_time)
        return _regime_cache

    log.warning("Regime fetch failed — using default NORMAL")
    return {"classification": "NORMAL", "vix": 18.0}


# ---------------------------------------------------------------------------
# Cached ticker risk
# ---------------------------------------------------------------------------

def assess_ticker_cached(axiom_url: str, axiom_secret: str, ticker: str) -> Optional[dict]:
    """
    Return Axiom risk assessment from cache if fresh, else fetch with 2s timeout.
    Per-ticker risk changes slowly intraday — 120 second cache is safe.
    """
    now = time.time()
    cached = _risk_cache.get(ticker)
    if cached:
        result, ts = cached
        if (now - ts) < RISK_TTL:
            log.debug("Risk cache HIT for %s (age=%.0fs)", ticker, now - ts)
            return result

    # Cache miss
    try:
        import requests
        resp = requests.post(
            f"{axiom_url}/assess",
            json={"ticker": ticker},
            headers={"X-Axiom-Secret": axiom_secret},
            timeout=EXTERNAL_TIMEOUT,
        )
        if resp.status_code == 200:
            result = resp.json()
            _risk_cache[ticker] = (result, now)
            log.debug("Risk cache MISS → fetched for %s", ticker)
            return result
    except Exception as exc:
        log.warning("Risk assess failed for %s (%.1fs timeout): %s", ticker, EXTERNAL_TIMEOUT, exc)

    # Return stale if available
    if cached:
        log.warning("Risk fetch failed for %s — using stale cache", ticker)
        return cached[0]

    # Safe default — no hard stops, normal sizing
    log.warning("Risk fetch failed for %s — using safe default", ticker)
    return {"risk_score": 10.0, "sizing_mult": 1.0, "hard_stops": [], "flags": []}


# ---------------------------------------------------------------------------
# Cached ORACLE context
# ---------------------------------------------------------------------------

def get_oracle_context_cached(oracle_url: str, nexus_secret: str, ticker: str) -> Optional[dict]:
    """
    Return ORACLE context from cache if fresh, else fetch with 2s timeout.
    Options/flow data changes on 5-minute cycles — 300 second cache is safe.
    """
    now = time.time()
    cached = _oracle_cache.get(ticker)
    if cached:
        result, ts = cached
        if (now - ts) < ORACLE_TTL:
            log.debug("ORACLE cache HIT for %s (age=%.0fs)", ticker, now - ts)
            return result

    # Cache miss
    try:
        import requests
        resp = requests.get(
            f"{oracle_url}/context/{ticker}",
            headers={"X-Nexus-Secret": nexus_secret},
            timeout=EXTERNAL_TIMEOUT,
        )
        if resp.status_code == 200:
            result = resp.json()
            _oracle_cache[ticker] = (result, now)
            log.debug("ORACLE cache MISS → fetched for %s", ticker)
            return result
    except Exception as exc:
        log.warning("ORACLE fetch failed for %s (%.1fs timeout): %s", ticker, EXTERNAL_TIMEOUT, exc)

    if cached:
        log.warning("ORACLE fetch failed for %s — using stale cache", ticker)
        return cached[0]

    return None


# ---------------------------------------------------------------------------
# Cache stats (for health endpoint)
# ---------------------------------------------------------------------------

def cache_stats() -> dict:
    now = time.time()
    return {
        "regime_cached": bool(_regime_cache),
        "regime_age_s": round(now - _regime_cache_time, 1) if _regime_cache_time else None,
        "risk_tickers_cached": len(_risk_cache),
        "oracle_tickers_cached": len(_oracle_cache),
        "external_timeout_s": EXTERNAL_TIMEOUT,
    }
