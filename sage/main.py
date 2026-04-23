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
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Optional, Union

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.log_setup import configure_service_logging
from shared.sovereign_comms import get_instructions, report

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

configure_service_logging("sage")
logger = logging.getLogger("sage.main")
ET = pytz.timezone("America/New_York")

_settings: Optional[Settings] = None
_consecutive_brain_failures: int = 0
BRAIN_ALERT_THRESHOLD = 3
MAX_PICKS_PER_WINDOW = 5          # Top N picks per window — prevents firehose behavior
_submitted_today: set = set()     # Daily dedup: ticker → submitted this session

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings
    _settings = load_settings()
    init_db(_settings.db_path)
    # Start midnight reset thread
    reset_thread = threading.Thread(target=_midnight_reset, daemon=True, name="sage-midnight-reset")
    reset_thread.start()
    logger.info("Sage agent started on port %d", _settings.port)
    report("sage", "status", {"event": "started", "port": _settings.port})
    _instr = get_instructions("sage")
    if _instr:
        logger.info("Sage: %d instruction(s) from SOVEREIGN on startup", len(_instr))
        for _i in _instr:
            _dispatch_sovereign_instruction(_i)
    yield
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
        # --- Phase 1: Fetch Oracle context concurrently, then score ---
        valid_tickers = [t for t in pool if isinstance(t, str) and t not in _submitted_today]

        def _fetch_ticker(ticker: str):
            """Fetch Oracle context for one ticker. Called concurrently."""
            ctx = fetch_context(ticker, _settings.oracle_url, _settings.oracle_headers())
            return ticker, ctx

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
                _trace_hop(f"{window_id}:{ticker}", "agent_received", "sage", ticker, "alpha")

            context = contexts.get(ticker)
            if context is None:
                logger.warning("Skipping %s — ORACLE context unavailable", ticker)
                analyzed += 1
                continue

            result = analyze(
                ticker, context, regime,
                _settings.deepseek_api_key, _settings.deepseek_base_url,
            )
            analyzed += 1

            if result is None:
                _consecutive_brain_failures += 1
                logger.warning(
                    "Brain failure for %s (consecutive: %d) — skipping submission",
                    ticker, _consecutive_brain_failures,
                )
                if _consecutive_brain_failures >= BRAIN_ALERT_THRESHOLD:
                    alert_brain_down(_settings.telegram_bot_token, _settings.telegram_chat_id, _consecutive_brain_failures)
                    report("sage", "alert", {"event": "brain_down", "consecutive_failures": _consecutive_brain_failures})
                    _consecutive_brain_failures = 0
                continue

            _consecutive_brain_failures = 0
            candidates.append((result["score"], ticker, result["direction"], result["reasoning"]))

        # --- Phase 2: Select top MAX_PICKS_PER_WINDOW by score ---
        candidates.sort(key=lambda x: x[0], reverse=True)
        top_picks = candidates[:MAX_PICKS_PER_WINDOW]

        if len(candidates) > MAX_PICKS_PER_WINDOW:
            logger.info(
                "Window %s: %d candidates scored, capping to top %d (scores: %s)",
                window_id, len(candidates), MAX_PICKS_PER_WINDOW,
                [round(c[0], 1) for c in top_picks],
            )

        # --- Phase 3: Submit top picks ---
        for score, ticker, direction, reasoning in top_picks:
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
                _submitted_today.add(ticker)  # Mark as submitted for today
                record_pick(_settings.db_path, window_id, ticker, direction, score, reasoning, alpha_ok, prime_ok)

            if score >= 58 and not alpha_ok:
                alert_submission_failed(_settings.telegram_bot_token, _settings.telegram_chat_id, ticker, "Alpha")
            if score >= 63 and not prime_ok:
                alert_submission_failed(_settings.telegram_bot_token, _settings.telegram_chat_id, ticker, "Prime")

    except Exception as e:
        logger.error("_analyze_pool crashed for window %s at analyzed=%d: %s", window_id, analyzed, e)
        report("sage", "incident", {"event": "pool_crash", "window_id": window_id, "error": str(e)[:200]})
    finally:
        complete_window(_settings.db_path, window_id, analyzed, submitted)
        logger.info("Window %s complete — analyzed=%d candidates=%d submitted=%d",
                    window_id, analyzed, len(candidates), submitted)
        report("sage", "status", {"event": "window_complete", "window_id": window_id, "analyzed": analyzed, "submitted": submitted})


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


if __name__ == "__main__":
    s = load_settings()
    uvicorn.run("main:app", host="0.0.0.0", port=s.port, log_level="info")
