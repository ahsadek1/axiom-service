"""
ORACLE Intelligence Hub — Main FastAPI Application
Port: 8007
Auth: X-Oracle-Secret header on all endpoints.
"""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Path, Query
from fastapi.responses import JSONResponse

import cache
import config
from engines import (flow_engine, fundamental_engine, gamma_engine,
                     historical_engine, macro_engine, news_engine,
                     price_engine, vol_engine)
from intelligence import coherence, patterns
from models import (ContextPacket, CycleIntelligence, HealthCheck,
                    NewsData, PrefetchRequest, PrefetchResponse)
import sys as _sys_wd
_sys_wd.path.insert(0, "/Users/ahmedsadek/nexus")
from shared.watchdog import Watchdog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_ORACLE_START_TIME = datetime.utcnow().isoformat() + "Z"
import hashlib as _hashlib, os as _os, glob as _glob

def _compute_module_hash() -> str:
    """Hash all *.py files in this service directory (excluding __pycache__).
    Returns 8-char hex digest. FLAW 1 fix: full module fingerprint, not just main.py.
    """
    _svc_dir = _os.path.dirname(_os.path.abspath(__file__))
    _files = sorted(
        f for f in _glob.glob(_os.path.join(_svc_dir, "*.py"))
        if "__pycache__" not in f
    )
    _h = _hashlib.md5()
    for _f in _files:
        try:
            with open(_f, "rb") as _fh:
                _h.update(_fh.read())
        except Exception:
            pass
    return _h.hexdigest()[:8]

_CODE_HASH = _compute_module_hash()

# RESILIENCE_SPEC_v2 Block 2: Service mode gate
# GENESIS-FIX-ORACLE-STANDBY-001 2026-05-01: Oracle had no STANDBY mode.
# During the 840-restart crash loop, agents called /oracle/context and got empty
# responses because the cache was always 0 after each restart. This gate blocks
# context requests until the cache is warm enough to serve real data.
import threading as _threading
_ORACLE_SERVICE_MODE: str = "warming"  # "warming" | "active"
_oracle_mode_lock = _threading.Lock()
MIN_WARM_TICKERS: int = 20  # Must have this many tickers warm before serving


def _set_oracle_active() -> None:
    global _ORACLE_SERVICE_MODE
    with _oracle_mode_lock:
        _ORACLE_SERVICE_MODE = "active"
    logger.info("Block 2: ORACLE mode → ACTIVE (%d warm tickers)", cache.warm_count())


_nns_watchdog = Watchdog("oracle")

# OMNI M3 fix: replace deprecated @app.on_event("startup") with lifespan context manager.
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown."""
    logger.info("ORACLE starting up on port %d", config.ORACLE_PORT)
    cache.init_db()
    asyncio.create_task(_refresh_edgar_map())
    asyncio.create_task(_startup_warmup())  # GENESIS-FIX-STARTUP-WARMUP-001: pre-warm on every restart
    asyncio.create_task(_startup_self_test())  # DATA INTEGRITY: verify engines return real data before serving
    asyncio.create_task(_warmup_gate_monitor())  # Block 2: monitor warmup, flip to ACTIVE when ready
    logger.info("ORACLE ready (warming)")
    _nns_watchdog.start()
    yield
    logger.info("ORACLE shutting down")


app = FastAPI(
    title="ORACLE Intelligence Hub",
    description="Single intelligence gateway for the Nexus trading system.",
    version="1.0.0",
    lifespan=lifespan,
)


async def _refresh_edgar_map() -> None:
    """Refresh the ticker→CIK map from SEC EDGAR in the background."""
    from clients import edgar_client
    loop = asyncio.get_event_loop()
    count = await loop.run_in_executor(None, edgar_client.refresh_ticker_cik_map)
    logger.info("EDGAR CIK map refreshed: %d tickers", count)


async def _startup_warmup() -> None:
    """GENESIS-FIX-STARTUP-WARMUP-001 2026-05-01: Pre-warm Oracle cache on every startup.

    Oracle's cache is in-memory and wipes on every restart. Before this fix, Oracle
    served empty context to agents until tickers were requested one-by-one — producing
    fallback scores and zero concordances. This task fetches the Axiom pool immediately
    on startup and warms all tickers so agents get real context from the first window.
    """
    import requests as _requests
    import os as _os

    await asyncio.sleep(2)  # Let the server fully initialize first

    # Fetch Axiom pool tickers
    axiom_url = _os.getenv("AXIOM_URL", "http://localhost:8001")
    axiom_secret = _os.getenv("AXIOM_SECRET", "")
    try:
        resp = _requests.get(
            f"{axiom_url}/pool",
            headers={"X-Axiom-Secret": axiom_secret},
            timeout=5,
        )
        body = resp.json()
        pool_tickers = body.get("pool", body.get("tickers", []))[:50] if resp.ok else []
    except Exception as e:
        logger.warning("Startup warmup: could not fetch Axiom pool: %s", e)
        pool_tickers = []

    # Supplement with a core set of high-frequency tickers in case Axiom is also restarting
    CORE_TICKERS = [
        "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "JPM",
        "GS", "CVX", "XOM", "PG", "KO", "JNJ", "UNH", "WMT", "COP", "CMS",
    ]
    all_tickers = list(dict.fromkeys(pool_tickers + CORE_TICKERS))  # deduplicate, pool first

    if not all_tickers:
        logger.warning("Startup warmup: no tickers to warm")
        return

    logger.info("Startup warmup: pre-fetching %d tickers (pool=%d + core=%d)",
                len(all_tickers), len(pool_tickers), len(CORE_TICKERS))

    # Warm all tickers concurrently
    tasks = [_assemble_full_packet(t) for t in all_tickers]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    warmed = sum(1 for r in results if not isinstance(r, Exception) and r is not None)
    logger.info("Startup warmup complete: %d/%d tickers warmed", warmed, len(all_tickers))


async def _warmup_gate_monitor() -> None:
    """Block 2: Monitor warmup progress and flip ORACLE to ACTIVE when cache is warm.

    GENESIS-FIX-ORACLE-STANDBY-001 2026-05-01: Prevents agents from receiving empty
    context during the post-restart warming period. Context endpoint returns 503
    with retry hint while in 'warming' state. Flips to 'active' once MIN_WARM_TICKERS
    are in cache, then exits (one-shot task).
    """
    import time as _time
    for _ in range(60):  # Check every 2s for up to 2 minutes
        await asyncio.sleep(2)
        try:
            warm = cache.warm_count()
        except Exception:
            warm = 0
        if warm >= MIN_WARM_TICKERS:
            _set_oracle_active()
            return
    # If still not warm after 2 minutes, activate anyway (degraded but available)
    _set_oracle_active()
    logger.warning("Block 2: ORACLE warmup gate timed out — activating in degraded state")


async def _startup_self_test() -> None:
    """
    Oracle startup self-test — verifies all data engines return real values.

    Runs after warmup. Tests 3 liquid tickers. If IVR, RSI, or revenue_growth
    are all null across test tickers → logs CRITICAL and alerts Ahmed.
    Oracle continues to serve (does not block), but agents are warned.

    Catches the exact failure pattern from 2026-05-01:
      - ORATS stub returning None for IVR
      - Polygon client missing technicals (RSI always null)
      - Fundamental engine missing revenue_growth_yoy field
    """
    await asyncio.sleep(15)  # Let warmup fetch data first

    TEST_TICKERS = ["AAPL", "JPM", "MSFT"]
    failures = []

    for ticker in TEST_TICKERS:
        try:
            packet = await _assemble_full_packet(ticker)
            if packet is None:
                failures.append(f"{ticker}: no context returned")
                continue

            vol   = getattr(packet, "vol",   None)
            price = getattr(packet, "price", None)
            fund  = getattr(packet, "fundamental", None)

            ivr     = getattr(vol,   "iv_rank",          None) if vol   else None
            rsi     = getattr(price, "rsi_14",            None) if price else None
            rev_gr  = getattr(fund,  "revenue_growth_yoy",None) if fund  else None

            ticker_fails = []
            if ivr is None:    ticker_fails.append("vol.iv_rank=None (ORATS broken?)")
            if rsi is None:    ticker_fails.append("price.rsi_14=None (Polygon bars broken?)")
            if rev_gr is None: ticker_fails.append("fundamental.revenue_growth_yoy=None (Polygon financials broken?)")

            if ticker_fails:
                failures.append(f"{ticker}: {'; '.join(ticker_fails)}")
            else:
                logger.info(
                    "Oracle self-test %s: IVR=%.1f RSI=%.1f rev_growth=%.1f%% ✔️",
                    ticker, ivr, rsi, rev_gr,
                )
        except Exception as e:
            failures.append(f"{ticker}: exception {e}")

    if failures:
        fail_str = "\n".join(f"• {f}" for f in failures)
        logger.critical(
            "ORACLE SELF-TEST FAILED — data engines returning nulls:\n%s\n"
            "Agents will receive fallback scores. Run oracle_data_preflight.py.",
            fail_str,
        )
        try:
            import requests as _req
            OMNI_BOT = "7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c"
            msg = (
                f"🔴 <b>ORACLE SELF-TEST FAILED</b>\n"
                f"Data engines returning nulls on startup:\n{fail_str}\n"
                f"Agents will score on defaults. Pipeline integrity compromised."
            )
            for chat_id in ["8573754783", "-1003954790884"]:
                _req.post(
                    f"https://api.telegram.org/bot{OMNI_BOT}/sendMessage",
                    json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                    timeout=5,
                )
        except Exception as _te:
            logger.warning("Self-test alert failed: %s", _te)
    else:
        logger.info("Oracle self-test PASSED — all engines returning real data ✔️")


# ── Auth Dependency ─────────────────────────────────────────────

def _check_auth(secret: Optional[str]) -> None:
    """Verify X-Oracle-Secret header.

    Cipher P2-3/P2-4 fix: constant-time comparison (timing-safe).
    Cipher P2-9 fix: returns 403 to match system-wide convention (all other
    services return 403 on auth failure; ORACLE was the only exception at 401).
    """
    import secrets as _sec
    if not secret or not _sec.compare_digest(secret, config.ORACLE_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")


# ── Context Packet Assembly ───────────────────────────────────────────────────

async def _assemble_full_packet(ticker: str) -> ContextPacket:
    """
    Assemble a full 7-engine context packet for a ticker.
    All engines run concurrently. DeepSeek coherence runs after.
    """
    loop = asyncio.get_event_loop()
    ts = datetime.now(tz=timezone.utc).isoformat()

    # Fetch macro first (system-wide, likely cached)
    macro_data, macro_fresh = await loop.run_in_executor(None, macro_engine.fetch)
    regime = macro_data.regime if macro_data else "UNKNOWN"

    # Run remaining 6 engines concurrently
    results = await asyncio.gather(
        loop.run_in_executor(None, price_engine.fetch, ticker, "full"),
        loop.run_in_executor(None, vol_engine.fetch, ticker, "full"),
        loop.run_in_executor(None, flow_engine.fetch, ticker, "full"),
        loop.run_in_executor(None, gamma_engine.fetch, ticker, "full"),
        loop.run_in_executor(None, fundamental_engine.fetch, ticker, "full"),
        loop.run_in_executor(None, historical_engine.fetch, ticker, regime, "any", "full"),
        loop.run_in_executor(None, news_engine.fetch, ticker, [ticker], "full"),
        return_exceptions=True,
    )

    price_data, price_fresh = _unpack(results[0])
    vol_data, vol_fresh = _unpack(results[1])
    flow_data, flow_fresh = _unpack(results[2])
    gamma_data, gamma_fresh = _unpack(results[3])
    fund_data, fund_fresh = _unpack(results[4])
    hist_data, hist_fresh = _unpack(results[5])
    news_raw, news_fresh = _unpack(results[6])

    news_data = NewsData(**news_raw) if isinstance(news_raw, dict) else None

    packet = ContextPacket(
        ticker=ticker.upper(),
        card_type="full",
        timestamp=ts,
        price=price_data,
        price_freshness=price_fresh,
        vol=vol_data,
        vol_freshness=vol_fresh,
        flow=flow_data,
        flow_freshness=flow_fresh,
        gamma=gamma_data,
        gamma_freshness=gamma_fresh,
        macro=macro_data,
        macro_freshness=macro_fresh,
        fundamental=fund_data,
        fundamental_freshness=fund_fresh,
        historical=hist_data,
        historical_freshness=hist_fresh,
        news=news_data,
        news_freshness=news_fresh,
    )

    # DeepSeek coherence (runs after assembly)
    coherence_result = await loop.run_in_executor(
        None, coherence.score, ticker, packet.model_dump()
    )
    packet.coherence = coherence_result

    return packet


async def _assemble_preliminary_card(ticker: str) -> Dict[str, Any]:
    """
    Assemble a lightweight preliminary card (3 engines: price, vol rank, fundamental-earnings).
    """
    loop = asyncio.get_event_loop()
    ts = datetime.now(tz=timezone.utc).isoformat()

    macro_data, _ = await loop.run_in_executor(None, macro_engine.fetch)

    results = await asyncio.gather(
        loop.run_in_executor(None, price_engine.fetch, ticker, "preliminary"),
        loop.run_in_executor(None, vol_engine.fetch, ticker, "preliminary"),
        loop.run_in_executor(None, fundamental_engine.fetch, ticker, "preliminary"),
        return_exceptions=True,
    )

    price_data, _ = _unpack(results[0])
    vol_data, _ = _unpack(results[1])
    fund_data, _ = _unpack(results[2])

    iv_rank = vol_data.iv_rank if vol_data else None
    days_to_earnings = fund_data.days_to_earnings if fund_data else None
    regime = macro_data.regime if macro_data else "UNKNOWN"

    tier2_signal = _compute_tier2_signal(iv_rank, days_to_earnings, regime)

    return {
        "ticker": ticker.upper(),
        "card_type": "preliminary",
        "timestamp": ts,
        "price": price_data.last if price_data else None,
        "iv_rank": iv_rank,
        "days_to_earnings": days_to_earnings,
        "earnings_clear": days_to_earnings is None or days_to_earnings > 14,
        "macro_regime": regime,
        "macro_vix": macro_data.vix if macro_data else None,
        "tier2_signal": tier2_signal,
    }


def _compute_tier2_signal(iv_rank: Optional[float], days_to_earnings: Optional[int],
                           regime: str) -> str:
    """Compute Tier 2 advancement signal for Axiom."""
    if days_to_earnings is not None and days_to_earnings < 14:
        return "HOLD"
    if regime in ("CRISIS",):
        return "HOLD"
    if iv_rank is not None and iv_rank < 20:
        return "BORDERLINE"
    if iv_rank is not None and iv_rank >= 30:
        return "ADVANCE"
    return "BORDERLINE"


def _unpack(result: Any) -> tuple:
    """Safely unpack engine result, handling exceptions."""
    if isinstance(result, Exception):
        logger.error("Engine exception: %s", result)
        return None, config.FRESHNESS_UNAVAILABLE
    if isinstance(result, tuple) and len(result) == 2:
        return result
    return None, config.FRESHNESS_UNAVAILABLE


# ── API Endpoints ─────────────────────────────────────────────────────────────

@app.get("/oracle/context/{ticker}", response_model=ContextPacket)
async def get_context(
    ticker: str = Path(..., description="Stock ticker symbol"),
    x_oracle_secret: Optional[str] = Header(None, alias="X-Oracle-Secret"),
) -> ContextPacket:
    """
    Return full 7-engine context packet for a ticker.
    Cache hit returns in <10ms. Cache miss takes up to 3 seconds.
    """
    _check_auth(x_oracle_secret)
    # Block 2: STANDBY gate — reject context requests while cache is warming.
    # This prevents agents from receiving empty context and producing fallback scores.
    with _oracle_mode_lock:
        _mode = _ORACLE_SERVICE_MODE
    if _mode == "warming":
        warm = cache.warm_count()
        raise HTTPException(
            status_code=503,
            detail=f"ORACLE warming ({warm}/{MIN_WARM_TICKERS} tickers ready). Retry in 5-10s."
        )
    # Adversarial fix #3: strict ticker validation — only A-Z, 0-9 allowed.
    # Prevents cache key poisoning via injected delimiters (e.g. "AAPL:fundamental").
    import re as _re
    ticker = ticker.upper().strip()
    if not _re.match(r'^[A-Z0-9]{1,10}$', ticker):
        raise HTTPException(status_code=422, detail=f"Invalid ticker: '{ticker}'")
    packet = await _assemble_full_packet(ticker)
    return packet


@app.get("/oracle/context/{ticker}/{engine}")
async def get_engine(
    ticker: str = Path(..., description="Stock ticker symbol"),
    engine: str = Path(..., description="Engine: price|vol|flow|gamma|macro|fundamental|historical"),
    x_oracle_secret: Optional[str] = Header(None, alias="X-Oracle-Secret"),
) -> JSONResponse:
    """Return a single engine's data for a ticker."""
    _check_auth(x_oracle_secret)

    valid_engines = config.VALID_ENGINES | {"news"}
    if engine not in valid_engines:
        raise HTTPException(status_code=400,
                            detail=f"Invalid engine. Valid: {sorted(config.VALID_ENGINES)}")

    # OMNI C2 fix: validate ticker to prevent cache key poisoning via injected
    # delimiters (e.g. "AAPL:fundamental", "../etc"). Mirrors main endpoint validation.
    import re as _re
    ticker = ticker.upper().strip()
    if not _re.match(r'^[A-Z0-9]{1,10}$', ticker):
        raise HTTPException(status_code=422, detail=f"Invalid ticker: '{ticker}'")

    loop = asyncio.get_event_loop()

    # OMNI C3 fix: pass card_type="full" to all engines that require it.
    # Original code omitted the second positional arg — every call that
    # hit a non-price/macro engine raised TypeError and returned 503.
    engine_map = {
        "price":       lambda: loop.run_in_executor(None, price_engine.fetch, ticker),
        "vol":         lambda: loop.run_in_executor(None, vol_engine.fetch, ticker, "full"),
        "flow":        lambda: loop.run_in_executor(None, flow_engine.fetch, ticker, "full"),
        "gamma":       lambda: loop.run_in_executor(None, gamma_engine.fetch, ticker, "full"),
        "macro":       lambda: loop.run_in_executor(None, macro_engine.fetch),
        "fundamental": lambda: loop.run_in_executor(None, fundamental_engine.fetch, ticker, "full"),
        "historical":  lambda: loop.run_in_executor(None, historical_engine.fetch, ticker, "UNKNOWN"),
        "news":        lambda: loop.run_in_executor(None, news_engine.fetch, ticker, [ticker]),
    }

    try:
        data, freshness = await engine_map[engine]()
        return JSONResponse({
            "ticker": ticker,
            "engine": engine,
            "data": data.model_dump() if data else None,
            "data_freshness": freshness,
        })
    except Exception as e:
        logger.error("Engine %s error for %s: %s", engine, ticker, e)
        return JSONResponse({"error": "Engine fetch failed"}, status_code=503)


@app.post("/oracle/prefetch", response_model=PrefetchResponse)
async def prefetch(
    request: PrefetchRequest,
    x_oracle_secret: Optional[str] = Header(None, alias="X-Oracle-Secret"),
) -> PrefetchResponse:
    """
    Bulk pre-warm context packets for a list of tickers.
    Returns 202 immediately. Pre-warm runs in background.
    """
    _check_auth(x_oracle_secret)

    if request.tier not in ("preliminary", "full"):
        raise HTTPException(status_code=400, detail="tier must be 'preliminary' or 'full'")

    # S-3 fix (2026-05-02): sanitize ticker input — reject SQL injection, path traversal,
    # oversized strings. Only accept valid ticker format: 1-6 uppercase alphanumeric + dot.
    import re as _re
    VALID_TICKER = _re.compile(r'^[A-Z0-9\.]{1,6}$')
    tickers = []
    for raw in request.tickers[:100]:
        t = str(raw).upper().strip()[:10]  # hard truncate before any processing
        if VALID_TICKER.match(t):
            tickers.append(t)
        else:
            logger.warning("Prefetch: rejected invalid ticker '%s'", raw)
    estimated_ms = len(tickers) * (1000 if request.tier == "full" else 400)

    asyncio.create_task(_run_prefetch(tickers, request.tier))

    return PrefetchResponse(
        accepted=True,
        ticker_count=len(tickers),
        tier=request.tier,
        estimated_ready_ms=estimated_ms,
    )


async def _run_prefetch(tickers: list, tier: str) -> None:
    """Background task: pre-warm all tickers concurrently."""
    if tier == "preliminary":
        tasks = [_assemble_preliminary_card(t) for t in tickers]
    else:
        tasks = [_assemble_full_packet(t) for t in tickers]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = sum(1 for r in results if isinstance(r, Exception))
    logger.info("Prefetch complete: %d/%d tickers (%s tier), %d errors",
                len(tickers) - errors, len(tickers), tier, errors)


@app.get("/oracle/macro")
async def get_macro(
    x_oracle_secret: Optional[str] = Header(None, alias="X-Oracle-Secret"),
) -> JSONResponse:
    """Return current system-wide macro packet."""
    _check_auth(x_oracle_secret)
    loop = asyncio.get_event_loop()
    data, freshness = await loop.run_in_executor(None, macro_engine.fetch)
    return JSONResponse({
        "macro": data.model_dump() if data else None,
        "data_freshness": freshness,
    })


@app.get("/oracle/intelligence/cycle", response_model=CycleIntelligence)
async def get_cycle_intelligence(
    x_oracle_secret: Optional[str] = Header(None, alias="X-Oracle-Secret"),
) -> CycleIntelligence:
    """
    Return cycle-level cross-ticker pattern detection.
    
    Detects echo chamber risks and cross-ticker patterns for the current cycle.
    Used by Axiom during pool refresh cycles to assess synthesis quality.
    
    Returns:
        CycleIntelligence with patterns_detected and echo_chamber_risk_tickers.
    """
    _check_auth(x_oracle_secret)
    
    # Generate cycle_id based on current 10-minute window
    import time as _time
    import requests as _requests
    cycle_time_unix = int(_time.time() / 600) * 600  # Round down to nearest 10-min boundary
    cycle_id = datetime.fromtimestamp(cycle_time_unix, tz=timezone.utc).isoformat()
    
    # Get current pool tickers from Axiom
    tickers = []
    try:
        axiom_url = config.AXIOM_URL if hasattr(config, 'AXIOM_URL') else "http://localhost:8001"
        axiom_secret = config.AXIOM_SECRET if hasattr(config, 'AXIOM_SECRET') else ""
        resp = _requests.get(
            f"{axiom_url}/pool",
            headers={"X-Axiom-Secret": axiom_secret},
            timeout=3,
        )
        if resp.ok:
            body = resp.json()
            tickers = body.get("pool", body.get("tickers", []))[:50]
    except Exception as e:
        logger.warning("Cycle intelligence: could not fetch Axiom pool: %s", e)
        # Fallback: use a default set of liquid tickers
        tickers = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]
    
    if not tickers:
        # Return empty intelligence if no tickers available
        return CycleIntelligence(
            cycle_id=cycle_id,
            cycle_time=datetime.now(tz=timezone.utc).isoformat(),
            tickers_in_pool=[],
            patterns_detected=[],
            echo_chamber_risk_tickers=[],
        )
    
    # Build full context cards for warm/cached tickers
    loop = asyncio.get_event_loop()
    full_cards = []
    packet_tasks = [_assemble_full_packet(t) for t in tickers]
    packets = await asyncio.gather(*packet_tasks, return_exceptions=True)
    
    for packet in packets:
        if not isinstance(packet, Exception) and packet is not None:
            full_cards.append(packet.model_dump())
    
    # Run pattern detection
    try:
        cycle_intel = await loop.run_in_executor(
            None, patterns.detect, cycle_id, full_cards
        )
    except Exception as e:
        logger.error("Cycle intelligence pattern detection failed: %s", e)
        cycle_intel = None
    
    if cycle_intel is None:
        # Fallback if pattern detection fails
        cycle_intel = CycleIntelligence(
            cycle_id=cycle_id,
            cycle_time=datetime.now(tz=timezone.utc).isoformat(),
            tickers_in_pool=tickers,
            patterns_detected=[],
            echo_chamber_risk_tickers=[],
        )
    
    return cycle_intel


@app.get("/oracle/cycle", response_model=CycleIntelligence)
async def get_cycle_intelligence_alias(
    x_oracle_secret: Optional[str] = Header(None, alias="X-Oracle-Secret"),
) -> CycleIntelligence:
    """
    Alias for /oracle/intelligence/cycle.
    
    GENESIS-FIX-ORACLE-CYCLE-ALIAS: Axiom and other agents call /oracle/cycle
    but the canonical endpoint is /oracle/intelligence/cycle. This alias maintains
    backward compatibility without duplicating logic.
    """
    return await get_cycle_intelligence(x_oracle_secret)


@app.get("/ping")
async def ping() -> dict:
    """Unauthenticated liveness probe for watchdog and load balancers.

    Cipher P2-8: intentionally unauthenticated. Guardian Angel calls this
    endpoint without credentials by design (Block 9). Acceptable for localhost-only
    deployment — leaks only service name and port (non-sensitive).
    """
    return {"status": "ok", "service": "oracle"}


@app.get("/health")
async def health_alias() -> dict:
    """Alias for /oracle/health — standard health check path for Guardian Angel.

    Cipher P2-8: intentionally unauthenticated. Same rationale as /ping.
    The authenticated /oracle/health endpoint returns full engine/stub status.
    """
    hit_rate = cache.hit_rate()
    warm     = cache.warm_count()
    return {
        "status":          "healthy" if hit_rate >= 0.5 or warm > 0 else "degraded",
        "service":         "oracle",
        "version":         "1.0.0",
        "port":            8007,
        "cache_hit_rate":  round(hit_rate, 3),
        "cache_warm_tickers": warm,
        "engines":         8,
        "uptime_since":    _ORACLE_START_TIME,
        "code_hash":       _CODE_HASH,
        "stale_deploy":    (not _os.path.exists("/tmp/nexus_deploy_in_progress")) and _CODE_HASH != _compute_module_hash(),
    }


@app.get("/oracle/health", response_model=HealthCheck)
async def health(
    x_oracle_secret: Optional[str] = Header(None, alias="X-Oracle-Secret"),
) -> HealthCheck:
    """Return platform statuses, cache hit rate, and service health."""
    _check_auth(x_oracle_secret)

    hit_rate = cache.hit_rate()
    warm = cache.warm_count()

    # Determine overall status — Block 2: expose service_mode so GA can route correctly
    with _oracle_mode_lock:
        _mode = _ORACLE_SERVICE_MODE
    status = "healthy"
    if _mode == "warming":
        status = "warming"  # Block 2: GA sees this and does NOT attempt heals
    elif hit_rate < 0.5 and warm == 0:
        status = "degraded"

    return HealthCheck(
        status=status,
        engines={
            "price": {"status": "healthy", "note": "Polygon + yfinance fallback"},
            "vol": {"status": "healthy", "note": "ORATS primary + Polygon IV calc fallback"},
            "flow": {"status": "healthy", "note": "Polygon options chain P/C ratio + Unusual Whales stub"},
            "gamma": {"status": "healthy", "note": "Polygon options chain GEX calculation"},
            "macro": {"status": "healthy", "note": "FRED live + Trading Economics live (indicators)"},
            "fundamental": {"status": "healthy", "note": "Alpha Vantage + EDGAR live"},
            "historical": {"status": "healthy", "note": "AILS (non-blocking)"},
            "news": {"status": "healthy", "note": "Benzinga Cloud API — live"},
        },
        cache={
            "hit_rate_1h": round(hit_rate, 3),
            "warm_tickers": warm,
        },
        intelligence_layer={
            "deepseek": "healthy",
            "gemini": "healthy",
        },
    )


@app.get("/oracle/cost-report")
async def cost_report(
    x_oracle_secret: Optional[str] = Header(None, alias="X-Oracle-Secret"),
) -> JSONResponse:
    """Return API call counts by platform for today."""
    _check_auth(x_oracle_secret)
    try:
        conn = sqlite3.connect(config.ORACLE_DB_PATH)
        rows = conn.execute(
            """SELECT platform, COUNT(*) as calls,
               SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as successes,
               AVG(latency_ms) as avg_latency
               FROM api_calls
               WHERE date(called_at) = date('now')
               GROUP BY platform
               ORDER BY calls DESC"""
        ).fetchall()
        conn.close()
        return JSONResponse({
            "date": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
            "by_platform": [
                {"platform": r[0], "calls": r[1],
                 "successes": r[2], "avg_latency_ms": round(r[3] or 0, 1)}
                for r in rows
            ],
        })
    except sqlite3.Error as e:
        logger.error("Cost report DB error: %s", e)
        return JSONResponse({"error": "Database error"}, status_code=503)


@app.get("/oracle/cache-status")
async def cache_status(
    x_oracle_secret: Optional[str] = Header(None, alias="X-Oracle-Secret"),
) -> JSONResponse:
    """Return cache inventory and hit rate."""
    _check_auth(x_oracle_secret)
    return JSONResponse({
        "hit_rate": round(cache.hit_rate(), 3),
        "warm_entries": cache.warm_count(),
    })


# ============================================================================
# MOCK ENDPOINTS — Resilience Framework
# ============================================================================

from oracle.mock_endpoints import (
    SimulateDownRequest,
    SimulateDownResponse,
    SimulateUpRequest,
    SimulateUpResponse,
    simulate_down,
    simulate_up,
    is_simulating_down,
)


@app.post("/mock/simulate/down", response_model=SimulateDownResponse, status_code=200)
def mock_simulate_down(
    request: SimulateDownRequest,
    x_oracle_secret: str = Header(default=""),
) -> SimulateDownResponse:
    """
    POST /mock/simulate/down — Simulate Oracle service unavailable.

    Scenario 5: Test fallback data source when Oracle is down.
    """
    _check_auth(x_oracle_secret)
    return simulate_down(request)


@app.post("/mock/simulate/up", response_model=SimulateUpResponse, status_code=200)
def mock_simulate_up(
    request: SimulateUpRequest,
    x_oracle_secret: str = Header(default=""),
) -> SimulateUpResponse:
    """
    POST /mock/simulate/up — Resume normal Oracle operation.

    Scenario 5 cleanup: Return Oracle to operational state.
    """
    _check_auth(x_oracle_secret)
    return simulate_up()


# Mock simulation endpoints available via /mock/down and /mock/up
# (No additional function wrapping required — mock state is checked per-endpoint)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.ORACLE_HOST, port=config.ORACLE_PORT, reload=False)
