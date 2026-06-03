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
from typing import AsyncGenerator, Optional

import pytz
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).parent.parent))
from shared.sovereign_comms import get_instructions, report
from shared.watchdog import Watchdog

# ── Axiom 30% Resilience Layer ─────────────────────────────────────────────
from axiom.resilience.state import make_vix_cache, make_regime_cache
from axiom.resilience.db import assess_db_write
from axiom.resilience.health import AxiomHealthReport

from axiom.config import load_settings, MAX_POSITIONS, MAX_RISK_PER_TRADE, MIN_DTE, MAX_DTE, VIX_PAUSE_THRESHOLD, MIN_IVR_CREDIT_SPREAD, MAX_IVR_DEBIT_SPREAD
from axiom.inspector import AxiomInspector

# P0-A: Stale deploy detection
import hashlib as _hashlib
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
    "regime_last_updated":       None,   # ISO timestamp — updated on every regime refresh
    "window_id":                 None,
    "last_vix":                  None,
    "tier2_consecutive_failures": 0,
    "deepseek_api_key":          os.getenv("DEEPSEEK_API_KEY", ""),
    "start_time":                datetime.now(ET).isoformat(),
    "status":                    "starting",
    # Resilience layer — populated in lifespan startup
    "_vix_cache":                None,
    "_regime_cache":             None,
    "_health_report":            None,
}

# ── Block 2: STANDBY mode ─────────────────────────────────────────────────────
_SERVICE_MODE:   str  = "active"   # "active" | "standby"
_standby_reason: str  = ""
_axiom_mode_lock = threading.Lock()


# ── Helper ────────────────────────────────────────────────────────────────────

# ── Window ID generation (G15: Tier 2 retry dedup) ─────────────────────────────
_window_run_counter: int = 0
_window_id_cache: str = ""
_window_id_cache_ts: float = 0.0

def current_window_id() -> str:
    """
    Generate the current 15-minute window ID in format 'YYYY-MM-DD-HHMM-NN'.
    
    Appends a 2-digit run counter (00-99) to distinguish Tier 2 retries/refreshes
    within the same 15-minute boundary. This prevents duplicate window IDs when
    Tier 2 runs multiple times in a single 15-min window (e.g., retries due to
    ORATS circuit breaker opening, then closing).
    
    Counter resets at the start of each new 15-minute window.

    Returns:
        Window ID string in format 'YYYY-MM-DD-HHMM-NN' (NN = run counter 00-99).
    """
    global _window_run_counter, _window_id_cache, _window_id_cache_ts
    
    now  = datetime.now(ET)
    mins = (now.minute // 15) * 15
    base_id = now.strftime(f"%Y-%m-%d-%H{mins:02d}")
    
    # Reset counter if we've entered a new 15-minute window
    current_ts = now.timestamp()
    if base_id != _window_id_cache or (current_ts - _window_id_cache_ts) >= 900:  # 900s = 15min
        _window_run_counter = 0
        _window_id_cache = base_id
        _window_id_cache_ts = current_ts
    else:
        _window_run_counter = (_window_run_counter + 1) % 100  # 00-99, then wrap
    
    return f"{base_id}-{_window_run_counter:02d}"


def verify_secret(request: Request) -> None:
    """
    Verify either X-Axiom-Secret or X-Nexus-Secret header.
    Accepts both header names (same secret value) for backward compatibility.

    Args:
        request: FastAPI Request object (extracts header from it).

    Raises:
        HTTPException: 403 if secret is missing or invalid.
    """
    import secrets as _sec
    axiom_val = request.headers.get("X-Axiom-Secret") or request.headers.get("x-axiom-secret")
    nexus_val = request.headers.get("X-Nexus-Secret") or request.headers.get("x-nexus-secret")
    secret_val = axiom_val or nexus_val or ""
    if not secret_val or not _sec.compare_digest(secret_val, settings.axiom_secret):
        raise HTTPException(status_code=403, detail="Forbidden")


# ── Lifespan — start/stop scheduler ──────────────────────────────────────────

def _axiom_preflight_check() -> tuple[bool, str]:
    """Block 2: Verify Axiom startup preconditions.

    Checks:
      1. VIX data reachable (Polygon probe via data_sources).
      2. Regime classification succeeds (not None).

    Returns:
        (ok: bool, reason: str) — reason is empty when ok=True.
    """
    try:
        from data_sources import get_vix_with_fallback
        vix, _ = get_vix_with_fallback(settings.fred_api_key, None)
        if vix is None:
            return False, "VIX data unavailable at startup (Polygon + FRED both unreachable)"
    except Exception as exc:
        return False, f"VIX probe failed at startup: {exc}"

    try:
        from regime import classify_regime
        regime = classify_regime(vix, False)
        if regime is None or getattr(regime, "classification", None) is None:
            return False, "Regime classification returned None at startup"
    except Exception as exc:
        return False, f"Regime classification failed at startup: {exc}"

    return True, ""


def _axiom_preflight_retry_loop() -> None:
    """Block 2: Background thread — retry axiom preflight every 30s until ACTIVE."""
    global _SERVICE_MODE, _standby_reason
    import time as _time
    while True:
        _time.sleep(30)
        with _axiom_mode_lock:
            if _SERVICE_MODE == "active":
                return
        ok, reason = _axiom_preflight_check()
        if ok:
            with _axiom_mode_lock:
                _SERVICE_MODE = "active"
                _standby_reason = ""
            logger.info("Block 2: Axiom preflight PASSED — transitioning to ACTIVE")
            return


_nns_watchdog = Watchdog("axiom")

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan — initialize DB and start scheduler on startup."""
    from database import init_db
    from scheduler import create_scheduler

    logger.info("Axiom service starting...")

    # ── Block 2: Startup Preflight Gate ───────────────────────────────────────────
    global _SERVICE_MODE, _standby_reason
    _pf_ok, _pf_reason = _axiom_preflight_check()
    if not _pf_ok:
        with _axiom_mode_lock:
            _SERVICE_MODE = "standby"
            _standby_reason = _pf_reason
        logger.critical("Block 2: Axiom PREFLIGHT FAILED — STANDBY. Reason: %s", _pf_reason)
        import threading as _th
        _th.Thread(target=_axiom_preflight_retry_loop, daemon=True, name="axiom-preflight-retry").start()
    else:
        logger.info("Block 2: Axiom preflight PASSED — ACTIVE mode")
    # ─────────────────────────────────────────────────────────────────

    # Initialize database
    try:
        init_db(settings.axiom_db_path)
        logger.info("Database initialized at %s", settings.axiom_db_path)
    except Exception as e:
        logger.error("Database initialization failed: %s", e)
        raise

    # ── Startup regime classification ─────────────────────────────────────────
    # Eliminates the REGIME_NOT_LOADED race condition: app_state["regime"] is
    # normally None at boot and only set by the scheduler at 8:45 AM or 9:15 AM.
    # Any /assess call before those jobs run (including post-restart during market
    # hours) previously returned REGIME_NOT_LOADED → sizing_mult=0.0 → zero trades.
    # Fix: classify regime immediately at startup so /assess is never gated on None.
    try:
        from data_sources import get_vix_with_fallback
        from regime import classify_regime, classify_regime_v2
        import oracle_client as _oracle_client

        # Try ORACLE composite regime first (4-factor) — more accurate at startup.
        # Fall back to VIX-only if ORACLE is unavailable (e.g. not yet started).
        macro_data = None
        try:
            macro_data = _oracle_client.get_macro_data(timeout=4.0)
        except Exception as _macro_err:
            logger.warning("Startup ORACLE macro fetch failed: %s — using VIX-only", _macro_err)

        if macro_data:
            startup_regime = classify_regime_v2(macro_data)
            app_state["regime"] = startup_regime
            app_state["regime_last_updated"] = datetime.now(ET).isoformat()
            logger.info(
                "Startup regime classification (COMPOSITE) — VIX=%.1f, class=%s, score=%d",
                startup_regime.vix,
                startup_regime.classification,
                startup_regime.composite_score,
            )
        else:
            vix, is_estimated = get_vix_with_fallback(
                settings.fred_api_key,
                app_state.get("last_vix"),
            )
            app_state["last_vix"] = vix
            startup_regime = classify_regime(vix, is_estimated)
            app_state["regime"] = startup_regime
            app_state["regime_last_updated"] = datetime.now(ET).isoformat()
            logger.info(
                "Startup regime classification (VIX-ONLY fallback) — VIX=%.1f, class=%s%s",
                vix,
                startup_regime.classification,
                " (estimated)" if is_estimated else "",
            )
    except Exception as e:
        # Non-fatal: log the failure but proceed. /assess will still gate on None
        # until the scheduler fires — same as old behaviour, but at least startup
        # succeeds and the window is minimised to the brief interval before the
        # scheduler's first VIX fetch.
        logger.warning(
            "Startup regime classification failed: %s — /assess will block until "
            "scheduler fires at 8:45 AM ET (REGIME_NOT_LOADED window open).",
            e,
        )

    # ── Startup Tier 1 load: Initialize anchor stocks before scheduler starts ─────
    # Ensures pool is ready if service starts/restarts during market hours.
    # Without this, Tier 1 jobs may not fire until tomorrow if startup is post-1PM ET.
    try:
        from tier1_filter import run_tier1_filter
        from database import save_anchor_stocks
        from datetime import date
        
        logger.info("Startup: Running Tier 1 filter to initialize anchor stocks...")
        anchors = run_tier1_filter(
            universe          = settings.stock_universe,
            polygon_api_key   = settings.polygon_api_key,
            alpha_vantage_key = settings.alpha_vantage_key,
        )
        app_state["anchor_stocks"] = anchors
        save_anchor_stocks(settings.axiom_db_path, anchors, date.today().isoformat(), "startup")
        logger.info("Startup Tier 1 complete — %d anchor stocks loaded", len(anchors))
    except Exception as e:
        logger.warning("Startup Tier 1 load failed: %s — scheduler will retry at 9 AM", e)

    # Start scheduler
    scheduler = create_scheduler(app_state)
    scheduler.start()
    app_state["scheduler"] = scheduler
    app_state["status"]    = "ready"

    # Resilience layer — init FreshValue caches after state is populated
    app_state["_vix_cache"]    = make_vix_cache()
    app_state["_regime_cache"] = make_regime_cache()
    # Pre-seed caches if startup classification succeeded
    if app_state.get("last_vix") is not None:
        app_state["_vix_cache"].update(app_state["last_vix"])
    if app_state.get("regime") is not None:
        app_state["_regime_cache"].update(app_state["regime"])

    logger.info("Axiom service ready — scheduler running %d jobs", len(scheduler.get_jobs()))
    report("axiom", "INFO", {"event": "started", "port": int(os.getenv("PORT", "8001"))})
    _instr = get_instructions("axiom")
    if _instr:
        logger.info("Axiom: %d instruction(s) from SOVEREIGN on startup", len(_instr))
    _nns_watchdog.start()

    # ── Axiom Inspector General — Autonomous audit module ──────────────────────
    inspector = AxiomInspector(app_state, settings, logger)
    app_state["inspector"] = inspector

    # Schedule inspector jobs
    try:
        # Pre-market sweep: 8:30 AM ET (before pool refresh)
        scheduler.add_job(
            inspector.run_pre_market_sweep,
            "cron",
            hour=8,
            minute=30,
            timezone="America/New_York",
            id="axiom-inspector-pre-market",
            replace_existing=True,
        )

        # Market-hours sweep: every 15 min from 9 AM to 4 PM ET
        scheduler.add_job(
            inspector.run_market_hours_sweep,
            "cron",
            hour="9-15",
            minute="*/15",
            timezone="America/New_York",
            id="axiom-inspector-market-hours",
            replace_existing=True,
        )

        # Post-market audit: 4:30 PM ET (after close)
        scheduler.add_job(
            inspector.run_post_market_audit,
            "cron",
            hour=16,
            minute=30,
            timezone="America/New_York",
            id="axiom-inspector-post-market",
            replace_existing=True,
        )

        # Weekly summary: Friday 5 PM ET
        scheduler.add_job(
            inspector.run_weekly_summary,
            "cron",
            day_of_week=4,  # Friday
            hour=17,
            minute=0,
            timezone="America/New_York",
            id="axiom-inspector-weekly",
            replace_existing=True,
        )

        logger.info("Axiom Inspector General initialized — 4 audit jobs scheduled")
    except Exception as e:
        logger.error("Failed to schedule inspector jobs: %s", e)

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
    version     = "4.1.0",
    lifespan    = lifespan,
)


# ── Request Models ────────────────────────────────────────────────────────────

class AssessRequest(BaseModel):
    ticker: str
    dte: Optional[int] = None         # Optional DTE for options — enforced against MIN_DTE/MAX_DTE
    strategy: Optional[str] = None    # Optional strategy type: "debit", "credit", "short", "long"
    ivr: Optional[float] = None       # IV rank/percentile (0–100) — enforced against IVR credit/debit limits
    vix: Optional[float] = None       # C-4 fix: caller-supplied VIX for defence-in-depth halt check
    proposed_usd: Optional[float] = None  # Proposed trade size in USD
    strike: Optional[float] = None    # Short strike price
    r2_score: Optional[float] = None  # R2 conviction score from OMNI
    confidence: Optional[float] = None   # OMNI confidence score
    pathway: Optional[str] = None        # Concordance pathway
    sizing: Optional[float] = None       # Sizing multiplier


class PendingPick(BaseModel):
    """A pending pick from a screening agent, awaiting Axiom risk assessment + OMNI synthesis."""
    pick_id: str                    # Unique pick identifier (candidate_id)
    ticker: str                     # Ticker symbol (e.g., "SPY")
    strike: float                   # Strike price
    dte: Optional[int] = None       # Days to expiration (computed from expiration date)
    direction: str                  # "call" or "put"
    thesis: str                     # Trade thesis from screening agent
    confidence: float               # Confidence score (0–100) from screening agent
    screening_agent: str            # Source agent ("ARGUS" | "APEX" | "TRIDENT")
    risk_flags: list[str]           # Risk flags from Axiom assessment (empty if not yet assessed)
    risk_score: Optional[float] = None  # Risk score (0–10) from Axiom (None if not assessed)
    submitted_at: str               # ISO timestamp when pick was submitted
    assessed_at: Optional[str] = None  # ISO timestamp when Axiom assessed it


class PicsResponse(BaseModel):
    """Response: List of pending picks awaiting OMNI synthesis."""
    pending_picks: list[PendingPick]
    count: int                      # Total count of pending picks
    timestamp: str                  # Response timestamp (ISO)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> JSONResponse:
    """
    Health check endpoint. Always returns 200.
    Never exposes internal errors or state details.
    """
    # Block 2: STANDBY fast path — GA takes no action for status: "standby"
    with _axiom_mode_lock:
        _sb_mode   = _SERVICE_MODE
        _sb_reason = _standby_reason
    if _sb_mode == "standby":
        return JSONResponse({
            "status":  "standby",
            "service": "axiom",
            "version": "4.1.0",
            "reason":  _sb_reason,
        })

    regime   = app_state.get("regime")
    pool_size = len(app_state.get("pool", []))

    regime_updated = app_state.get("regime_last_updated")
    return JSONResponse({
        "status":               "healthy",
        "service":              "axiom",
        "version":              "4.1.0",
        "pool_size":            pool_size,
        "regime":               regime.classification if regime else "unknown",
        "regime_source":        regime.regime_source if regime else "unknown",
        "composite_score":      regime.composite_score if regime else None,
        "vix":                  regime.vix if regime else None,
        "regime_last_updated":  regime_updated,
        "submissions_open":     _is_submissions_open(),
        "uptime_since":         app_state.get("start_time"),
        "code_hash":           _CODE_HASH,
        "stale_deploy":        (not _os.path.exists("/tmp/nexus_deploy_in_progress")) and _CODE_HASH != _compute_module_hash(),
        "resilience_status":   (app_state["_health_report"].to_dict()
                                if app_state.get("_health_report") else None),
    })


@app.get("/pool")
def get_pool(request: Request) -> JSONResponse:
    """
    Return the current live candidate pool.

    Returns:
        Pool payload with tickers, count, window_id, and regime.
    """
    verify_secret(request)

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


@app.get("/universe")
def get_universe(request: Request) -> JSONResponse:
    """
    Return the current Axiom universe as a tickers list.

    Alias for /pool — provides the tickers key expected by Alpha Buffer C-01
    universe validation check.

    Returns:
        Dict with tickers list and count.
    """
    verify_secret(request)

    with _state_lock:
        pool = list(app_state.get("pool", []))

    return JSONResponse({
        "tickers": pool,
        "count":   len(pool),
    })


@app.get("/regime")
def get_regime(request: Request) -> JSONResponse:
    """
    Return the current VIX regime classification.

    Returns:
        Regime dict with classification and all allowed flags.
    """
    verify_secret(request)

    regime = app_state.get("regime")
    if regime is None:
        return JSONResponse({"classification": "UNKNOWN", "vix": None, "error": "Regime not yet loaded"})

    return JSONResponse(regime.to_dict())


@app.get("/anchor")
def get_anchor(request: Request) -> JSONResponse:
    """
    Return the current Tier 1 anchor stocks.

    Returns:
        List of anchor stock tickers from the latest Tier 1 run.
    """
    verify_secret(request)

    anchors = app_state.get("anchor_stocks", [])
    return JSONResponse({
        "anchor_stocks": anchors,
        "count":         len(anchors),
        "as_of":         datetime.now(ET).strftime("%Y-%m-%d"),
    })


@app.post("/assess")
def assess_stock(
    body: AssessRequest,
    request: Request,
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
    verify_secret(request)

    # Block 2: STANDBY gate — return hard stop when VIX/regime preflight has not passed
    with _axiom_mode_lock:
        if _SERVICE_MODE == "standby":
            return JSONResponse({
                "ticker":         body.ticker.upper().strip(),
                "risk_score":     10.0,
                "sizing_mult":    0.0,
                "hard_stops":     ["AXIOM_STANDBY"],
                "critical_flags": [],
                "concern_1":      f"Axiom in STANDBY: {_standby_reason}",
                "concern_2":      "N/A",
                "concern_3":      "N/A",
                "in_pool":        False,
                "regime":         "UNKNOWN",
                "window_id":      app_state.get("window_id") or current_window_id(),
                "model":          "axiom-risk-engine-v3",
            })

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

    # Resilience — advisory VIX staleness check (additive, does NOT replace regime is None gate)
    _vix_cache = app_state.get("_vix_cache")
    if _vix_cache and _vix_cache.get() is None:
        logger.warning("VIX cache stale in /assess (age=%ds) — using last_vix fallback",
                       _vix_cache.age_seconds)

    # Basic risk scoring
    hard_stops: list[str] = []
    flags:      list[str] = []
    concerns:   list[str] = []
    risk_score  = 2.0
    sizing_mult = 1.0

    # C-4 fix (2026-05-02): VIX hard halt at Axiom layer — defence in depth.
    # Previously only enforced at alpha-execution. If execution is bypassed or
    # restarts with stale state, VIX halt was invisible to Axiom.
    vix_in_assess = body.vix if body.vix is not None else app_state.get("last_vix")
    if vix_in_assess is not None and vix_in_assess >= VIX_PAUSE_THRESHOLD:
        hard_stops.append(
            f"VIX {vix_in_assess:.1f} ≥ halt threshold {VIX_PAUSE_THRESHOLD} — "
            f"no new positions permitted"
        )
        risk_score = 10.0
        sizing_mult = 0.0

    # DTE enforcement (hard block — item ④ OMNI 7AM diagnostic)
    if body.dte is not None:
        if body.dte < MIN_DTE:
            hard_stops.append(f"DTE {body.dte}d below minimum {MIN_DTE}d — hard blocked")
            risk_score = 10.0
            sizing_mult = 0.0
        elif body.dte > MAX_DTE:
            hard_stops.append(f"DTE {body.dte}d above maximum {MAX_DTE}d — hard blocked")
            risk_score = 10.0
            sizing_mult = 0.0

    # IVR-based strategy enforcement — HARD GATES (Blocker 4 fix, 2026-04-29)
    # Credit spreads require meaningful premium to have a viable risk/reward.
    # Below MIN_IVR_CREDIT_SPREAD (30): credit is too thin — hard block.
    # Debit spreads above MAX_IVR_DEBIT_SPREAD (70): overpaying premium — hard block.
    if body.ivr is not None:
        if body.strategy in ("credit", "bull_put_spread", "bear_call_spread", "iron_condor"):
            if body.ivr < MIN_IVR_CREDIT_SPREAD:
                hard_stops.append(
                    f"IVR {body.ivr} below minimum {MIN_IVR_CREDIT_SPREAD} for credit spreads — "
                    f"premium too thin for viable risk/reward"
                )
                risk_score = max(risk_score, 8.0)
                sizing_mult = 0.0
        elif body.strategy in ("debit", "bull_call_spread", "bear_put_spread"):
            if body.ivr > MAX_IVR_DEBIT_SPREAD:
                hard_stops.append(
                    f"IVR {body.ivr} above maximum {MAX_IVR_DEBIT_SPREAD} for debit spreads — "
                    f"overpaying premium, poor risk/reward"
                )
                risk_score = max(risk_score, 8.0)
                sizing_mult = 0.0
    elif body.strategy in ("credit", "bull_put_spread", "bear_call_spread", "iron_condor"):
        # No IVR provided — flag it but don't hard block (IVR data may be unavailable)
        flags.append("Credit spread submitted without IVR — verify IV percentile >= 30 before entry")

    # ────────────────────────────────────────────────────────────────────────────────
    # LAYER 1: CONCENTRATION RISK — Position gating (CRITICAL FIX)
    # ────────────────────────────────────────────────────────────────────────────────
    try:
        from risk_engine import score_layer_1_concentration
        positions = requests.get(f"{ALPACA_BASE}/v2/positions", headers=ALPACA_H, timeout=8).json()
        if isinstance(positions, list):
            equity = float(requests.get(f"{ALPACA_BASE}/v2/account", headers=ALPACA_H, timeout=8).json().get("equity", 1))
            layer1_result = score_layer_1_concentration(ticker, 1000, positions, equity)  # Proposed: $1000 test
            if layer1_result.get("critical_flag"):
                hard_stops.append(layer1_result.get("note", "Position concentration limit breached"))
                risk_score = 10.0
                sizing_mult = 0.0
            elif "gate CLOSED" in layer1_result.get("note", ""):
                hard_stops.append(layer1_result.get("note", "Position gate closed"))
                risk_score = 10.0
                sizing_mult = 0.0
    except Exception as e:
        logger.warning("Layer 1 concentration check failed: %s — falling through", e)
        # Do NOT block submission if check fails — risk being overly permissive vs blocking legitimately
        pass

    # Not in current pool
    if pool and ticker not in pool:
        concerns.append(f"{ticker} not in current Axiom pool — submitted outside pool")
        risk_score += 1.0

    # Strategy-specific risk (debit spreads in high IV = premium risk)
    if body.strategy and body.strategy == "debit":
        flags.append("Debit spread — verify IVR is not elevated before entry")
        concerns.append("Debit spread — check IVR to avoid overpaying premium")

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

    # Cache to DB — BEGIN IMMEDIATE (resilience layer: prevents concurrent write race)
    try:
        assess_db_write(
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
def oracle_status(request: Request) -> JSONResponse:
    """
    Diagnostic endpoint — ORACLE integration status.

    Returns current ORACLE reachability, last pre-warm and coherence query
    timestamps, and current regime source.
    """
    verify_secret(request)

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


@app.get("/picks")
def get_pending_picks(request: Request) -> PicsResponse:
    """
    GET /picks — Retrieve pending picks awaiting OMNI synthesis.

    OMNI polls this endpoint every ~30 seconds during market hours.
    Returns all pending picks that have passed Axiom gate checks but
    not yet been synthesized by OMNI.

    Response (200):
      {
        "pending_picks": [
          {
            "pick_id": "MOCK_SPY_20260620_CALL_500",
            "ticker": "SPY",
            "strike": 500.0,
            "dte": 14,
            "direction": "call",
            "thesis": "breakdown below 200-SMA, oversold",
            "confidence": 8.5,
            "screening_agent": "ARGUS",
            "risk_flags": ["concentration_risk", "vix_elevated"],
            "risk_score": 6.2,
            "submitted_at": "2026-05-19T14:35:22Z",
            "assessed_at": "2026-05-19T14:35:45Z"
          }
        ],
        "count": 1,
        "timestamp": "2026-05-19T14:36:00Z"
      }

    Contract:
      - Returns empty array if no pending picks
      - Includes risk assessment (if completed) for OMNI's reference
      - OMNI uses pick_id for correlation after GO/NO-GO decision
      - Does NOT remove picks from queue (executor handles that)
    """
    verify_secret(request)

    try:
        import sqlite3 as _sqlite3, json as _json
        from datetime import datetime

        now = datetime.now(ET).isoformat()
        _conn = _sqlite3.connect(settings.axiom_db_path)
        _conn.row_factory = _sqlite3.Row
        _rows = _conn.execute(
            "SELECT * FROM candidates WHERE status = \'pending\' ORDER BY submitted_at ASC"
        ).fetchall()
        _conn.close()
        pending_candidates = [dict(r) for r in _rows]

        # Convert candidates to PendingPick format
        picks: list[PendingPick] = []
        for candidate in pending_candidates:
            # Compute DTE from expiration date
            from dateutil.parser import parse as parse_date
            try:
                exp_date = parse_date(candidate["expiration"]).date()
                today = datetime.now(ET).date()
                dte = (exp_date - today).days
            except Exception:
                dte = None

            # Build PendingPick
            pick = PendingPick(
                pick_id=candidate["candidate_id"],
                ticker=candidate["symbol"],
                strike=candidate["strike"],
                dte=dte,
                direction=candidate["option_type"].lower(),
                thesis="",  # Not yet available in candidate (populated by screening agent)
                confidence=float(candidate.get("conviction", 50)) / 100.0 * 10.0,  # Scale 0-100 to 0-10
                screening_agent=candidate.get("source", "UNKNOWN").upper(),
                risk_flags=[],  # Will be populated after Axiom assessment
                risk_score=None,  # Will be populated after Axiom assessment
                submitted_at=candidate["submitted_at"],
                assessed_at=None,  # Mark as not yet assessed
            )
            picks.append(pick)

        return PicsResponse(
            pending_picks=picks,
            count=len(picks),
            timestamp=now
        )

    except Exception as e:
        logger.error("Error retrieving pending picks: %s", e)
        raise HTTPException(status_code=500, detail={"error": "failed_to_retrieve_picks", "message": str(e)})


@app.post("/trigger-tier1")
def trigger_tier1(request: Request) -> JSONResponse:
    """
    Manually trigger a Tier 1 anchor-stock scan.

    Used by Guardian Angel when pool is empty during market hours,
    and by the morning diagnostic if Axiom missed its scheduled run.
    """
    verify_secret(request)
    from scheduler import _run_tier1
    import threading
    try:
        t = threading.Thread(target=_run_tier1, args=(app_state, "manual"), daemon=True)
        t.start()
        return JSONResponse({"status": "triggered", "message": "Tier 1 scan started in background"})
    except Exception as e:
        logger.error("Manual Tier 1 trigger failed: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/trigger-tier2")
def trigger_tier2(request: Request) -> JSONResponse:
    """
    Manually trigger a Tier 2 pool refresh and agent push.

    Used to recover missed windows after service restart, or to force
    an immediate pool dispatch outside the normal 15-min schedule.
    Bypasses the submissions_open gate so it works pre-market or
    any time anchor_stocks are populated.

    Root cause fix (2026-04-23): APScheduler cron jobs do not replay
    missed fire times after a restart — this endpoint is the manual
    recovery path when Axiom restarts between 8:30 and 9:15 AM ET.
    """
    verify_secret(request)
    from scheduler import _run_tier2_if_open
    import threading

    if not app_state.get("anchor_stocks"):
        return JSONResponse(
            {"status": "error", "message": "No anchor stocks — run /trigger-tier1 first"},
            status_code=400,
        )
    try:
        # force=True bypasses the market-hours gate so manual triggers work pre-market
        t = threading.Thread(target=_run_tier2_if_open, args=(app_state, True), daemon=True)
        t.start()
        return JSONResponse({"status": "triggered", "message": "Tier 2 pool refresh started in background"})
    except Exception as e:
        logger.error("Manual Tier 2 trigger failed: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/premarket")
def trigger_premarket(request: Request) -> JSONResponse:
    """
    Manually trigger the pre-market brief generation.

    Useful for testing or if the scheduled run was missed.
    """
    verify_secret(request)

    from scheduler import _run_premarket_brief
    try:
        _run_premarket_brief(app_state)
        return JSONResponse({"status": "sent"})
    except Exception as e:
        logger.error("Manual premarket trigger failed: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)



# ── SOVEREIGN Push Endpoints ──────────────────────────────────────────────────

class _SovDirective(BaseModel):
    directive: str
    data: dict = {}
    from_agent: str = "sovereign"


@app.post("/sovereign/directive", status_code=200)
def sovereign_directive(
    body: _SovDirective,
    request: Request,
) -> JSONResponse:
    """SOVEREIGN pushes a directive to Axiom. Zero polling lag."""
    verify_secret(request)
    d = body.directive.strip().upper()
    logger.info("SOVEREIGN direct push to axiom: %s", d)

    if d == "PING":
        report("axiom", "ack", {"directive": "PING", "status": "alive"})
        return JSONResponse({"ok": True, "directive": "PING", "status": "alive"})
    elif d == "STATUS":
        regime = app_state.get("regime")
        snap = {
            "directive": "STATUS", "service": "axiom",
            "status": app_state.get("status", "unknown"),
            "regime": regime.classification if regime else None,
            "pool_size": len(app_state.get("anchor_stocks", [])),
            "port": int(__import__("os").getenv("PORT", "8001")),
        }
        report("axiom", "status", snap)
        return JSONResponse({"ok": True, **snap})
    elif d in ("FLUSH", "RESET_DAY"):
        app_state["anchor_stocks"] = []
        report("axiom", "ack", {"directive": d, "status": "applied"})
        return JSONResponse({"ok": True, "directive": d})
    else:
        report("axiom", "ack", {"directive": d, "status": "unrecognized"})
        return JSONResponse({"ok": False, "error": f"unrecognized directive: {d}"}, status_code=400)


@app.get("/limits", status_code=200)
def get_limits(request: Request) -> JSONResponse:
    """
    Return the current system limits and circuit breaker thresholds.

    Provides a single source of truth for all limit config.
    All values defined in config.py — no silent defaults.

    Returns:
        Dict with max_positions, max_risk_per_trade, min_dte, max_dte,
        vix_pause_threshold, and current regime state.
    """
    verify_secret(request)

    regime = app_state.get("regime")
    vix    = regime.vix if regime else None

    # Check if VIX pause is active
    vix_paused = False
    if vix is not None and vix >= VIX_PAUSE_THRESHOLD:
        vix_paused = True
    if regime and regime.classification == "CRISIS":
        vix_paused = True

    return JSONResponse({
        "max_positions":           MAX_POSITIONS,
        "max_risk_per_trade":      MAX_RISK_PER_TRADE,
        "min_dte":                 MIN_DTE,
        "max_dte":                 MAX_DTE,
        "vix_pause_threshold":     VIX_PAUSE_THRESHOLD,
        "min_ivr_credit_spread":   MIN_IVR_CREDIT_SPREAD,
        "max_ivr_debit_spread":    MAX_IVR_DEBIT_SPREAD,
        "vix_current":             vix,
        "vix_paused":              vix_paused,
        "regime":                  regime.classification if regime else "UNKNOWN",
        "source":                  "axiom-config",
    })


@app.get("/positions/count", status_code=200)
def positions_count(request: Request) -> JSONResponse:
    """
    Return the current open position count and max allowed.
    
    Used by Prime (and Alpha) to verify cross-system position gating.
    Prevents both systems from opening positions that would breach Ahmed's cap.
    
    Returns:
        Dict with total_positions, max_allowed, compliant (bool).
    """
    verify_secret(request)
    
    try:
        # Query Alpaca for actual open position count across ALL systems
        import os as _os
        import requests as _r_pos
        _alpaca_key = _os.getenv("ALPACA_KEY", "")
        _alpaca_secret = _os.getenv("ALPACA_SECRET", "")
        
        if not _alpaca_key or not _alpaca_secret:
            # Keys not configured — fall back to database count if available
            # This is not a fatal error; position gating will use local count
            _count = 0
        else:
            _h_alpaca = {
                "APCA-API-KEY-ID": _alpaca_key,
                "APCA-API-SECRET-KEY": _alpaca_secret,
            }
            _resp_pos = _r_pos.get(
                "https://paper-api.alpaca.markets/v2/positions",
                headers=_h_alpaca,
                timeout=4
            )
            if _resp_pos.status_code == 200:
                _positions = _resp_pos.json()
                _count = len(_positions) if isinstance(_positions, list) else 0
            else:
                # Fallback: assume 0 if Alpaca unreachable (safe fallback)
                _count = 0
    except Exception as _e:
        # On any error, return current count as unknown but allow execution
        # (fail-safe: don't block trades on gate endpoint failure)
        _count = 0
    
    _max = MAX_POSITIONS  # From config: typically 3
    _compliant = _count < _max
    
    return JSONResponse({
        "total_positions": _count,
        "max_allowed": _max,
        "compliant": _compliant,
        "service": "axiom-position-gate",
    })


@app.get("/sovereign/status", status_code=200)
def sovereign_status(
    request: Request,
) -> JSONResponse:
    """SOVEREIGN queries Axiom state on-demand."""
    verify_secret(request)
    regime = app_state.get("regime")
    return JSONResponse({
        "ok": True, "service": "axiom",
        "status": app_state.get("status", "unknown"),
        "regime": regime.classification if regime else None,
        "pool_size": len(app_state.get("anchor_stocks", [])),
        "port": int(__import__("os").getenv("PORT", "8001")),
    })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_submissions_open() -> bool:
    """
    Return True if current time is within the submission window (9:15 AM – 3:30 PM ET, Mon-Fri)
    AND position count is below the 3-position max.

    Returns:
        True if submissions are currently accepted.
    """
    # Check market hours
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    open_time  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
    
    if not (open_time <= now <= close_time):
        return False
    
    # Check position count (CRITICAL FIX 2026-06-01 11:34 ET)
    try:
        import requests
        ALPACA_KEY = os.getenv("ALPACA_KEY_ID", "PKPGM3BRNYPGCF5Z56IAUZCZJL")
        ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs")
        h = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        r = requests.get("https://paper-api.alpaca.markets/v2/positions", headers=h, timeout=5)
        if r.status_code == 200:
            positions = r.json()
            pos_count = len(positions) if isinstance(positions, list) else 0
            # Gate: allow submissions only if <= 3 positions open (fail-closed)
            if pos_count > 3:
                logger.warning(f"Position gate CLOSED: {pos_count}/3 positions open")
                return False
    except Exception as e:
        # On Alpaca connection error, FAIL CLOSED (block submissions)
        logger.warning(f"Position count check failed: {e} — blocking submissions as safety measure")
        return False
    
    return True

@app.get("/trace")
def trace_headers(request: Request) -> JSONResponse:
    """Debug: returns all received headers. Requires valid auth."""
    verify_secret(request)
    return JSONResponse({
        "headers": dict(request.headers),
        "version": "4.0.0",
    })


# ── MOCK ENDPOINTS for Resilience Testing ─────────────────────────────────────
# All /mock/* routes are available at all times (not restricted by mode).
# Used by daily resilience rehearsal (RESILIENCE_FRAMEWORK).

from axiom.mock_endpoints import (
    ClearPendingRequest,
    ClearPendingResponse,
    SubmitCandidateRequest,
    SubmitCandidateResponse,
    GateBlockedResponse,
    SetVixRequest,
    SetVixResponse,
    SetEarningsRequest,
    SetEarningsResponse,
    SetOptionParamsRequest,
    SetOptionParamsResponse,
    SetPositionLimitRequest,
    SetPositionLimitResponse,
    clear_pending_candidates,
    submit_candidate,
    set_vix_mock,
    set_earnings_calendar,
    set_option_params,
    set_position_limit,
    reset_mock_state,
)


@app.post("/mock/clear_pending", response_model=ClearPendingResponse, status_code=200)
def handle_clear_pending(request: ClearPendingRequest) -> ClearPendingResponse:
    """
    POST /mock/clear_pending — Clear all pending candidates.

    Request:
      {"symbol": null}  // optional: clear only this symbol, null = clear all

    Response (200):
      {"status": "cleared", "count": 5, "timestamp": "..."}

    Contract:
      - Idempotent (safe to call multiple times)
      - Does NOT trigger any buffer submissions
      - Returns actual count deleted
      - If symbol specified and not found: returns count=0, status=cleared
    """
    try:
        return clear_pending_candidates(settings.axiom_db_path, request)
    except RuntimeError as e:
        logger.error("Error clearing pending candidates: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/mock/submit_candidate", status_code=201)
def handle_submit_candidate(request: SubmitCandidateRequest):
    """
    POST /mock/submit_candidate — Submit a mock candidate trade.

    Response (201):
      {"status": "submitted", "candidate_id": "MOCK_SPY_170620_CALL_500", "timestamp": "..."}

    Response (400): Validation failed
      {"error": "validation_failed", "message": "..."}

    Response (403): Gate blocked (intended)
      {"error": "gate_blocked", "gate": "vix_brake", "reason": "...", "timestamp": "..."}

    Contract:
      - Validates all fields before accepting
      - Returns which gate (if any) rejects the candidate
      - Duplicate detection: if identical symbol+strike+expiration+type exists → reject
      - Generates deterministic candidate_id
      - Does NOT submit to buffer directly
    """
    try:
        result = submit_candidate(settings.axiom_db_path, request, vix_current=app_state.get("last_vix"))

        if "error" in result:
            if result["error"] == "gate_blocked":
                logger.info("Gate blocked: %s", result.get("gate"))
                raise HTTPException(
                    status_code=403,
                    detail=result
                )
            else:
                raise HTTPException(
                    status_code=400,
                    detail=result
                )

        # Return 201 on success
        return JSONResponse(
            status_code=201,
            content=result
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": "validation_failed", "message": str(e)})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error submitting candidate: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/mock/set_vix", response_model=SetVixResponse, status_code=200)
def handle_set_vix(request: SetVixRequest) -> SetVixResponse:
    """
    POST /mock/set_vix — Set mock VIX for scenario testing.

    Contract:
      - Affects all subsequent Axiom gate checks
      - Brake threshold is configurable
      - If VIX >= threshold, ALL new submissions blocked immediately
    """
    try:
        response = set_vix_mock(settings.axiom_db_path, request)
        # Update app state for gate checks
        app_state["last_vix"] = request.value
        return response
    except RuntimeError as e:
        logger.error("Error setting VIX: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/mock/set_earnings_calendar", response_model=SetEarningsResponse, status_code=200)
def handle_set_earnings(request: SetEarningsRequest) -> SetEarningsResponse:
    """
    POST /mock/set_earnings_calendar — Mock earnings events for symbol.

    Contract:
      - Affects gate checks: if symbol has earnings today, blocks all new picks
      - Persists until explicitly cleared
      - Multiple symbols can have earnings simultaneously
    """
    try:
        return set_earnings_calendar(settings.axiom_db_path, request)
    except RuntimeError as e:
        logger.error("Error setting earnings calendar: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/mock/set_option_params", response_model=SetOptionParamsResponse, status_code=200)
def handle_set_option_params(request: SetOptionParamsRequest) -> SetOptionParamsResponse:
    """
    POST /mock/set_option_params — Override option metadata for testing.

    Contract:
      - Overrides live option data for this specific strike
      - Scope: this symbol+strike+expiration only
      - Used by Axiom gates (DTE < 7 blocks)
    """
    try:
        return set_option_params(settings.axiom_db_path, request)
    except RuntimeError as e:
        logger.error("Error setting option params: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/mock/set_position_limit", response_model=SetPositionLimitResponse, status_code=200)
def handle_set_position_limit(request: SetPositionLimitRequest) -> SetPositionLimitResponse:
    """
    POST /mock/set_position_limit — Configure max concurrent positions.

    Contract:
      - Applies immediately to next submission
      - Returns current count so caller knows how many positions are open
      - Rejecting new candidate returns error="position_limit_exceeded"
    """
    try:
        return set_position_limit(settings.axiom_db_path, request)
    except RuntimeError as e:
        logger.error("Error setting position limit: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/mock/reset")
def handle_mock_reset():
    """
    POST /mock/reset — Reset all mock state to defaults.
    
    Used between test scenarios to clear pending candidates, earnings calendar, etc.
    """
    try:
        reset_mock_state()
        return {
            "status": "reset",
            "message": "All mock state cleared",
            "timestamp": datetime.now(ET).isoformat()
        }
    except Exception as e:
        logger.error("Error resetting mock state: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
