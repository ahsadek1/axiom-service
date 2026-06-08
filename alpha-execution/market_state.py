"""
market_state.py — Single source of truth for market conditions
==============================================================
One component owns VIX. Everything else reads through get_scanning_allowed().
C4 fix: staleness TTL — if VIX > 12min old, fail safe (pause), not fail open.
C5 fix: suspension uses wait-and-retry with backoff, not SystemExit.

Authored: 2026-05-02 | Cipher spec + OMNI adversarial review
"""

from __future__ import annotations
import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
VIX_PAUSE_THRESHOLD     = 35.0
VIX_ELEVATED_THRESHOLD  = 25.0
MAX_MARKET_STATE_AGE    = timedelta(minutes=12)   # 2 missed 5-min cycles = stale
POLL_INTERVAL_SECONDS   = 300                      # 5 min
VIX_CACHE_TTL_SECONDS   = 120                      # Cache successful VIX for 2 min

ORATS_TOKEN    = os.getenv("ORATS_TOKEN") or os.getenv("ORATS_API_KEY")
POLYGON_API_KEY = "ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl"
FRED_API_KEY   = "5749ecf7ebd18e7f77e30ef2357f55b7"

# ── VIX Cache ─────────────────────────────────────────────────────────────────
_vix_cache: Optional[tuple[float, str, float]] = None  # (value, source, timestamp)

def _get_cached_vix() -> Optional[tuple[float, str]]:
    """Return cached VIX if still fresh, else None."""
    global _vix_cache
    if _vix_cache is None:
        return None
    value, source, ts = _vix_cache
    age = datetime.now(timezone.utc).timestamp() - ts
    if age < VIX_CACHE_TTL_SECONDS:
        logger.debug(f"VIX cache hit: {value:.1f} from {source} ({age:.0f}s old)")
        return value, source
    _vix_cache = None
    return None

def _set_cached_vix(value: float, source: str) -> None:
    """Store VIX in cache."""
    global _vix_cache
    _vix_cache = (value, source, datetime.now(timezone.utc).timestamp())


# ── MarketState ───────────────────────────────────────────────────────────────

@dataclass
class MarketState:
    regime:                 str     = "UNKNOWN"   # NORMAL|ELEVATED_VOL|HIGH_VOL|EXTREME_FEAR
    vix:                    Optional[float] = None
    vix_source:             str     = "unavailable"
    vix_updated_at:         Optional[datetime] = None
    scanning_allowed:       bool    = False       # default CLOSED until first successful poll
    credit_spreads_allowed: bool    = False
    debit_spreads_allowed:  bool    = False
    pause_reason:           Optional[str] = None
    is_stale:               bool    = True        # starts stale until first update

    # Suspension state (C5 fix — no SystemExit)
    suspended:              bool    = False
    suspend_reason:         Optional[str] = None
    suspended_at:           Optional[datetime] = None


def get_scanning_allowed(state: MarketState) -> bool:
    """
    C4 fix: Never read state.scanning_allowed directly.
    Always check staleness first — fail safe, not fail open.
    """
    if state.suspended:
        return False

    if state.vix_updated_at is None:
        logger.warning("MarketState: no VIX reading yet — scanning blocked")
        return False

    age = datetime.now(timezone.utc) - state.vix_updated_at
    if age > MAX_MARKET_STATE_AGE:
        logger.warning(
            "MarketState STALE: VIX data is %d min old (max %d) — pausing scan",
            int(age.total_seconds() / 60),
            int(MAX_MARKET_STATE_AGE.total_seconds() / 60),
        )
        return False

    return state.scanning_allowed


def classify_regime(vix: float) -> tuple[str, bool, bool, bool]:
    """
    Returns (regime, scanning_allowed, credit_spreads_allowed, debit_spreads_allowed).
    """
    if vix >= VIX_PAUSE_THRESHOLD:
        return "EXTREME_FEAR", False, False, False
    elif vix >= VIX_ELEVATED_THRESHOLD:
        return "HIGH_VOL", True, True, False   # credit OK, no debit in high vol
    elif vix >= 18:
        return "ELEVATED_VOL", True, True, True
    else:
        return "NORMAL", True, True, True


# ── VIX Fetchers ─────────────────────────────────────────────────────────────

async def _fetch_vix_yahoo() -> Optional[float]:
    """Fetch VIX from Yahoo Finance (primary — no API key required)."""
    import aiohttp
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX"
    # Improved headers to reduce 429 rate limit issues
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://finance.yahoo.com",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    price = (data.get("chart", {}).get("result", [{}])[0]
                             .get("meta", {}).get("regularMarketPrice"))
                    if price:
                        return float(price)
                elif r.status == 429:
                    logger.warning("Yahoo Finance rate limited (429) — trying alternative endpoint")
                    return await _fetch_vix_yahoo_alt(headers)
    except Exception as e:
        logger.warning("Yahoo Finance VIX fetch failed: %s", e)
    return None


async def _fetch_vix_yahoo_alt(headers: dict) -> Optional[float]:
    """Alternative Yahoo endpoint for VIX (v10 quote summary)."""
    import aiohttp
    url = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/%5EVIX"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params={"modules": "price"},
                                   timeout=aiohttp.ClientTimeout(total=6)) as r:
                if r.status == 200:
                    data = await r.json()
                    price = (data.get("quoteSummary", {}).get("result", [{}])[0]
                             .get("price", {}).get("regularMarketPrice", {}).get("raw"))
                    if price:
                        return float(price)
    except Exception as e:
        logger.debug("Yahoo alt VIX fetch failed: %s", e)
    return None


async def _fetch_vix_polygon() -> Optional[float]:
    """Fetch VIX from Polygon indices snapshot (primary — I:VIX, real-time)."""
    import aiohttp
    # NOTE: VIX is I:VIX (index), not a stock. Requires Polygon index data plan.
    url = "https://api.polygon.io/v3/snapshot"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"ticker.any_of": "I:VIX", "apiKey": POLYGON_API_KEY},
                                   timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    results = data.get("results", [])
                    if results and not results[0].get("error"):
                        last = results[0].get("session", {}).get("close") or results[0].get("value")
                        if last:
                            return float(last)
    except Exception as e:
        logger.warning("Polygon VIX fetch failed: %s", e)
    return None


async def _fetch_vix_fred() -> Optional[float]:
    """Fetch VIX from FRED API (fallback)."""
    import aiohttp
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": "VIXCLS", "api_key": FRED_API_KEY,
        "sort_order": "desc", "limit": 1, "file_type": "json"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    obs = data.get("observations", [])
                    if obs and obs[0].get("value") != ".":
                        return float(obs[0]["value"])
    except Exception as e:
        logger.warning("FRED VIX fetch failed: %s", e)
    return None


async def get_vix_with_fallback() -> tuple[Optional[float], str]:
    """
    Fetch VIX: Cache check → Polygon primary → Yahoo secondary → FRED tertiary.
    Returns (vix_value, source_string).
    Returns (None, "unavailable") if all three fail — caller must pause scanning.
    NEVER returns a silent default.
    """
    # Check cache first
    cached = _get_cached_vix()
    if cached is not None:
        return cached
    
    vix = await _fetch_vix_polygon()
    if vix is not None:
        _set_cached_vix(vix, "polygon")
        return vix, "polygon"

    logger.warning("Polygon VIX failed — trying Yahoo fallback")
    vix = await _fetch_vix_yahoo()
    if vix is not None:
        _set_cached_vix(vix, "yahoo_fallback")
        return vix, "yahoo_fallback"

    logger.warning("Yahoo VIX failed — trying FRED fallback")
    vix = await _fetch_vix_fred()
    if vix is not None:
        _set_cached_vix(vix, "fred_fallback")
        return vix, "fred_fallback"

    logger.error("All three VIX sources failed (Polygon, Yahoo, FRED) — returning unavailable")
    return None, "unavailable"


# ── Poller ────────────────────────────────────────────────────────────────────

async def market_state_poller(
    state: MarketState,
    alert_fn,       # callable(msg: str) — sends Telegram alert to Ahmed
    db_path: str,
) -> None:
    """
    Single dedicated poller. Updates MarketState every 5 minutes.
    On VIX unavailable: pause scanning, alert Ahmed once.
    On VIX restored: resume automatically, alert Ahmed.

    C4: This is the ONLY code that writes to MarketState.
    Everyone else calls get_scanning_allowed(state) — never reads .scanning_allowed directly.
    """
    vix_unavailable_alerted = False

    while True:
        try:
            vix, source = await get_vix_with_fallback()

            if vix is None:
                # Fail safe — pause scanning
                state.scanning_allowed        = False
                state.credit_spreads_allowed  = False
                state.debit_spreads_allowed   = False
                state.vix_source              = "unavailable"
                state.pause_reason            = "VIX data unavailable — both Polygon and FRED failed"
                state.regime                  = "UNKNOWN"
                # Alert Ahmed only once per outage
                if not vix_unavailable_alerted:
                    vix_unavailable_alerted = True
                    await alert_fn(
                        "⚠️ <b>VIX DATA UNAVAILABLE</b>\n"
                        "Both Polygon and FRED failed.\n"
                        "Scanning PAUSED until data restored.\n"
                        "No trades will execute."
                    )
                logger.error("VIX unavailable — scanning paused")
            else:
                regime, scan_ok, credit_ok, debit_ok = classify_regime(vix)
                was_unavailable = (state.vix_source == "unavailable" or vix_unavailable_alerted)

                state.vix                     = vix
                state.vix_source              = source
                state.vix_updated_at          = datetime.now(timezone.utc)
                state.regime                  = regime
                state.scanning_allowed        = scan_ok
                state.credit_spreads_allowed  = credit_ok
                state.debit_spreads_allowed   = debit_ok
                state.pause_reason            = None if scan_ok else f"VIX={vix:.1f} >= {VIX_PAUSE_THRESHOLD}"
                state.is_stale                = False

                # Alert on VIX halt
                if not scan_ok:
                    await alert_fn(
                        f"🚨 <b>VIX HALT: {vix:.1f}</b>\n"
                        f"Scanning paused (threshold: {VIX_PAUSE_THRESHOLD}).\n"
                        "All new position entries blocked."
                    )

                # Alert on recovery
                if was_unavailable and vix is not None:
                    vix_unavailable_alerted = False
                    await alert_fn(
                        f"✅ <b>VIX data restored</b>: {vix:.1f} from {source}\n"
                        f"Regime: {regime}. Scanning resumed."
                    )

                # Persist to DB
                try:
                    import sqlite3
                    with sqlite3.connect(db_path) as conn:
                        conn.execute(
                            "INSERT INTO market_state_log "
                            "(vix, regime, scanning_allowed, source) VALUES (?,?,?,?)",
                            (vix, regime, int(scan_ok), source)
                        )
                except Exception as db_err:
                    logger.warning("MarketState DB log failed: %s", db_err)

                logger.info(
                    "MarketState updated: VIX=%.1f regime=%s scan=%s source=%s",
                    vix, regime, scan_ok, source
                )

        except Exception as e:
            logger.error("MarketState poller error: %s", e)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ── Suspension (C5 fix — no SystemExit) ──────────────────────────────────────

async def suspend_until_resume(
    state: MarketState,
    reason: str,
    alert_fn,
    resume_check_fn,    # callable() -> bool — checks if Ahmed sent /resume
    retry_fn=None,      # optional callable() -> bool — retries the failing condition
) -> None:
    """
    Suspends scanning with wait-and-retry. No SystemExit. No restart loop.
    Alerts Ahmed once. Retries the failing condition every 10 minutes.
    Auto-resumes if retry_fn passes or if Ahmed sends /resume.
    """
    state.suspended       = True
    state.suspend_reason  = reason
    state.suspended_at    = datetime.now(timezone.utc)

    await alert_fn(
        f"⏸️ <b>OMNI SUSPENDED</b>\n"
        f"Reason: {reason}\n"
        f"Auto-retry every 10 minutes.\n"
        f"Or send /resume to force restart."
    )
    logger.critical("OMNI suspended: %s", reason)

    retry_interval = 600  # 10 minutes
    attempt = 0

    while state.suspended:
        await asyncio.sleep(60)
        attempt += 1

        # Check manual /resume from Ahmed
        if await resume_check_fn():
            logger.info("Resume received from Ahmed — clearing suspension")
            state.suspended      = False
            state.suspend_reason = None
            await alert_fn("▶️ <b>OMNI resumed</b> (manual /resume received)")
            return

        # Retry the failing condition periodically
        if retry_fn and attempt % (retry_interval // 60) == 0:
            try:
                ok = await retry_fn()
                if ok:
                    state.suspended      = False
                    state.suspend_reason = None
                    await alert_fn(f"▶️ <b>OMNI auto-resumed</b> — condition resolved: {reason}")
                    logger.info("OMNI auto-resumed after condition resolved")
                    return
                else:
                    logger.info("Suspension retry failed — still suspended: %s", reason)
            except Exception as e:
                logger.warning("Suspension retry error: %s", e)
