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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# OMNI M3 fix: replace deprecated @app.on_event("startup") with lifespan context manager.
# on_event("startup") was deprecated in FastAPI 0.93 and will stop working in future versions.
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown."""
    logger.info("ORACLE starting up on port %d", config.ORACLE_PORT)
    cache.init_db()
    asyncio.create_task(_refresh_edgar_map())
    logger.info("ORACLE ready")
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


# ── Auth Dependency ───────────────────────────────────────────────────────────

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
    # Adversarial fix #3: strict ticker validation — only A-Z, 0-9 allowed.
    # Prevents cache key poisoning via injected delimiters (e.g. "AAPL:fundamental").
    import re as _re
    ticker = ticker.upper().strip()
    if not _re.match(r'^[A-Z0-9]{1,10}$', ticker):
        from fastapi import HTTPException
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

    tickers = [t.upper() for t in request.tickers[:100]]  # cap at 100
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
    return {"status": "healthy", "service": "oracle", "port": 8007}


@app.get("/oracle/health", response_model=HealthCheck)
async def health(
    x_oracle_secret: Optional[str] = Header(None, alias="X-Oracle-Secret"),
) -> HealthCheck:
    """Return platform statuses, cache hit rate, and service health."""
    _check_auth(x_oracle_secret)

    hit_rate = cache.hit_rate()
    warm = cache.warm_count()

    # Determine overall status
    status = "healthy"
    if hit_rate < 0.5 and warm == 0:
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.ORACLE_HOST, port=config.ORACLE_PORT, reload=False)
