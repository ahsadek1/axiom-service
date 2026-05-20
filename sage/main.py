"""
main.py — Sage Agent Service

FastAPI service that receives ticker pools from Axiom, analyzes each ticker
for fundamental and macro trade quality using DeepSeek, and submits qualifying
picks to Alpha Buffer and Prime Buffer.

Port: 9003
Auth: X-Nexus-Secret header on inbound requests
"""

import logging
import os
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Optional, Union

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.log_setup import configure_service_logging
from shared.sovereign_comms import EscalationLevel, get_instructions, report

import pytz
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from analyzer import AGENT_NAME, analyze
from shared.decision_log import (
    log_decision,
    SUBMITTED, REJECTED, BRAIN_FAIL, ORACLE_MISS, HALTED as DL_HALTED,
)
from buffer_client import submit_to_alpha, submit_to_prime
from config import Settings, load_settings
from database import (
    has_submitted_today,
    complete_window,
    get_stats,
    get_today_picks,
    init_db,
    is_duplicate_window,
    record_pick,
    record_window_received,
)
from oracle_client import fetch_context
from telegram import alert_brain_down, alert_submission_failed

configure_service_logging("sage")
logger = logging.getLogger("sage.main")
ET = pytz.timezone("America/New_York")

_settings: Optional[Settings] = None
_consecutive_brain_failures: int = 0
BRAIN_ALERT_THRESHOLD = 3
MAX_PICKS_PER_WINDOW = 5          # Top N picks per window — prevents firehose behavior
_submitted_today: set = set()     # Daily dedup: ticker → submitted this session
_restart_count: int = 0           # Track restarts to detect crash loops
MAX_RESTART_CYCLES = 5            # Slow down after 5 rapid restarts

# ── SOVEREIGN Directive State ─────────────────────────────────────────────────
_sovereign_halted: bool = False
_score_threshold_override: Optional[float] = None


def _dispatch_sovereign_instruction(instr: dict) -> None:
    """Execute a SOVEREIGN instruction. Supported: HALT, RESUME, STATUS, SET_THRESHOLD, FLUSH."""
    global _sovereign_halted, _score_threshold_override, _consecutive_brain_failures, _submitted_today

    raw = instr.get("message", "").strip()
    directive = raw.split(":", 1)[0].strip().upper() if ":" in raw else raw.upper()
    rest = raw.split(":", 1)[1].strip() if ":" in raw else ""

    logger.info("SOVEREIGN directive received — raw: %s", raw[:200])

    if directive == "HALT":
        _sovereign_halted = True
        logger.warning("SOVEREIGN DIRECTIVE: HALT — pool analysis suspended")
        report("sage", "ack", {"directive": "HALT", "status": "applied", "halted": True})
    elif directive == "RESUME":
        _sovereign_halted = False
        logger.info("SOVEREIGN DIRECTIVE: RESUME — pool analysis resumed")
        report("sage", "ack", {"directive": "RESUME", "status": "applied", "halted": False})
    elif directive == "STATUS":
        stats = get_stats(_settings.db_path) if _settings else {}
        report("sage", "status", {"directive": "STATUS", "halted": _sovereign_halted,
                                   "brain_failures": _consecutive_brain_failures,
                                   "submitted_today": len(_submitted_today), **stats})
        logger.info("SOVEREIGN DIRECTIVE: STATUS — reported back")
    elif directive == "SET_THRESHOLD":
        try:
            val = float(rest)
            _score_threshold_override = val
            logger.info("SOVEREIGN DIRECTIVE: SET_THRESHOLD — threshold set to %.1f", val)
            report("sage", "ack", {"directive": "SET_THRESHOLD", "value": val, "status": "applied"})
        except ValueError:
            report("sage", "ack", {"directive": "SET_THRESHOLD", "value": rest, "status": "invalid"})
    elif directive == "FLUSH":
        _consecutive_brain_failures = 0
        _submitted_today = set()
        logger.info("SOVEREIGN DIRECTIVE: FLUSH — counters and daily dedup reset")
        report("sage", "ack", {"directive": "FLUSH", "status": "applied"})
    else:
        logger.warning("SOVEREIGN DIRECTIVE: unrecognized '%s'", directive[:100])
        report("sage", "ack", {"directive": directive[:100], "status": "unrecognized"})


def _midnight_reset() -> None:
    """Reset daily dedup set at midnight ET. Runs as a daemon thread."""
    import time as _time
    while True:
        now_et = datetime.now(ET)
        # Calculate seconds until next midnight ET
        next_midnight = (now_et + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        sleep_seconds = (next_midnight - now_et).total_seconds()
        _time.sleep(sleep_seconds)
        global _submitted_today
        prev_count = len(_submitted_today)
        _submitted_today = set()
        logger.info("Daily dedup reset — cleared %d tickers", prev_count)



# ── SOVEREIGN Continuous Comms Loop ──────────────────────────────────────────
_comms_stop_sage = threading.Event()
SOVEREIGN_POLL_INTERVAL_S      = 30   # Poll SOVEREIGN inbox every 30 seconds
SOVEREIGN_HEARTBEAT_INTERVAL_S = 300  # Push heartbeat every 5 minutes


def _sovereign_comms_loop_sage() -> None:
    """
    Daemon thread: polls SOVEREIGN inbox every 30s and pushes a heartbeat
    every 5 minutes, independent of any pool cycle. Never raises.
    """
    last_heartbeat: float = 0.0
    logger.info("SOVEREIGN comms loop started (poll=%ds heartbeat=%ds)",
                SOVEREIGN_POLL_INTERVAL_S, SOVEREIGN_HEARTBEAT_INTERVAL_S)
    while not _comms_stop_sage.is_set():
        now = time.monotonic()
        try:
            instructions = get_instructions("sage")
            if instructions:
                logger.info("SOVEREIGN comms: %d directive(s) received", len(instructions))
                for instr in instructions:
                    _dispatch_sovereign_instruction(instr)
            if now - last_heartbeat >= SOVEREIGN_HEARTBEAT_INTERVAL_S:
                stats = get_stats(_settings.db_path) if _settings else {}
                report("sage", "AUTONOMOUS", {
                    "event":              "heartbeat",
                    "halted":             _sovereign_halted,
                    "brain_failures":     _consecutive_brain_failures,
                    "ts":                 __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    **stats,
                })
                last_heartbeat = now
        except Exception as exc:  # noqa: BLE001
            logger.warning("SOVEREIGN comms loop error: %s", exc)
        _comms_stop_sage.wait(SOVEREIGN_POLL_INTERVAL_S)
    logger.info("SOVEREIGN comms loop stopped")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings, _submitted_today, _restart_count
    _settings = load_settings()
    from shared.db_guard import assert_unique_db_path  # G4: collision guard
    assert_unique_db_path("sage", _settings.db_path)
    init_db(_settings.db_path)
    
    # Increment restart counter and warn if crash loop detected
    _restart_count += 1
    if _restart_count > MAX_RESTART_CYCLES:
        logger.critical("Sage: %d rapid restarts detected — possible crash loop. Slowing startup." % _restart_count)
        time.sleep(5)  # Back off on startup to avoid tight restart loop
    
    # Rebuild in-memory submitted_today from database (restart-safe)
    try:
        today_picks = get_today_picks(_settings.db_path)
        _submitted_today = set((p["ticker"] for p in today_picks))
        logger.info("In-memory dedup rebuilt from DB: %d tickers marked as submitted today", len(_submitted_today))
    except Exception as e:
        logger.warning("Failed to rebuild in-memory dedup: %s", e)
        _submitted_today = set()
    
    # Start midnight reset thread
    reset_thread = threading.Thread(target=_midnight_reset, daemon=True, name="sage-midnight-reset")
    reset_thread.start()
    logger.info("Sage agent started on port %d [restart #%d]", _settings.port, _restart_count)
    report("sage", "status", {"event": "started", "port": _settings.port, "restart_count": _restart_count})
    # Drain queued directives from before this startup
    _startup_instr = get_instructions("sage")
    if _startup_instr:
        logger.info("Sage: %d instruction(s) from SOVEREIGN on startup", len(_startup_instr))
        for _i in _startup_instr:
            _dispatch_sovereign_instruction(_i)

    # Launch continuous comms loop (polls every 30s, heartbeat every 5min)
    _comms_stop_sage.clear()
    threading.Thread(target=_sovereign_comms_loop_sage, daemon=True, name="sage-sovereign-comms").start()

    yield

    _comms_stop_sage.set()
    report("sage", "status", {"event": "stopped"})
    logger.info("Sage agent shutting down")


app = FastAPI(title="Sage Agent", version="1.0.0", lifespan=lifespan)


class PoolPayload(BaseModel):
    pool: list
    count: int
    window_id: str
    updated_at: str
    market_open: bool = True
    regime: dict = {}
    coherence_summary: Union[list, dict] = []   # Axiom sends dict; list accepted for legacy
    coherence_available: bool = False
    echo_chamber_risk: list = []
    cycle_patterns: list = []
    pattern_intelligence_available: bool = False
    oracle_warmed: bool = False

    model_config = {"extra": "ignore"}  # tolerate any future Axiom payload additions


def _check_auth(provided: Optional[str]) -> None:
    if not provided or not secrets.compare_digest(provided, _settings.nexus_secret):
        logger.warning("Unauthorized request — invalid or missing X-Nexus-Secret")
        raise HTTPException(status_code=401, detail="Unauthorized")


def _analyze_pool(payload: dict) -> None:
    """
    Analyze all tickers in the pool. Runs in background thread.

    Behavior:
      - Scores all tickers via DeepSeek
      - Deduplicates: skips any ticker already submitted today (_submitted_today)
      - Caps submissions at MAX_PICKS_PER_WINDOW (top scorers only)
      - Submits top picks to Alpha Buffer and Prime Buffer
    """
    global _consecutive_brain_failures, _submitted_today

    window_id: str = payload["window_id"]
    pool: list = payload["pool"]
    regime: dict = payload.get("regime", {})

    # Poll SOVEREIGN for any mid-session instructions (deduped via watermark)
    _instr = get_instructions("sage")
    if _instr:
        logger.info("Sage: %d instruction(s) from SOVEREIGN this cycle", len(_instr))
        for _i in _instr:
            _dispatch_sovereign_instruction(_i)

    # Halt gate
    if _sovereign_halted:
        logger.warning("Sage: SOVEREIGN HALT active — skipping window %s", window_id)
        report("sage", "status", {"event": "pool_skipped", "reason": "sovereign_halt", "window_id": window_id})
        complete_window(_settings.db_path, window_id, 0, 0)
        return

    analyzed = 0
    submitted = 0
    candidates: list = []  # [(score, ticker, direction, reasoning)]

    try:
        from pipeline_client import trace_hop as _trace_hop
    except Exception:
        _trace_hop = None  # type: ignore[assignment]

    try:
        # --- Deterministic scoring via sage_scorer (replaces DeepSeek LLM) ---
        from sage_scorer import score_pool as _sage_score_pool

        # Dedup: skip tickers already submitted today (in-memory + DB)
        valid_tickers = [
            t for t in pool
            if isinstance(t, str)
            and t not in _submitted_today
            and not has_submitted_today(_settings.db_path, t, "bullish")
            and not has_submitted_today(_settings.db_path, t, "bearish")
        ]

        scored_results = _sage_score_pool(valid_tickers, window_id=window_id)

        import json as _json
        _regime_json = _json.dumps(regime) if regime else None

        # No MAX_PICKS_PER_WINDOW cap in POC — every qualifying signal fires
        for result in scored_results:
            ticker = result.ticker
            analyzed += 1

            if _trace_hop:
                _trace_hop(f"{window_id}:{ticker}", "agent_received", "sage", ticker, "alpha")

            if result.hard_block:
                logger.info("BLOCKED %s: %s", ticker, result.block_reason)
                log_decision(
                    _settings.db_path, AGENT_NAME, window_id, ticker, REJECTED,
                    direction="N/A", score=0.0, threshold=45.0,
                    reasoning=result.block_reason,
                )
                continue

            direction = result.direction
            reasoning = result.reasoning

            if result.llm_flag:
                logger.info("LLM review flagged %s: triggers=%s", ticker, result.llm_triggers)

            alpha_ok = submit_to_alpha(
                ticker, AGENT_NAME, direction, result.alpha_score, reasoning,
                _settings.alpha_buffer_url, _settings.alpha_headers(),
                window_id=window_id,
            )
            prime_ok = submit_to_prime(
                ticker, AGENT_NAME, direction, result.prime_score, reasoning,
                _settings.prime_buffer_url, _settings.prime_headers(),
                window_id=window_id,
            )

            if alpha_ok or prime_ok:
                submitted += 1
                _submitted_today.add(ticker)
                record_pick(
                    _settings.db_path, window_id, ticker, direction,
                    result.alpha_score, reasoning, alpha_ok, prime_ok,
                )
                log_decision(
                    _settings.db_path, AGENT_NAME, window_id, ticker, SUBMITTED,
                    direction=direction, score=result.alpha_score,
                    threshold=45.0, reasoning=reasoning,
                    alpha_submitted=alpha_ok, prime_submitted=prime_ok,
                    regime=_regime_json,
                )
            else:
                log_decision(
                    _settings.db_path, AGENT_NAME, window_id, ticker, REJECTED,
                    direction=direction, score=result.alpha_score,
                    threshold=45.0, reasoning=reasoning, regime=_regime_json,
                )

    except Exception as e:
        logger.error("_analyze_pool crashed for window %s at analyzed=%d: %s", window_id, analyzed, e)
        report("sage", "incident", {"event": "pool_crash", "window_id": window_id, "error": str(e)[:200]}, escalation=EscalationLevel.CRITICAL)
    finally:
        complete_window(_settings.db_path, window_id, analyzed, submitted)
        logger.info("Window %s complete — analyzed=%d candidates=%d submitted=%d",
                    window_id, analyzed, len(candidates), submitted)
        report("sage", "status", {"event": "window_complete", "window_id": window_id, "analyzed": analyzed, "submitted": submitted}, escalation=EscalationLevel.INFO)


@app.post("/receive-pool", status_code=200)
async def receive_pool(
    payload: PoolPayload,
    x_nexus_secret: Optional[str] = Header(None, alias="X-Nexus-Secret"),
) -> JSONResponse:
    _check_auth(x_nexus_secret)

    window_id = payload.window_id
    if is_duplicate_window(_settings.db_path, window_id):
        logger.info("Duplicate window %s — skipping", window_id)
        return JSONResponse({"status": "duplicate", "window_id": window_id})

    record_window_received(_settings.db_path, window_id, payload.count)

    thread = threading.Thread(
        target=_analyze_pool,
        args=(payload.model_dump(),),
        daemon=True,
        name=f"sage-{window_id}",
    )
    thread.start()

    logger.info("Pool received — window=%s tickers=%d regime=%s",
                window_id, payload.count, payload.regime.get("classification", "?"))
    return JSONResponse({"status": "accepted", "window_id": window_id, "agent": AGENT_NAME})


@app.get("/health")
async def health() -> JSONResponse:
    stats = get_stats(_settings.db_path) if _settings else {}
    return JSONResponse({"status": "ok", "agent": AGENT_NAME, "port": _settings.port if _settings else 9003,
                         "brain_failures": _consecutive_brain_failures, **stats})


@app.get("/picks/today")
async def picks_today(x_nexus_secret: Optional[str] = Header(None, alias="X-Nexus-Secret")) -> JSONResponse:
    _check_auth(x_nexus_secret)
    picks = get_today_picks(_settings.db_path)
    return JSONResponse({"agent": AGENT_NAME, "count": len(picks), "picks": picks})


@app.get("/stats")
async def stats(x_nexus_secret: Optional[str] = Header(None, alias="X-Nexus-Secret")) -> JSONResponse:
    _check_auth(x_nexus_secret)
    return JSONResponse({"agent": AGENT_NAME, **get_stats(_settings.db_path)})


# ── SOVEREIGN Push Endpoints ─────────────────────────────────────────────────

class SovereignDirective(BaseModel):
    directive: str
    data: dict = {}
    from_agent: str = "sovereign"


@app.post("/sovereign/directive", status_code=200)
async def sovereign_directive(
    body: SovereignDirective,
    x_nexus_secret: Optional[str] = Header(None, alias="X-Nexus-Secret"),
) -> JSONResponse:
    """SOVEREIGN pushes a directive directly. Zero polling lag."""
    _check_auth(x_nexus_secret)
    global _consecutive_brain_failures, _score_threshold_override, _sovereign_halted, _submitted_today

    d = body.directive.strip().upper()
    logger.info("SOVEREIGN direct push: %s", d)

    if d == "PING":
        report(AGENT_NAME, "ack", {"directive": "PING", "status": "alive", "agent": AGENT_NAME})
        return JSONResponse({"ok": True, "directive": "PING", "status": "alive"})
    elif d == "HALT":
        _sovereign_halted = True
        report(AGENT_NAME, "ack", {"directive": "HALT", "status": "applied", "halted": True})
        return JSONResponse({"ok": True, "directive": "HALT", "halted": True})
    elif d == "RESUME":
        _sovereign_halted = False
        report(AGENT_NAME, "ack", {"directive": "RESUME", "status": "applied", "halted": False})
        return JSONResponse({"ok": True, "directive": "RESUME", "halted": False})
    elif d in ("FLUSH", "RESET_DAY"):
        _consecutive_brain_failures = 0
        _submitted_today = set()
        report(AGENT_NAME, "ack", {"directive": d, "status": "applied", "submitted_today_cleared": True})
        return JSONResponse({"ok": True, "directive": d})
    elif d == "SET_THRESHOLD":
        try:
            val = float(body.data.get("value", ""))
            _score_threshold_override = val
            report(AGENT_NAME, "ack", {"directive": "SET_THRESHOLD", "value": val, "status": "applied"})
            return JSONResponse({"ok": True, "directive": "SET_THRESHOLD", "threshold": val})
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "invalid value"}, status_code=400)
    elif d == "STATUS":
        stats = get_stats(_settings.db_path) if _settings else {}
        payload = {"directive": "STATUS", "agent": AGENT_NAME,
                   "halted": _sovereign_halted, "brain_failures": _consecutive_brain_failures,
                   "submitted_today": len(_submitted_today),
                   "threshold_override": _score_threshold_override, **stats}
        report(AGENT_NAME, "status", payload)
        return JSONResponse({"ok": True, **payload})
    else:
        report(AGENT_NAME, "ack", {"directive": d, "status": "unrecognized"})
        return JSONResponse({"ok": False, "error": f"unrecognized directive: {d}"}, status_code=400)


@app.get("/sovereign/status", status_code=200)
async def sovereign_status(
    x_nexus_secret: Optional[str] = Header(None, alias="X-Nexus-Secret"),
) -> JSONResponse:
    """SOVEREIGN queries full agent state on-demand."""
    _check_auth(x_nexus_secret)
    stats = get_stats(_settings.db_path) if _settings else {}
    return JSONResponse({
        "ok": True, "agent": AGENT_NAME,
        "halted": _sovereign_halted,
        "brain_failures": _consecutive_brain_failures,
        "submitted_today": len(_submitted_today),
        "threshold_override": _score_threshold_override,
        "port": _settings.port if _settings else 9003,
        **stats,
    })


if __name__ == "__main__":
    s = load_settings()
    uvicorn.run("main:app", host="0.0.0.0", port=s.port, log_level="info")
