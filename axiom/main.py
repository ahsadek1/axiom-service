"""
main.py — Axiom Service FastAPI Entry Point

ARCHITECTURE:
  - app_state dict at MODULE LEVEL — /health returns 200 immediately
  - asyncio lifespan manages scheduler start/stop
  - /health NEVER depends on scheduler or pool state — always 200
  - All secrets validated at import time — crash fast if missing

Axiom Service — Nexus Trading System
Port: 8001 (local), $PORT (Railway)
"""

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator

import pytz
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import load_settings

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("axiom.main")

ET = pytz.timezone("America/New_York")

# ── Load settings at module level — crash fast if env vars missing ────────────
settings = load_settings()

# ── Thread safety for app_state mutations (adversarial fix #1) ───────────────
# Scheduler jobs and HTTP handlers both read/write app_state concurrently.
# All compound updates (pool refresh, regime update, window_id advance) must
# acquire this lock to prevent partial reads during mid-update state.
_state_lock = threading.Lock()

# ── Module-level state — created BEFORE FastAPI app ───────────────────────────
app_state: dict = {
    "settings":                  settings,
    "pool":                      [],
    "anchor_stocks":             [],
    "regime":                    None,
    "window_id":                 None,
    "last_vix":                  None,
    "tier2_consecutive_failures": 0,
    "deepseek_api_key":          os.getenv("DEEPSEEK_API_KEY", ""),
    "start_time":                datetime.now(ET).isoformat(),
    "status":                    "starting",
}


# ── Helper ────────────────────────────────────────────────────────────────────

def current_window_id() -> str:
    """
    Generate the current 15-minute window ID in format 'YYYY-MM-DD-HHMM'.

    Returns:
        Window ID string rounded to the current 15-minute boundary.
    """
    now  = datetime.now(ET)
    mins = (now.minute // 15) * 15
    return now.strftime(f"%Y-%m-%d-%H{mins:02d}")


def verify_secret(x_axiom_secret: str) -> None:
    """
    Verify the X-Axiom-Secret header matches the configured secret.

    Args:
        x_axiom_secret: Value from X-Axiom-Secret header.

    Raises:
        HTTPException: 403 if secret is missing or invalid.
    """
    import secrets as _sec
    if not x_axiom_secret or not _sec.compare_digest(x_axiom_secret, settings.axiom_secret):
        raise HTTPException(status_code=403, detail="Forbidden")


# ── Lifespan — start/stop scheduler ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan — initialize DB and start scheduler on startup."""
    from database import init_db
    from scheduler import create_scheduler

    logger.info("Axiom service starting...")

    # Initialize database
    try:
        init_db(settings.axiom_db_path)
        logger.info("Database initialized at %s", settings.axiom_db_path)
    except Exception as e:
        logger.error("Database initialization failed: %s", e)
        raise

    # Start scheduler
    scheduler = create_scheduler(app_state)
    scheduler.start()
    app_state["scheduler"] = scheduler
    app_state["status"]    = "ready"

    logger.info("Axiom service ready — scheduler running %d jobs", len(scheduler.get_jobs()))

    yield

    # Shutdown
    logger.info("Axiom service shutting down...")
    if scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info("Axiom service stopped")


# ── FastAPI App ───────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "Axiom Service",
    description = "Nexus Trading System — Intelligent pool curator and risk gate",
    version     = "4.0.0",
    lifespan    = lifespan,
)


# ── Request Models ────────────────────────────────────────────────────────────

class AssessRequest(BaseModel):
    ticker: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> JSONResponse:
    """
    Health check endpoint. Always returns 200.
    Never exposes internal errors or state details.
    """
    regime   = app_state.get("regime")
    pool_size = len(app_state.get("pool", []))

    return JSONResponse({
        "status":          "healthy",
        "service":         "axiom",
        "version":         "4.0.0",
        "pool_size":       pool_size,
        "regime":          regime.classification if regime else "unknown",
        "vix":             regime.vix if regime else None,
        "submissions_open": _is_submissions_open(),
        "uptime_since":    app_state.get("start_time"),
    })


@app.get("/pool")
def get_pool(x_axiom_secret: str = Header(default="")) -> JSONResponse:
    """
    Return the current live candidate pool.

    Returns:
        Pool payload with tickers, count, window_id, and regime.
    """
    verify_secret(x_axiom_secret)

    # Adversarial fix #1: snapshot app_state under lock to avoid partial reads
    # during concurrent pool refresh from scheduler.
    with _state_lock:
        regime    = app_state.get("regime")
        pool      = list(app_state.get("pool", []))   # copy to avoid mutation during response
        window_id = app_state.get("window_id") or current_window_id()

    return JSONResponse({
        "pool":         pool,
        "count":        len(pool),
        "window_id":    window_id,
        "updated_at":   datetime.now(ET).isoformat(),
        "market_open":  _is_submissions_open(),
        "regime":       regime.to_dict() if regime else {},
    })


@app.get("/regime")
def get_regime(x_axiom_secret: str = Header(default="")) -> JSONResponse:
    """
    Return the current VIX regime classification.

    Returns:
        Regime dict with classification and all allowed flags.
    """
    verify_secret(x_axiom_secret)

    regime = app_state.get("regime")
    if regime is None:
        return JSONResponse({"classification": "UNKNOWN", "vix": None, "error": "Regime not yet loaded"})

    return JSONResponse(regime.to_dict())


@app.get("/anchor")
def get_anchor(x_axiom_secret: str = Header(default="")) -> JSONResponse:
    """
    Return the current Tier 1 anchor stocks.

    Returns:
        List of anchor stock tickers from the latest Tier 1 run.
    """
    verify_secret(x_axiom_secret)

    anchors = app_state.get("anchor_stocks", [])
    return JSONResponse({
        "anchor_stocks": anchors,
        "count":         len(anchors),
        "as_of":         datetime.now(ET).strftime("%Y-%m-%d"),
    })


@app.post("/assess")
def assess_stock(
    body: AssessRequest,
    x_axiom_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Run a per-stock risk assessment on demand.

    Called by OMNI during synthesis to get Axiom's risk evaluation
    for a specific ticker.

    Args:
        body: AssessRequest with ticker field.

    Returns:
        Risk assessment with score, sizing_mult, hard_stops, and concerns.
    """
    verify_secret(x_axiom_secret)

    ticker    = body.ticker.upper().strip()
    window_id = app_state.get("window_id") or current_window_id()
    pool      = app_state.get("pool", [])
    regime    = app_state.get("regime")

    # Cipher Finding 5 fix (startup race): if regime has not yet been loaded by
    # the scheduler (None at startup), return a hard stop — fail-safe, not fail-open.
    # The INCIDENT-9 fix handles Axiom being unreachable; this handles Axiom being
    # reachable but in its startup window before the first regime classification.
    if regime is None:
        logger.warning(
            "Axiom /assess called before regime loaded — returning REGIME_NOT_LOADED hard stop"
        )
        return JSONResponse({
            "ticker":         ticker,
            "risk_score":     10.0,
            "sizing_mult":    0.0,
            "hard_stops":     ["REGIME_NOT_LOADED"],
            "critical_flags": [],
            "concern_1":      "Axiom regime not yet loaded — system in startup window",
            "concern_2":      "N/A",
            "concern_3":      "N/A",
            "in_pool":        False,
            "regime":         "UNKNOWN",
            "window_id":      window_id,
            "model":          "axiom-risk-engine-v3",
        })

    # Basic risk scoring
    hard_stops: list[str] = []
    flags:      list[str] = []
    concerns:   list[str] = []
    risk_score  = 2.0
    sizing_mult = 1.0

    # Not in current pool
    if pool and ticker not in pool:
        concerns.append(f"{ticker} not in current Axiom pool — submitted outside pool")
        risk_score += 1.0

    # Regime-based risk
    if regime:
        if regime.classification == "CRISIS":
            hard_stops.append("CRISIS regime — no new entries permitted")
            risk_score = 10.0
            sizing_mult = 0.0
        elif regime.classification in ("HIGH_STRESS", "STRESS"):
            concerns.append(f"Elevated regime ({regime.classification}) — reduced sizing")
            risk_score += 2.0
            sizing_mult = regime.alpha_size_mult

    # Normalize
    risk_score  = min(10.0, max(0.0, risk_score))
    sizing_mult = min(1.0, max(0.0, sizing_mult))

    result = {
        "ticker":         ticker,
        "risk_score":     round(risk_score, 1),
        "sizing_mult":    round(sizing_mult, 2),
        "hard_stops":     hard_stops,
        "critical_flags": flags,
        "concern_1":      concerns[0] if len(concerns) > 0 else "N/A",
        "concern_2":      concerns[1] if len(concerns) > 1 else "N/A",
        "concern_3":      concerns[2] if len(concerns) > 2 else "N/A",
        "in_pool":        ticker in pool,
        "regime":         regime.classification if regime else "UNKNOWN",
        "window_id":      window_id,
        "model":          "axiom-risk-engine-v3",
    }

    # Cache to DB
    try:
        from database import save_risk_assessment
        save_risk_assessment(
            db_path     = settings.axiom_db_path,
            ticker      = ticker,
            window_id   = window_id,
            risk_score  = result["risk_score"],
            sizing_mult = result["sizing_mult"],
            hard_stops  = hard_stops,
            flags       = flags,
            raw_result  = result,
        )
    except Exception as e:
        logger.warning("Failed to cache risk assessment for %s: %s", ticker, e)

    return JSONResponse(result)


@app.get("/oracle/status")
def oracle_status(x_axiom_secret: str = Header(default="")) -> JSONResponse:
    """
    Diagnostic endpoint — ORACLE integration status.

    Returns current ORACLE reachability, last pre-warm and coherence query
    timestamps, and current regime source.
    """
    verify_secret(x_axiom_secret)

    import oracle_client
    reachable = oracle_client.health_check()

    regime = app_state.get("regime")

    return JSONResponse({
        "oracle_reachable":          reachable,
        "oracle_preliminary_warmed": app_state.get("oracle_preliminary_warmed", False),
        "regime_source":             regime.regime_source if regime else "UNKNOWN",
        "composite_score":           regime.composite_score if regime else None,
        "regime_classification":     regime.classification if regime else "UNKNOWN",
        "vix":                       regime.vix if regime else None,
    })


@app.post("/premarket")
def trigger_premarket(x_axiom_secret: str = Header(default="")) -> JSONResponse:
    """
    Manually trigger the pre-market brief generation.

    Useful for testing or if the scheduled run was missed.
    """
    verify_secret(x_axiom_secret)

    from scheduler import _run_premarket_brief
    try:
        _run_premarket_brief(app_state)
        return JSONResponse({"status": "sent"})
    except Exception as e:
        logger.error("Manual premarket trigger failed: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_submissions_open() -> bool:
    """
    Return True if current time is within the submission window (9:15 AM – 3:30 PM ET, Mon-Fri).

    Returns:
        True if submissions are currently accepted.
    """
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    open_time  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_time <= now <= close_time
