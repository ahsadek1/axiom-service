"""
main.py — Atlas Agent Service

FastAPI service that receives ticker pools from Axiom, analyzes each ticker
for technical trade quality using Gemini, and submits qualifying picks to
Alpha Buffer and Prime Buffer.

Port: 9002
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

configure_service_logging("atlas")
logger = logging.getLogger("atlas.main")
ET = pytz.timezone("America/New_York")

_settings: Optional[Settings] = None
_consecutive_brain_failures: int = 0
BRAIN_ALERT_THRESHOLD = 3

# ── SOVEREIGN Directive State ─────────────────────────────────────────────────
_sovereign_halted: bool = False
_score_threshold_override: Optional[float] = None


def _dispatch_sovereign_instruction(instr: dict) -> None:
    """Execute a SOVEREIGN instruction. Supported: HALT, RESUME, STATUS, SET_THRESHOLD, FLUSH."""
    global _sovereign_halted, _score_threshold_override, _consecutive_brain_failures

    raw = instr.get("message", "").strip()
    directive = raw.split(":", 1)[0].strip().upper() if ":" in raw else raw.upper()
    rest = raw.split(":", 1)[1].strip() if ":" in raw else ""

    logger.info("SOVEREIGN directive received — raw: %s", raw[:200])

    if directive == "HALT":
        _sovereign_halted = True
        logger.warning("SOVEREIGN DIRECTIVE: HALT — pool analysis suspended")
        report("atlas", "ack", {"directive": "HALT", "status": "applied", "halted": True})
    elif directive == "RESUME":
        _sovereign_halted = False
        logger.info("SOVEREIGN DIRECTIVE: RESUME — pool analysis resumed")
        report("atlas", "ack", {"directive": "RESUME", "status": "applied", "halted": False})
    elif directive == "STATUS":
        stats = get_stats(_settings.db_path) if _settings else {}
        report("atlas", "status", {"directive": "STATUS", "halted": _sovereign_halted,
                                    "brain_failures": _consecutive_brain_failures,
                                    "threshold_override": _score_threshold_override, **stats})
        logger.info("SOVEREIGN DIRECTIVE: STATUS — reported back")
    elif directive == "SET_THRESHOLD":
        try:
            val = float(rest)
            _score_threshold_override = val
            logger.info("SOVEREIGN DIRECTIVE: SET_THRESHOLD — threshold set to %.1f", val)
            report("atlas", "ack", {"directive": "SET_THRESHOLD", "value": val, "status": "applied"})
        except ValueError:
            report("atlas", "ack", {"directive": "SET_THRESHOLD", "value": rest, "status": "invalid"})
    elif directive == "FLUSH":
        _consecutive_brain_failures = 0
        logger.info("SOVEREIGN DIRECTIVE: FLUSH — brain failure counter reset")
        report("atlas", "ack", {"directive": "FLUSH", "status": "applied"})
    else:
        logger.warning("SOVEREIGN DIRECTIVE: unrecognized '%s'", directive[:100])
        report("atlas", "ack", {"directive": directive[:100], "status": "unrecognized"})



# ── SOVEREIGN Continuous Comms Loop ──────────────────────────────────────────
_comms_stop_atlas = threading.Event()
SOVEREIGN_POLL_INTERVAL_S      = 30   # Poll SOVEREIGN inbox every 30 seconds
SOVEREIGN_HEARTBEAT_INTERVAL_S = 300  # Push heartbeat every 5 minutes


def _sovereign_comms_loop_atlas() -> None:
    """
    Daemon thread: polls SOVEREIGN inbox every 30s and pushes a heartbeat
    every 5 minutes, independent of any pool cycle. Never raises.
    """
    last_heartbeat: float = 0.0
    logger.info("SOVEREIGN comms loop started (poll=%ds heartbeat=%ds)",
                SOVEREIGN_POLL_INTERVAL_S, SOVEREIGN_HEARTBEAT_INTERVAL_S)
    while not _comms_stop_atlas.is_set():
        now = time.monotonic()
        try:
            instructions = get_instructions("atlas")
            if instructions:
                logger.info("SOVEREIGN comms: %d directive(s) received", len(instructions))
                for instr in instructions:
                    _dispatch_sovereign_instruction(instr)
            if now - last_heartbeat >= SOVEREIGN_HEARTBEAT_INTERVAL_S:
                stats = get_stats(_settings.db_path) if _settings else {}
                report("atlas", "AUTONOMOUS", {
                    "event":              "heartbeat",
                    "halted":             _sovereign_halted,
                    "brain_failures":     _consecutive_brain_failures,
                    "ts":                 __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    **stats,
                })
                last_heartbeat = now
        except Exception as exc:  # noqa: BLE001
            logger.warning("SOVEREIGN comms loop error: %s", exc)
        _comms_stop_atlas.wait(SOVEREIGN_POLL_INTERVAL_S)
    logger.info("SOVEREIGN comms loop stopped")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings
    _settings = load_settings()
    init_db(_settings.db_path)
    prime_secret = getattr(_settings, "nexus_prime_secret", None) or ""
    if not prime_secret or len(prime_secret) < 32:
        logger.critical("NEXUS_PRIME_SECRET invalid at startup (len=%d). Prime submissions will 403.", len(prime_secret))
    else:
        logger.info("Atlas NEXUS_PRIME_SECRET validated at startup (len=%d)", len(prime_secret))
    logger.info("Atlas agent started on port %d", _settings.port)
    report("atlas", "status", {"event": "started", "port": _settings.port})
    # Drain queued directives from before this startup
    _startup_instr = get_instructions("atlas")
    if _startup_instr:
        logger.info("Atlas: %d instruction(s) from SOVEREIGN on startup", len(_startup_instr))
        for _i in _startup_instr:
            _dispatch_sovereign_instruction(_i)

    # Launch continuous comms loop (polls every 30s, heartbeat every 5min)
    _comms_stop_atlas.clear()
    threading.Thread(target=_sovereign_comms_loop_atlas, daemon=True, name="atlas-sovereign-comms").start()

    yield

    _comms_stop_atlas.set()
    report("atlas", "status", {"event": "stopped"})
    logger.info("Atlas agent shutting down")


app = FastAPI(title="Atlas Agent", version="1.0.0", lifespan=lifespan)


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
    """Analyze all tickers in the pool. Runs in background thread."""
    global _consecutive_brain_failures

    window_id: str = payload["window_id"]
    pool: list = payload["pool"]
    regime: dict = payload.get("regime", {})
    echo_chamber_risk: list = payload.get("echo_chamber_risk", [])

    # Poll SOVEREIGN for any mid-session instructions (deduped via watermark)
    _instr = get_instructions("atlas")
    if _instr:
        logger.info("Atlas: %d instruction(s) from SOVEREIGN this cycle", len(_instr))
        for _i in _instr:
            _dispatch_sovereign_instruction(_i)

    # Halt gate
    if _sovereign_halted:
        logger.warning("Atlas: SOVEREIGN HALT active — skipping window %s", window_id)
        report("atlas", "status", {"event": "pool_skipped", "reason": "sovereign_halt", "window_id": window_id})
        complete_window(_settings.db_path, window_id, 0, 0)
        return

    analyzed = 0
    submitted = 0

    try:
        from pipeline_client import trace_hop as _trace_hop
    except Exception:
        _trace_hop = None  # type: ignore[assignment]

    try:
        # --- Fetch all Oracle contexts concurrently ---
        valid_tickers = [t for t in pool if isinstance(t, str)]

        def _fetch_ticker(ticker: str):
            return ticker, fetch_context(ticker, _settings.oracle_url, _settings.oracle_headers())

        contexts: dict = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_fetch_ticker, t): t for t in valid_tickers}
            for future in as_completed(futures):
                try:
                    ticker, ctx = future.result()
                    contexts[ticker] = ctx
                except Exception as e:
                    ticker = futures[future]
                    logger.warning("Oracle fetch error for %s: %s", ticker, e)
                    contexts[ticker] = None

        logger.info("Window %s — Oracle fetch complete: %d/%d contexts retrieved",
                    window_id, sum(1 for c in contexts.values() if c), len(valid_tickers))

        for ticker in valid_tickers:
            if _trace_hop:
                _trace_hop(f"{window_id}:{ticker}", "agent_received", "atlas", ticker, "alpha")

            context = contexts.get(ticker)
            if not context:
                logger.warning("Oracle context unavailable for %s — skipping", ticker)
                continue

            result = analyze(ticker, context, regime, echo_chamber_risk, _settings.gemini_api_key)
            analyzed += 1

            if result is None:
                _consecutive_brain_failures += 1
                logger.warning(
                    "Brain failure for %s (consecutive: %d) — skipping submission",
                    ticker, _consecutive_brain_failures,
                )
                if _consecutive_brain_failures >= BRAIN_ALERT_THRESHOLD:
                    alert_brain_down(_settings.telegram_bot_token, _settings.telegram_chat_id, _consecutive_brain_failures)
                    report("atlas", "alert", {"event": "brain_down", "consecutive_failures": _consecutive_brain_failures}, escalation=EscalationLevel.CRITICAL)
                    _consecutive_brain_failures = 0
                continue

            _consecutive_brain_failures = 0

            direction: str = result["direction"]
            score: float = result["score"]
            reasoning: str = result["reasoning"]

            # Submit to buffers (buffer deduplicates per window via UNIQUE constraint)
            alpha_ok = submit_to_alpha(
                ticker, AGENT_NAME, direction, score, reasoning,
                _settings.alpha_buffer_url, _settings.alpha_headers(),
            )
            prime_ok = submit_to_prime(
                ticker, AGENT_NAME, direction, score, reasoning,
                _settings.prime_buffer_url, _settings.prime_headers(),
            )

            if alpha_ok or prime_ok:
                submitted += 1
                record_pick(_settings.db_path, window_id, ticker, direction, score, reasoning, alpha_ok, prime_ok)

            if score >= 58 and not alpha_ok:
                alert_submission_failed(_settings.telegram_bot_token, _settings.telegram_chat_id, ticker, "Alpha")
            if score >= 63 and not prime_ok:
                alert_submission_failed(_settings.telegram_bot_token, _settings.telegram_chat_id, ticker, "Prime")

    except Exception as e:
        logger.error("_analyze_pool crashed for window %s at analyzed=%d: %s", window_id, analyzed, e)
        report("atlas", "incident", {"event": "pool_crash", "window_id": window_id, "error": str(e)[:200]}, escalation=EscalationLevel.CRITICAL)
    finally:
        complete_window(_settings.db_path, window_id, analyzed, submitted)
        logger.info("Window %s complete — analyzed=%d submitted=%d", window_id, analyzed, submitted)
        report("atlas", "status", {"event": "window_complete", "window_id": window_id, "analyzed": analyzed, "submitted": submitted}, escalation=EscalationLevel.INFO)


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
        name=f"atlas-{window_id}",
    )
    thread.start()

    logger.info("Pool received — window=%s tickers=%d regime=%s",
                window_id, payload.count, payload.regime.get("classification", "?"))
    return JSONResponse({"status": "accepted", "window_id": window_id, "agent": AGENT_NAME})


@app.get("/health")
async def health() -> JSONResponse:
    stats = get_stats(_settings.db_path) if _settings else {}
    return JSONResponse({"status": "ok", "agent": AGENT_NAME, "port": _settings.port if _settings else 9002,
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
    global _consecutive_brain_failures, _score_threshold_override, _sovereign_halted

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
        report(AGENT_NAME, "ack", {"directive": d, "status": "applied"})
        return JSONResponse({"ok": True, "directive": d, "brain_failures": 0})
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
        "threshold_override": _score_threshold_override,
        "port": _settings.port if _settings else 9002,
        **stats,
    })


if __name__ == "__main__":
    s = load_settings()
    uvicorn.run("main:app", host="0.0.0.0", port=s.port, log_level="info")
