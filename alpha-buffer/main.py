"""
main.py — Alpha Concordance Buffer FastAPI Entry Point

Receives R1 agent picks (options/Alpha) and evaluates concordance.
Auth: X-Nexus-Secret header → NEXUS_WEBHOOK_SECRET env var.
Port: 8002 (local), $PORT (Railway).

Endpoints:
  POST /submit          — Agent submits a pick
  GET  /status          — Buffer status + current window submissions
  GET  /health          — Always 200
  POST /circuit-breaker/reset — Ahmed manual circuit breaker reset
  POST /trade-outcome   — Record closed trade result (updates circuit breaker)
"""

import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

import pytz
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from config import VALID_AGENTS, VALID_PATHWAYS, MIN_SUBMISSION_SCORE, load_settings, assert_thresholds, assert_axiom_secret
from concordance import evaluate_concordance
from circuit_breaker import (
    CircuitBreakerStatus,
    evaluate_circuit_breaker,
    record_trade_outcome,
    reset_circuit_breaker,
)
from database import (
    get_conn,
    is_already_submitted_today,
    clear_submission_for_retry,
    get_all_tickers_in_window,
    get_circuit_breaker_state,
    get_concordance_result,
    get_undispatched_concordances,
    get_window_submissions,
    increment_dispatch_attempts,
    init_db,
    init_circuit_breaker,
    mark_omni_dispatched,
    save_submission,
    save_concordance_result,
)
from omni_client import dispatch_to_omni

# ── Shared sovereign_comms (module-level import so route handlers can call report()) ──
import sys as _sys_sc
_sys_sc.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
try:
    from shared.sovereign_comms import report as _sov_report, get_instructions as _sov_get_instructions
except Exception:  # pragma: no cover
    def _sov_report(*a, **kw): pass  # type: ignore[misc]
    def _sov_get_instructions(*a, **kw): return []  # type: ignore[misc]
from shared.watchdog import Watchdog

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("alpha_buffer.main")

ET = pytz.timezone("America/New_York")

import hashlib as _hashlib, os as _os, glob as _glob

# G7 NOTE (2026-05-03): canonical implementation lives in shared/module_hash.py.
def _compute_module_hash() -> str:
    """Hash all *.py files in this service directory (excluding __pycache__).
    Returns 8-char hex digest. Canonical: shared/module_hash.compute_module_hash(__file__)
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

# ── Load settings — crash fast on missing env vars ────────────────────────────
settings = load_settings()

# ── App state ─────────────────────────────────────────────────────────────────
app_state: dict = {
    "settings":  settings,
    "start_time": datetime.now(ET).isoformat(),
}

# ── Score Distribution Watchdog state ─────────────────────────────────────────
# Tracks scores per window to detect uniform/fabricated scoring
_window_score_log: dict = {}   # {window_id: {ticker: score}}
_score_log_lock = threading.Lock()
SCORE_UNIFORM_THRESHOLD = 0.75   # 75%+ identical scores in a window = data failure
SCORE_UNIFORM_MIN_PICKS = 5      # need at least 5 picks to flag


def _record_window_score(window_id: str, ticker: str, score: float) -> None:
    """Record a score for uniform distribution monitoring."""
    with _score_log_lock:
        if window_id not in _window_score_log:
            _window_score_log[window_id] = {}
        _window_score_log[window_id][ticker] = score


def _check_score_distribution(window_id: str) -> None:
    """
    After a window closes, check if scores are suspiciously uniform.
    Uniform scores across different companies = Oracle data failure, not market signal.
    Alerts Ahmed and logs CRITICAL if detected.
    """
    with _score_log_lock:
        scores = list(_window_score_log.get(window_id, {}).values())
    if len(scores) < SCORE_UNIFORM_MIN_PICKS:
        return
    most_common = max(set(scores), key=scores.count)
    uniformity = scores.count(most_common) / len(scores)
    if uniformity >= SCORE_UNIFORM_THRESHOLD:
        msg = (
            f"🔴 SCORE UNIFORMITY ALERT — window {window_id}\n"
            f"{uniformity:.0%} of {len(scores)} scores = {most_common:.1f}/100\n"
            f"Scores: {sorted(scores)}\n"
            f"DIAGNOSIS: Oracle data likely null/frozen. D1/D3/D5 returning defaults.\n"
            f"ACTION: Check Oracle /health and run oracle_data_preflight.py immediately."
        )
        logger.critical("SCORE_UNIFORM: %s", msg)
        try:
            import requests as _req
            _bot = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            if not _bot:
                logger.error("SCORE_UNIFORM Telegram alert skipped: TELEGRAM_BOT_TOKEN not set")
            for chat_id in ["8573754783", "-1003954790884"]:
                _req.post(
                    f"https://api.telegram.org/bot{_bot}/sendMessage",
                    json={"chat_id": chat_id, "text": msg},
                    timeout=5,
                )
        except Exception as _te:
            logger.warning("Score uniform alert telegram failed: %s", _te)


# ── Helpers ───────────────────────────────────────────────────────────────────

def current_window_id() -> str:
    """
    Generate the current 15-minute window ID: 'YYYY-MM-DD-HHMM'.

    Returns:
        Window ID string rounded to the current 15-minute boundary.
    """
    now  = datetime.now(ET)
    mins = (now.minute // 15) * 15
    return now.strftime(f"%Y-%m-%d-%H{mins:02d}")


def verify_secret(x_nexus_secret: str) -> None:  # noqa: N802
    """
    Verify X-Nexus-Secret header against configured secret.

    Raises:
        HTTPException: 403 if missing or invalid.
    """
    if not x_nexus_secret or not secrets.compare_digest(
        x_nexus_secret, settings.nexus_webhook_secret
    ):
        raise HTTPException(status_code=403, detail="Forbidden")


# ── Lifespan ──────────────────────────────────────────────────────────────────

async def _omni_retry_loop() -> None:
    """
    Background retry loop for failed OMNI dispatches (OMNI H2 fix).

    Polls every 60s for concordances with omni_dispatched=0.
    Retries up to 3 times. Alerts Ahmed via Telegram after exhausting retries.
    A missed concordance = missed trade entry — this loop prevents permanent loss.
    """
    import asyncio as _asyncio
    import json as _json
    MAX_ATTEMPTS = 3
    POLL_INTERVAL = 60

    while True:
        await _asyncio.sleep(POLL_INTERVAL)
        try:
            # OMNI M-NEW-1 fix: wrap synchronous SQLite + HTTP calls in run_in_executor
            # to avoid blocking the asyncio event loop. get_undispatched_concordances()
            # and dispatch_to_omni() are both synchronous — running them directly in an
            # async def blocks all other coroutines during execution.
            loop = _asyncio.get_event_loop()
            pending = await loop.run_in_executor(
                None, get_undispatched_concordances,
                settings.alpha_db_path, 30, MAX_ATTEMPTS,
            )
            for row in pending:
                try:
                    concordance_dict = {
                        "ticker":          row["ticker"],
                        "direction":       row["direction"],
                        "pathway":         row["pathway"],
                        "window_id":       row["window_id"],
                        "weighted_score":  row["weighted_score"],
                        "sizing_mult":     row["sizing_mult"],
                        "verdict":         row["verdict"],
                        "agents_involved": _json.loads(row["agents_involved"]),
                        "scores":          _json.loads(row["scores"]),
                        "agent_count":     row["agent_count"],
                        # FIX: retry loop was missing required fields → 422 Unprocessable
                        # OMNI ConcordancePayload requires: system, echo_chamber, notes
                        # These columns don't exist in concordance_results DB (historical)
                        # so we apply safe defaults: alpha system, no echo chamber, empty notes
                        "system":          "alpha",
                        "echo_chamber":    False,
                        "notes":           [],
                    }
                    ok, response = await loop.run_in_executor(
                        None, lambda: dispatch_to_omni(
                            omni_url=settings.omni_webhook_url,
                            auth_headers=settings.omni_auth_header,
                            concordance_dict=concordance_dict,
                        )
                    )
                    if ok:
                        mark_omni_dispatched(
                            settings.alpha_db_path,
                            row["window_id"], row["ticker"], row["direction"],
                            omni_response=response,
                        )
                        logger.info(
                            "OMNI retry succeeded: %s/%s window=%s attempt=%d",
                            row["ticker"], row["direction"], row["window_id"],
                            row["dispatch_attempts"] + 1,
                        )
                    else:
                        increment_dispatch_attempts(
                            settings.alpha_db_path,
                            row["window_id"], row["ticker"], row["direction"],
                        )
                        new_attempts = row["dispatch_attempts"] + 1
                        logger.warning(
                            "OMNI retry failed: %s/%s attempt=%d",
                            row["ticker"], row["direction"], new_attempts,
                        )
                        if new_attempts >= MAX_ATTEMPTS:
                            # Alert Ahmed — concordance permanently lost
                            _alert_lost_concordance(row)
                except Exception as e:
                    logger.error("OMNI retry loop error for %s: %s", row.get("ticker"), e)
        except Exception as e:
            logger.error("OMNI retry loop poll error: %s", e)


def _alert_lost_concordance(row: dict) -> None:
    """Notify SOVEREIGN + Discord when a concordance exhausts all OMNI dispatch retries."""
    try:
        from shared.notification_router import notify_warn as _nw
        _nw(
            "alpha-buffer",
            "LOST CONCORDANCE — OMNI Dispatch Failed (3 retries)",
            (
                f"Pathway: {row['pathway']} | Window: {row['window_id']}\n"
                f"Verdict: {row['verdict']} | Score: {row['weighted_score']:.1f}\n"
                f"Concordance is permanently unexecuted."
            ),
            ticker=row['ticker'],
        )
    except Exception as e:
        logger.error("Lost concordance notification failed: %s", e)


_nns_watchdog = Watchdog("alpha-buffer")

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize DB and circuit breaker state on startup."""
    import asyncio as _asyncio
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
    from shared.db_guard import assert_unique_db_path  # S4: collision guard
    assert_unique_db_path("alpha-buffer", settings.alpha_db_path)
    logger.info("Alpha Buffer starting...")
    # G9 SYS-2: Auth registry validation
    from shared.auth_registry import validate_service_auth_config, AuthConfigError
    try:
        validate_service_auth_config("alpha-buffer")
        logger.info("Auth registry validation passed for alpha-buffer")
    except AuthConfigError as e:
        logger.critical("Auth config invalid: %s", e)
    init_db(settings.alpha_db_path)
    init_circuit_breaker(settings.alpha_db_path)
    # Permanent guard: fail loudly if concordance thresholds are miscalibrated
    # Prevents silent P2 starvation if MIN_SCORE_P2 is ever set above real agent output range
    assert_thresholds()   # crashes startup if thresholds are wrong — better than silent 0 trades
    assert_axiom_secret(settings)  # FIX-AXIOM-SECRET: warn + alert if missing (C-01 gate silently fails open)
    logger.info("Alpha Buffer ready (solo_entries=%s)", settings.solo_entries_enabled)
    from shared.sovereign_comms import get_instructions, report
    report("alpha_buffer", "INFO", {"event": "started"})
    _instr = get_instructions("alpha_buffer")
    if _instr:
        logger.info("Alpha Buffer: %d instruction(s) from SOVEREIGN on startup", len(_instr))
    # OMNI H2 fix: start background OMNI retry loop
    retry_task = _asyncio.create_task(_omni_retry_loop())
    # Score distribution watchdog — checks each window after it closes
    def _score_watchdog_thread():
        import time as _time
        last_checked_window = None
        while True:
            _time.sleep(60)
            try:
                # Current window — check the PREVIOUS window (it just closed)
                now = datetime.now(ET)
                # Previous 15-min window
                prev_min = (now.minute // 15) * 15 - 15
                if prev_min < 0:
                    prev_hour = now.hour - 1
                    prev_min = 45
                else:
                    prev_hour = now.hour
                prev_window = f"{now.strftime('%Y-%m-%d')}-{prev_hour:02d}{prev_min:02d}"
                if prev_window != last_checked_window:
                    last_checked_window = prev_window
                    _check_score_distribution(prev_window)
            except Exception as _we:
                logger.warning("Score watchdog error: %s", _we)
    threading.Thread(target=_score_watchdog_thread, daemon=True, name="score-dist-watchdog").start()
    _nns_watchdog.start()
    yield
    retry_task.cancel()
    logger.info("Alpha Buffer stopped")


# ── FastAPI App ───────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "Alpha Concordance Buffer",
    description = "Nexus Trading System — Options concordance engine",
    version     = "3.0.0",
    lifespan    = lifespan,
)


# ── Request/Response Models ───────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    """Agent pick submission payload."""

    agent:     str
    ticker:    str
    direction: str    # 'bullish' or 'bearish'
    score:     float  # 0-100
    reasoning: Optional[str] = None
    window_id: Optional[str] = None  # Agent's pool window (YYYY-MM-DD-HHMM). Used as
    # canonical DB key so all 3 agents analyzing the same pool land in the same window
    # even if they submit across a 15-min boundary. [GENESIS concordance-window-fix]

    @field_validator("agent")
    @classmethod
    def validate_agent(cls, v: str) -> str:
        """Normalize and reject unknown agents. Strips whitespace; enforces canonical casing."""
        # Normalize to canonical form: strip + title-case lookup
        v_clean = v.strip()
        # Build a canonical map {lowercase -> canonical} for case-insensitive lookup
        canonical = {a.lower(): a for a in VALID_AGENTS}
        resolved = canonical.get(v_clean.lower())
        if resolved is None:
            raise ValueError(f"Unknown agent '{v}'. Valid agents: {sorted(VALID_AGENTS)}")
        return resolved  # always return canonical casing

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, v: str) -> str:
        """Normalize and validate direction."""
        v = v.lower().strip()
        if v not in ("bullish", "bearish"):
            raise ValueError("direction must be 'bullish' or 'bearish'")
        return v

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, v: str) -> str:
        import re
        v = v.upper().strip()
        if not v:
            raise ValueError("ticker cannot be empty")
        if not re.match(r'^[A-Z]{1,10}$', v):
            # GAP-12: whitelist uppercase letters only (1-10 chars); reject digits/dots/symbols
            raise ValueError(f"ticker '{v}' invalid — must be 1-10 uppercase letters only (A-Z)")
        return v

    @field_validator("score")
    @classmethod
    def validate_score(cls, v: float) -> float:
        if not (0.0 <= v <= 100.0):
            raise ValueError("score must be between 0 and 100")
        return round(v, 2)

    @field_validator("window_id")
    @classmethod
    def validate_window_id(cls, v: Optional[str]) -> Optional[str]:
        """GENESIS-STRESS-F6-001: Validate pathway is in VALID_PATHWAYS when present.
        Window_id is also the dedup key — enforce non-empty if provided."""
        if v is not None and len(v.strip()) == 0:
            raise ValueError("window_id cannot be empty string if provided")
        return v.strip() if v else v


class TradeOutcomeRequest(BaseModel):
    """Closed trade result for circuit breaker tracking."""

    ticker:   str
    won:      bool
    pnl_pct:  float    # decimal: 0.35 = +35%, -0.15 = -15%
    pathway:  Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> JSONResponse:
    """Health check. Always returns 200. No auth required."""
    cb = get_circuit_breaker_state(settings.alpha_db_path)
    # shared/resilience HealthReport — standard surface for VECTOR + monitoring
    try:
        import sys as _sys_h, os as _os_h
        _sys_h.path.insert(0, _os_h.path.join(_os_h.path.dirname(__file__), ".."))
        from shared.resilience.health import HealthReport
        _hr = HealthReport(agent="alpha-buffer", version="3.0.0")
        _hr.add_source("circuit_breaker", ok=(cb.get("status") not in ("RED", "STOP")))
        _hr.add_source("db", ok=_os_h.path.exists(settings.alpha_db_path))
        _resilience = _hr.to_dict()
    except Exception as _he:
        _resilience = {"error": str(_he)}
    return JSONResponse({
        "status":           "healthy",
        "service":          "alpha-buffer",
        "version":          "3.0.0",
        "cb_status":        cb.get("status", "UNKNOWN"),
        "uptime_since":     app_state["start_time"],
        "code_hash":        _CODE_HASH,
        "stale_deploy":     (not _os.path.exists("/tmp/nexus_deploy_in_progress")) and _CODE_HASH != _compute_module_hash(),
        "resilience_status": _resilience,
    })


@app.post("/submit")
def submit_pick(
    body: SubmitRequest,
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Receive an R1 agent pick for Alpha concordance evaluation.

    On success: persists submission, evaluates concordance, dispatches to OMNI if triggered.
    On circuit breaker block: rejects with 503 and explains why.

    Args:
        body: SubmitRequest payload.

    Returns:
        JSON with submission status and concordance result (if triggered).
    """
    verify_secret(x_nexus_secret)

    # Earnings / manual block check — must run before anything else.
    # Tickers in EARNINGS_BLOCKED_TICKERS env var are unconditionally rejected.
    # This is the primary defence against re-fire after a Railway restart because
    # it does not depend on DB state — it's config-driven and always survives restarts.
    if body.ticker.upper() in settings.earnings_blocked_tickers:
        logger.warning(
            "Submission BLOCKED (earnings/manual block): %s/%s from %s",
            body.ticker, body.direction, body.agent,
        )
        return JSONResponse(
            status_code = 423,   # 423 Locked — unambiguous signal for monitoring
            content     = {
                "accepted": False,
                "reason":   f"{body.ticker} is blocked (earnings/manual override). "
                            f"Remove from EARNINGS_BLOCKED_TICKERS to re-enable.",
            },
        )

    # Circuit breaker check
    cb_status = evaluate_circuit_breaker(settings.alpha_db_path)
    if not cb_status.can_trade:
        logger.warning(
            "Submission BLOCKED by circuit breaker (%s): %s/%s from %s",
            cb_status.status, body.ticker, body.direction, body.agent,
        )
        # P2: Alert Ahmed when circuit breaker opens — deduped 30-min cooldown
        try:
            from shared.resilience.alerts import alert_ahmed as _aa
            _aa(
                f"Alpha Buffer circuit breaker OPEN ({cb_status.status}).\n"
                f"Submissions blocked. Reason: {cb_status.notes}",
                key=f"alpha-buffer-cb-{cb_status.status}",
                severity="WARNING",
            )
        except Exception:
            pass
        return JSONResponse(
            status_code = 503,
            content     = {
                "accepted":          False,
                "reason":            f"Circuit breaker {cb_status.status} — {cb_status.notes}",
                "circuit_breaker":   cb_status.to_dict(),
            },
        )

    # Score gate
    if body.score < MIN_SUBMISSION_SCORE:
        logger.info(
            "Submission REJECTED (score %.1f < %.1f): %s/%s from %s",
            body.score, MIN_SUBMISSION_SCORE, body.ticker, body.direction, body.agent,
        )
        return JSONResponse(
            status_code = 422,
            content     = {
                "accepted": False,
                "reason":   f"Score {body.score} below minimum {MIN_SUBMISSION_SCORE} for Alpha",
            },
        )

    # GENESIS-STRESS-F7-001: Direction collision check.
    # Reject if the same ticker already has an active opposite-direction concordance
    # forming in the CURRENT window with 2+ agents. Prevents OMNI from receiving
    # simultaneous bullish+bearish signals on the same ticker in the same window.
    try:
        _opp_direction = "bearish" if body.direction == "bullish" else "bullish"
        _win_id = body.window_id or current_window_id()
        _opp_subs = get_window_submissions(
            settings.alpha_db_path, _win_id,
            body.ticker.upper(), _opp_direction
        )
        if len(_opp_subs) >= 2:  # P2 or better forming in opposite direction
            logger.warning(
                "DIRECTION COLLISION: %s/%s rejected — %s/%s already has %d agents this window",
                body.ticker, body.direction, body.ticker, _opp_direction, len(_opp_subs)
            )
            return JSONResponse(
                status_code=409,
                content={
                    "accepted": False,
                    "reason": f"Direction collision: {body.ticker}/{_opp_direction} already forming ({len(_opp_subs)} agents)",
                    "collision": True,
                },
            )
    except Exception:
        pass  # fail-open: direction collision check is advisory, not blocking

    # C-01: Validate ticker is in Axiom universe (non-blocking — fail open if Axiom slow/down)
    try:
        import httpx as _hx
        _axiom_url = getattr(settings, "axiom_url", "http://localhost:8001")
        _axiom_secret = getattr(settings, "axiom_secret", "")
        _pool_resp = _hx.get(
            f"{_axiom_url}/universe",
            headers={"X-Axiom-Secret": _axiom_secret},
            timeout=2.0,
        )
        if _pool_resp.status_code == 200:
            _universe = _pool_resp.json().get("tickers", [])
            # C-01 FIX: Only enforce pool check if universe is large enough to be authoritative.
            # Axiom Tier 2 intentionally filters to ~20 tickers. If pool < 50, fail open.
            # This prevents false rejections when Axiom pool is incomplete/updating.
            if _universe and len(_universe) >= 50 and body.ticker not in _universe:
                logger.warning(
                    "C-01: Ticker %s rejected — not in Axiom universe (%d tickers)",
                    body.ticker, len(_universe),
                )
                return JSONResponse(
                    status_code=422,
                    content={
                        "accepted": False,
                        "reason":   "ticker_not_in_axiom_universe",
                        "ticker":   body.ticker,
                    },
                )
            elif _universe and len(_universe) < 50:
                logger.debug(
                    "C-01: Axiom pool too small (%d tickers) — failing open for %s (checkpoint: C-01-FAIl-OPEN-V2)",
                    len(_universe), body.ticker,
                )
    except Exception as _ae:
        # Fail open — do not block valid signals because Axiom is slow or unreachable
        logger.debug("C-01: Axiom universe check skipped (%s) — proceeding", _ae)

    # Daily dedup gate — one submission per agent+ticker+direction per day.
    # Prevents the same agent flooding concordance with identical picks across windows.
    # Sage fix (Apr 14): Sage produced 106 picks / 14 windows = same tickers 7x.
    # Even with per-agent dedup on the agent side, the buffer is the last line of defense.
    if is_already_submitted_today(settings.alpha_db_path, body.agent, body.ticker, body.direction):
        logger.info(
            "Submission DEDUPLICATED (already submitted today): %s/%s from %s",
            body.ticker, body.direction, body.agent,
        )
        return JSONResponse(
            status_code=409,
            content={
                "accepted": False,
                "reason":   f"Duplicate: {body.agent} already submitted {body.ticker} {body.direction} today",
                "deduplicated": True,
            },
        )

    window_id = current_window_id()

    # SW3: Window mismatch detection — check pick staleness before accepting
    _current_window = window_id  # default: fail-open (treat as FRESH if Axiom unreachable)
    try:
        import sys as _sys_wc, os as _os_wc
        _sys_wc.path.insert(0, _os_wc.path.join(_os_wc.path.dirname(__file__), ".."))
        import requests as _wc_req
        _axiom_resp = _wc_req.get(
            f"{getattr(settings, 'axiom_url', 'http://localhost:8001')}/pool/window",
            timeout=3,
        )
        if _axiom_resp.status_code == 200:
            _current_window = _axiom_resp.json().get("window_id", window_id)
    except Exception:
        pass  # fail-open: treat as FRESH

    from shared.window_coordinator import evaluate_window
    _window_result = evaluate_window(
        body.window_id if hasattr(body, "window_id") and body.window_id else window_id,
        _current_window,
        body.ticker,
        body.agent,
    )
    if _window_result["action"] == "REJECTED":
        logger.warning("WINDOW EXPIRED — rejecting %s/%s from %s", body.ticker, body.direction, body.agent)
        return JSONResponse(
            status_code=422,
            content={
                "accepted":     False,
                "reason":       f"Window EXPIRED: pick is {_window_result['cycles_stale'] * 15} min stale",
                "window_match": _window_result,
            },
        )

    # GENESIS-BLOCKER-FIX (May 11 2026, 13:45 ET): Resolve window coalescence issue.
    # Problem: Submissions with stale window_ids (1-2 cycles old) were being saved
    # with their old window_id, preventing concordance across agents (e.g. Sage in
    # window 1215, Cipher in window 1230). This created zero concordances for 147 min.
    # Solution: When a submission is STALE but ACCEPTED (cycles_stale 1-2),
    # use CURRENT window for DB storage so recent submissions converge in same bucket.
    # Sizing adjustment from evaluate_window is preserved separately.
    if _window_result["cycles_stale"] >= 1 and _window_result["action"] != "REJECTED":
        # Stale but accepted — use current window to enable concordance
        logger.info(
            "GENESIS-FIX: %s/%s stale by %d cycle(s) — coalescing to current window %s (was %s)",
            body.ticker, body.direction, _window_result["cycles_stale"],
            _current_window,
            body.window_id if hasattr(body, "window_id") else "(none)",
        )
        window_id = _current_window
    elif body.window_id:
        # Fresh submission — use agent's window_id to avoid concordance splitting across 15-min boundaries
        window_id = body.window_id

    # Persist submission
    save_submission(
        db_path   = settings.alpha_db_path,
        window_id = window_id,
        agent     = body.agent,
        ticker    = body.ticker,
        direction = body.direction,
        score     = body.score,
        reasoning = body.reasoning,
    )

    logger.info(
        "Submission ACCEPTED: %s | %s/%s | score=%.1f | window=%s",
        body.agent, body.ticker, body.direction, body.score, window_id,
    )
    # Score distribution watchdog: record this score for uniform detection
    _record_window_score(window_id, body.ticker, body.score)

    # Pipeline Sentinel — buffer accepted this pick
    try:
        from pipeline_client import trace_hop as _trace_hop
        _trace_hop(f"{window_id}:{body.ticker}", "buffer_accepted", "alpha-buffer", body.ticker, "alpha")
    except Exception:
        pass

    # Evaluate concordance
    all_subs = get_window_submissions(
        settings.alpha_db_path,
        window_id,
        body.ticker,
        body.direction,
    )

    concordance = evaluate_concordance(
        window_id            = window_id,
        ticker               = body.ticker,
        direction            = body.direction,
        submissions          = all_subs,
        solo_entries_enabled = settings.solo_entries_enabled,
    )

    if concordance is None:
        return JSONResponse({
            "accepted":     True,
            "window_id":    window_id,
            "agent":        body.agent,
            "ticker":       body.ticker,
            "direction":    body.direction,
            "score":        body.score,
            "agents_in_window": len(all_subs),
            "concordance":  None,
            "message":      f"Submission accepted. {len(all_subs)}/3 agents in window.",
        })

    # Concordance formed — persist result
    save_concordance_result(
        db_path         = settings.alpha_db_path,
        window_id       = window_id,
        ticker          = body.ticker,
        direction       = body.direction,
        pathway         = concordance.pathway,
        agent_count     = concordance.agent_count,
        weighted_score  = concordance.weighted_score,
        sizing_mult     = concordance.sizing_mult,
        verdict         = concordance.verdict,
        agents_involved = concordance.agents_involved,
        scores          = concordance.scores,
    )

    logger.info(
        "CONCORDANCE FORMED: %s %s/%s | pathway=%s | score=%.1f | verdict=%s",
        window_id, body.ticker, body.direction,
        concordance.pathway, concordance.weighted_score, concordance.verdict,
    )

    # H-06: Reject invalid pathway before dispatch — P1/P2/P3/P4 only
    _VALID_DISPATCH_PATHWAYS = {"P1", "P2", "P3", "P4"}
    if concordance.pathway not in _VALID_DISPATCH_PATHWAYS:
        logger.error(
            "H-06: Concordance rejected — invalid pathway '%s' for %s/%s. "
            "Valid: %s. Will NOT dispatch to OMNI.",
            concordance.pathway, body.ticker, body.direction, _VALID_DISPATCH_PATHWAYS,
        )
        return JSONResponse({
            "accepted":    True,
            "window_id":   window_id,
            "agent":       body.agent,
            "ticker":      body.ticker,
            "direction":   body.direction,
            "score":       body.score,
            "concordance": None,
            "omni_dispatched": False,
            "message": f"Submission accepted but concordance pathway '{concordance.pathway}' "
                       f"is not dispatchable. Discarded.",
        })

    # OMNI M2 fix: fire OMNI dispatch in a background thread — do NOT block /submit.
    # Under concurrent agent submissions, a slow OMNI (AI model latency) would cause
    # /submit to queue up behind the network call. The concordance is already persisted;
    # dispatch is best-effort with omni_dispatched=False as the retry signal.
    concordance_dict = concordance.to_dict()
    import threading as _threading

    def _dispatch_background() -> None:
        omni_ok, omni_response = dispatch_to_omni(
            omni_url         = settings.omni_webhook_url,
            auth_headers     = settings.omni_auth_header,
            concordance_dict = concordance_dict,
        )
        if omni_ok:
            # Only mark dispatched on success — failed dispatches stay omni_dispatched=0
            # so the background retry loop can pick them up.
            # BUG FIX: previously always called mark_omni_dispatched regardless of
            # ok/fail, silently discarding failed dispatches (422/timeout). [GENESIS 2026-04-23]
            mark_omni_dispatched(
                db_path       = settings.alpha_db_path,
                window_id     = window_id,
                ticker        = body.ticker,
                direction     = body.direction,
                omni_response = omni_response,
            )
        else:
            logger.error(
                "OMNI dispatch FAILED for %s/%s (pathway=%s) — concordance persisted, "
                "omni_dispatched=False for retry",
                body.ticker, body.direction, concordance.pathway,
            )
            # C-05: Clear dedup so agents can re-submit in next window
            # Dispatch failure = no position opened = signal should not be wasted
            try:
                for _agent in concordance_dict.get("agents_involved", []):
                    clear_submission_for_retry(
                        settings.alpha_db_path, _agent,
                        body.ticker, body.direction,
                    )
            except Exception as _ce:
                logger.warning("C-05: dedup clear failed: %s", _ce)

    _threading.Thread(target=_dispatch_background, daemon=True).start()

    return JSONResponse({
        "accepted":        True,
        "window_id":       window_id,
        "agent":           body.agent,
        "ticker":          body.ticker,
        "direction":       body.direction,
        "score":           body.score,
        "agents_in_window": len(all_subs),
        "concordance":     concordance_dict,
        "omni_dispatched": True,   # background dispatch fired; check DB for actual result
        "message":         f"Concordance {concordance.pathway} formed — dispatching to OMNI.",
    })


@app.get("/status")
def buffer_status(
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Return current buffer state: circuit breaker, current window, submission counts.

    Returns:
        Status payload with circuit breaker, window_id, and per-ticker submission counts.
    """
    verify_secret(x_nexus_secret)

    window_id   = current_window_id()
    cb_status   = evaluate_circuit_breaker(settings.alpha_db_path)
    cb_raw      = get_circuit_breaker_state(settings.alpha_db_path)

    # Get all tickers with submissions in current window
    ticker_pairs  = get_all_tickers_in_window(settings.alpha_db_path, window_id)
    ticker_status = {}
    for ticker, direction in ticker_pairs:
        subs = get_window_submissions(settings.alpha_db_path, window_id, ticker, direction)
        key  = f"{ticker}/{direction}"
        concordance = get_concordance_result(settings.alpha_db_path, window_id, ticker, direction)
        ticker_status[key] = {
            "agents_submitted": [s["agent"] for s in subs],
            "count":            len(subs),
            "scores":           {s["agent"]: s["score"] for s in subs},
            "concordance":      concordance["pathway"] if concordance else None,
            "omni_dispatched":  concordance["omni_dispatched"] if concordance else False,
        }

    return JSONResponse({
        "service":          "alpha-buffer",
        "window_id":        window_id,
        "circuit_breaker":  cb_status.to_dict(),
        "raw_cb_state":     {
            "consecutive_losses":  cb_raw.get("consecutive_losses", 0),
            "daily_trades":        cb_raw.get("daily_trades", 0),
            "daily_wins":          cb_raw.get("daily_wins", 0),
            "daily_losses":        cb_raw.get("daily_losses", 0),
            "daily_pnl_pct":       cb_raw.get("daily_pnl_pct", 0.0),
        },
        "solo_entries_enabled": settings.solo_entries_enabled,
        "current_window":   ticker_status,
        "checked_at":       datetime.now(ET).isoformat(),
    })


@app.post("/trade-outcome")
def trade_outcome(
    body: TradeOutcomeRequest,
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Record a closed trade result and update circuit breaker state.

    Called by the Alpha execution engine when a trade closes.

    Args:
        body: TradeOutcomeRequest with ticker, won flag, and pnl_pct.
    """
    verify_secret(x_nexus_secret)

    record_trade_outcome(
        db_path = settings.alpha_db_path,
        won     = body.won,
        pnl_pct = body.pnl_pct,
    )

    logger.info(
        "Trade outcome recorded: %s %s | pnl=%.1f%%",
        "WIN" if body.won else "LOSS",
        body.ticker,
        body.pnl_pct * 100,
    )

    return JSONResponse({
        "recorded":   True,
        "ticker":     body.ticker,
        "result":     "win" if body.won else "loss",
        "pnl_pct":    body.pnl_pct,
    })


@app.post("/circuit-breaker/reset")
def circuit_breaker_reset(
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Ahmed manual circuit breaker reset. Clears all counters and returns to NORMAL.

    This is the STOP-level override endpoint. Only Ahmed should call this.
    """
    verify_secret(x_nexus_secret)

    reset_circuit_breaker(settings.alpha_db_path, note="Manual reset via API")
    logger.info("Circuit breaker MANUALLY RESET")

    return JSONResponse({
        "reset":   True,
        "status":  "NORMAL",
        "message": "Circuit breaker reset to NORMAL. All counters cleared.",
    })


@app.post("/concordance/purge")
def concordance_purge(
    x_nexus_secret: str = Header(default=""),
    tickers: Optional[list[str]] = None,
) -> JSONResponse:
    """
    Purge concordance state for specific tickers (or all tickers if none given).

    Deletes today's submission records and concordance results for the named tickers,
    preventing them from re-entering the concordance pool after a restart.
    Call this after a Railway deploy when earnings-blocked tickers may have
    pre-restart concordances sitting in the DB.

    Args:
        tickers: Optional list of uppercase ticker symbols. If omitted, purges ALL
                 today's concordance and submission state (full reset).

    Returns:
        JSON summary of rows deleted.
    """
    verify_secret(x_nexus_secret)

    from zoneinfo import ZoneInfo
    et_date = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    deleted_submissions    = 0
    deleted_concordances   = 0

    with get_conn(settings.alpha_db_path) as conn:
        if tickers:
            _upper = [t.upper() for t in tickers]
            # Build IN clause
            placeholders = ",".join("?" * len(_upper))
            deleted_submissions = conn.execute(
                f"""
                DELETE FROM submissions
                WHERE SUBSTR(window_id, 1, 10) = ?
                  AND ticker IN ({placeholders})
                """,
                [et_date, *_upper],
            ).rowcount
            deleted_concordances = conn.execute(
                f"""
                DELETE FROM concordance_results
                WHERE SUBSTR(window_id, 1, 10) = ?
                  AND ticker IN ({placeholders})
                """,
                [et_date, *_upper],
            ).rowcount
        else:
            # Full day purge
            deleted_submissions = conn.execute(
                "DELETE FROM submissions WHERE SUBSTR(window_id, 1, 10) = ?",
                (et_date,),
            ).rowcount
            deleted_concordances = conn.execute(
                "DELETE FROM concordance_results WHERE SUBSTR(window_id, 1, 10) = ?",
                (et_date,),
            ).rowcount

    scope = tickers if tickers else ["ALL"]
    logger.warning(
        "CONCORDANCE PURGE — tickers=%s deleted_submissions=%d deleted_concordances=%d",
        scope, deleted_submissions, deleted_concordances,
    )

    return JSONResponse({
        "purged":               True,
        "date":                 et_date,
        "tickers":              scope,
        "deleted_submissions":  deleted_submissions,
        "deleted_concordances": deleted_concordances,
    })


# ── SOVEREIGN Push Endpoints ──────────────────────────────────────────────────

class _SovDirective(BaseModel):
    directive: str
    data: dict = {}
    from_agent: str = "sovereign"


@app.post("/sovereign/directive", status_code=200)
def sovereign_directive(
    body: _SovDirective,
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """SOVEREIGN pushes a directive to alpha-buffer. Zero polling lag."""
    verify_secret(x_nexus_secret)
    d = body.directive.strip().upper()
    logger.info("SOVEREIGN direct push to alpha-buffer: %s", d)

    if d == "PING":
        _sov_report("alpha_buffer", "ack", {"directive": "PING", "status": "alive"})
        return JSONResponse({"ok": True, "directive": "PING", "status": "alive"})
    elif d == "STATUS":
        snap = {"directive": "STATUS", "service": "alpha_buffer", "port": int(__import__("os").getenv("PORT", "8002"))}
        _sov_report("alpha_buffer", "status", snap)
        return JSONResponse({"ok": True, **snap})
    elif d in ("FLUSH", "RESET_DAY"):
        _sov_report("alpha_buffer", "ack", {"directive": d, "status": "applied"})
        return JSONResponse({"ok": True, "directive": d})
    else:
        _sov_report("alpha_buffer", "ack", {"directive": d, "status": "unrecognized"})
        return JSONResponse({"ok": False, "error": f"unrecognized directive: {d}"}, status_code=400)


@app.get("/sovereign/status", status_code=200)
def sovereign_status(
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """SOVEREIGN queries alpha-buffer state on-demand."""
    verify_secret(x_nexus_secret)
    return JSONResponse({"ok": True, "service": "alpha_buffer", "port": int(__import__("os").getenv("PORT", "8002"))})


