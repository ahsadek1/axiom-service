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

from config import VALID_AGENTS, MIN_SUBMISSION_SCORE, load_settings
from concordance import evaluate_concordance
from circuit_breaker import (
    CircuitBreakerStatus,
    evaluate_circuit_breaker,
    record_trade_outcome,
    reset_circuit_breaker,
)
from database import (
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

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("alpha_buffer.main")

ET = pytz.timezone("America/New_York")

# ── Load settings — crash fast on missing env vars ────────────────────────────
settings = load_settings()

# ── App state ─────────────────────────────────────────────────────────────────
app_state: dict = {
    "settings":  settings,
    "start_time": datetime.now(ET).isoformat(),
}


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
    """Send Telegram alert when a concordance exhausts all OMNI dispatch retries."""
    try:
        import requests as _req
        msg = (
            f"⚠️ <b>LOST CONCORDANCE — OMNI Dispatch Failed (3 retries)</b>\n"
            f"Ticker: {row['ticker']} {row['direction'].upper()}\n"
            f"Pathway: {row['pathway']} | Window: {row['window_id']}\n"
            f"Verdict: {row['verdict']} | Score: {row['weighted_score']:.1f}\n\n"
            f"Concordance is permanently unexecuted. Manual review required."
        )
        _req.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.ahmed_chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        logger.error("Lost concordance alert failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize DB and circuit breaker state on startup."""
    import asyncio as _asyncio
    logger.info("Alpha Buffer starting...")
    init_db(settings.alpha_db_path)
    init_circuit_breaker(settings.alpha_db_path)
    logger.info("Alpha Buffer ready (solo_entries=%s)", settings.solo_entries_enabled)
    # OMNI H2 fix: start background OMNI retry loop
    retry_task = _asyncio.create_task(_omni_retry_loop())
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
        return v.upper().strip()

    @field_validator("score")
    @classmethod
    def validate_score(cls, v: float) -> float:
        if not (0.0 <= v <= 100.0):
            raise ValueError("score must be between 0 and 100")
        return round(v, 2)


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
    return JSONResponse({
        "status":      "healthy",
        "service":     "alpha-buffer",
        "version":     "3.0.0",
        "cb_status":   cb.get("status", "UNKNOWN"),
        "uptime_since": app_state["start_time"],
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

    # Circuit breaker check
    cb_status = evaluate_circuit_breaker(settings.alpha_db_path)
    if not cb_status.can_trade:
        logger.warning(
            "Submission BLOCKED by circuit breaker (%s): %s/%s from %s",
            cb_status.status, body.ticker, body.direction, body.agent,
        )
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

    window_id = current_window_id()

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
        mark_omni_dispatched(
            db_path       = settings.alpha_db_path,
            window_id     = window_id,
            ticker        = body.ticker,
            direction     = body.direction,
            omni_response = omni_response,
        )
        if not omni_ok:
            logger.error(
                "OMNI dispatch FAILED for %s/%s (pathway=%s) — concordance persisted, "
                "omni_dispatched=False for retry",
                body.ticker, body.direction, concordance.pathway,
            )

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
