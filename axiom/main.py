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

def current_window_id() -> str:
    """
    Generate the current 15-minute window ID in format 'YYYY-MM-DD-HHMM'.

    Returns:
        Window ID string rounded to the current 15-minute boundary.
    """
    now  = datetime.now(ET)
    mins = (now.minute // 15) * 15
    return now.strftime(f"%Y-%m-%d-%H{mins:02d}")


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

@app.get("/trace")
def trace_headers(request: Request) -> JSONResponse:
    """Debug: returns all received headers. Requires valid auth."""
    verify_secret(request)
    return JSONResponse({
        "headers": dict(request.headers),
        "version": "4.0.0",
    })
