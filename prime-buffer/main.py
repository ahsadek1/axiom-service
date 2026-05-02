"""
main.py — Prime Concordance Buffer FastAPI Entry Point

Receives R2 agent picks (swing equity/Prime) and evaluates concordance.
Auth: X-Nexus-Prime-Secret header → NEXUS_PRIME_SECRET env var.
Port: 8003 (local), $PORT (Railway).

Key differences from Alpha Buffer:
  - Auth header: X-Nexus-Prime-Secret (not X-Nexus-Secret)
  - Min submission score: 63 (vs 58)
  - P1 GO threshold: 70 (vs 65)
  - System label: 'prime' in all concordance payloads to OMNI
  - DB: PRIME_DB_PATH env var
"""

import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator, Optional

import pytz
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from config import VALID_AGENTS, MIN_SUBMISSION_SCORE, load_settings
from concordance import evaluate_concordance
from circuit_breaker import (
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

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("prime_buffer.main")

ET = pytz.timezone("America/New_York")

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

settings  = load_settings()
app_state = {
    "settings":   settings,
    "start_time": datetime.now(ET).isoformat(),
}


def current_window_id() -> str:
    """
    Generate the current 15-minute window ID: 'YYYY-MM-DD-HHMM'.

    Returns:
        Window ID rounded to the current 15-minute boundary.
    """
    now  = datetime.now(ET)
    mins = (now.minute // 15) * 15
    return now.strftime(f"%Y-%m-%d-%H{mins:02d}")


def verify_secret(x_nexus_prime_secret: str) -> None:
    """
    Verify X-Nexus-Prime-Secret header against configured secret.

    Raises:
        HTTPException: 403 if missing or invalid.
    """
    if not x_nexus_prime_secret or not secrets.compare_digest(
        x_nexus_prime_secret, settings.nexus_prime_secret
    ):
        raise HTTPException(status_code=403, detail="Forbidden")


async def _omni_retry_loop() -> None:
    """
    Background retry loop for failed Prime OMNI dispatches (Cipher Pass 3 P3-5).

    Mirrors Alpha Buffer's _omni_retry_loop exactly.
    Polls every 60s for Prime concordances with omni_dispatched=0.
    Retries up to 3 times. Alerts Ahmed via Telegram after exhausting retries.
    A missed Prime concordance = missed swing trade signal.
    """
    import asyncio as _asyncio
    import json as _json
    MAX_ATTEMPTS = 3
    POLL_INTERVAL = 60

    while True:
        await _asyncio.sleep(POLL_INTERVAL)
        try:
            loop = _asyncio.get_event_loop()
            pending = await loop.run_in_executor(
                None, get_undispatched_concordances,
                settings.prime_db_path, 30, MAX_ATTEMPTS,
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
                            settings.prime_db_path,
                            row["window_id"], row["ticker"], row["direction"],
                            omni_response=response,
                        )
                        logger.info(
                            "Prime OMNI retry succeeded: %s/%s window=%s attempt=%d",
                            row["ticker"], row["direction"], row["window_id"],
                            row["dispatch_attempts"] + 1,
                        )
                    else:
                        increment_dispatch_attempts(
                            settings.prime_db_path,
                            row["window_id"], row["ticker"], row["direction"],
                        )
                        new_attempts = row["dispatch_attempts"] + 1
                        logger.warning(
                            "Prime OMNI retry failed: %s/%s attempt=%d",
                            row["ticker"], row["direction"], new_attempts,
                        )
                        if new_attempts >= MAX_ATTEMPTS:
                            _alert_lost_concordance(row)
                except Exception as e:
                    logger.error("Prime OMNI retry error for %s: %s", row.get("ticker"), e)
        except Exception as e:
            logger.error("Prime OMNI retry poll error: %s", e)


def _alert_lost_concordance(row: dict) -> None:
    """Alert Ahmed when a Prime concordance exhausts all OMNI dispatch retries."""
    try:
        import requests as _req
        msg = (
            f"⚠️ <b>PRIME LOST CONCORDANCE — OMNI Dispatch Failed (3 retries)</b>\n"
            f"Ticker: {row['ticker']} {row['direction'].upper()}\n"
            f"Pathway: {row['pathway']} | Window: {row['window_id']}\n"
            f"Verdict: {row['verdict']} | Score: {row['weighted_score']:.1f}\n\n"
            f"Prime concordance permanently unexecuted. Manual review required."
        )
        _req.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.ahmed_chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        logger.error("Prime lost concordance alert failed: %s", e)


@asynccontextmanager
async def lifespan(app: "FastAPI") -> AsyncGenerator[None, None]:
    """Initialize DB and circuit breaker state on startup."""
    import asyncio as _asyncio
    logger.info("Prime Buffer starting...")
    init_db(settings.prime_db_path)
    init_circuit_breaker(settings.prime_db_path)
    logger.info("Prime Buffer ready (solo_entries=%s)", settings.solo_entries_enabled)
    retry_task = _asyncio.create_task(_omni_retry_loop())
    yield
    retry_task.cancel()
    logger.info("Prime Buffer stopped")


app = FastAPI(
    title       = "Prime Concordance Buffer",
    description = "Nexus Trading System — Swing equity concordance engine",
    version     = "3.0.0",
    lifespan    = lifespan,
)


class SubmitRequest(BaseModel):
    """Agent R2 pick submission payload for Prime (swing equity)."""

    agent:     str
    ticker:    str
    direction: str
    score:     float
    reasoning: Optional[str] = None

    @field_validator("agent")
    @classmethod
    def validate_agent(cls, v: str) -> str:
        """Normalize and reject unknown agents — canonical casing enforced."""
        v_clean  = v.strip()
        canonical = {a.lower(): a for a in VALID_AGENTS}
        resolved  = canonical.get(v_clean.lower())
        if resolved is None:
            raise ValueError(f"Unknown agent '{v}'. Valid: {sorted(VALID_AGENTS)}")
        return resolved

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, v: str) -> str:
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
    pnl_pct:  float
    pathway:  Optional[str] = None


@app.get("/health")
def health() -> JSONResponse:
    """Health check. Always returns 200. No auth required."""
    cb = get_circuit_breaker_state(settings.prime_db_path)
    return JSONResponse({
        "status":       "healthy",
        "service":      "prime-buffer",
        "version":      "3.0.0",
        "cb_status":    cb.get("status", "UNKNOWN"),
        "uptime_since": app_state["start_time"],
        "code_hash":    _CODE_HASH,
        "stale_deploy": (not _os.path.exists("/tmp/nexus_deploy_in_progress")) and _CODE_HASH != _compute_module_hash(),
    })


@app.post("/submit")
def submit_pick(
    body: SubmitRequest,
    x_nexus_prime_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Receive an R2 agent pick for Prime concordance evaluation (swing equity).

    Validates agent, score, and circuit breaker before accepting.
    On concordance: persists result and dispatches to OMNI with system='prime'.

    Args:
        body: SubmitRequest payload.

    Returns:
        JSON with submission status and concordance result (if triggered).
    """
    verify_secret(x_nexus_prime_secret)

    # Circuit breaker check
    cb_status = evaluate_circuit_breaker(settings.prime_db_path)
    if not cb_status.can_trade:
        logger.warning(
            "Submission BLOCKED by circuit breaker (%s): %s/%s from %s (Prime)",
            cb_status.status, body.ticker, body.direction, body.agent,
        )
        return JSONResponse(
            status_code = 503,
            content     = {
                "accepted":        False,
                "reason":          f"Circuit breaker {cb_status.status} — {cb_status.notes}",
                "circuit_breaker": cb_status.to_dict(),
            },
        )

    # Score gate — Prime minimum is 63
    if body.score < MIN_SUBMISSION_SCORE:
        logger.info(
            "Submission REJECTED (score %.1f < %.1f): %s/%s from %s (Prime)",
            body.score, MIN_SUBMISSION_SCORE, body.ticker, body.direction, body.agent,
        )
        return JSONResponse(
            status_code = 422,
            content     = {
                "accepted": False,
                "reason":   f"Score {body.score} below minimum {MIN_SUBMISSION_SCORE} for Prime",
            },
        )

    window_id = current_window_id()

    save_submission(
        db_path   = settings.prime_db_path,
        window_id = window_id,
        agent     = body.agent,
        ticker    = body.ticker,
        direction = body.direction,
        score     = body.score,
        reasoning = body.reasoning,
    )

    logger.info(
        "Submission ACCEPTED (Prime): %s | %s/%s | score=%.1f | window=%s",
        body.agent, body.ticker, body.direction, body.score, window_id,
    )

    all_subs = get_window_submissions(
        settings.prime_db_path,
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
            "accepted":          True,
            "window_id":         window_id,
            "agent":             body.agent,
            "ticker":            body.ticker,
            "direction":         body.direction,
            "score":             body.score,
            "agents_in_window":  len(all_subs),
            "concordance":       None,
            "message":           f"Submission accepted (Prime). {len(all_subs)}/3 agents in window.",
        })

    save_concordance_result(
        db_path         = settings.prime_db_path,
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
        "CONCORDANCE FORMED (Prime): %s %s/%s | pathway=%s | score=%.1f | verdict=%s",
        window_id, body.ticker, body.direction,
        concordance.pathway, concordance.weighted_score, concordance.verdict,
    )

    concordance_dict   = concordance.to_dict()
    omni_ok, omni_resp = dispatch_to_omni(
        omni_url         = settings.omni_webhook_url,
        auth_headers     = settings.omni_auth_header,
        concordance_dict = concordance_dict,
    )

    # Cipher Pass 3 P3-5: only mark omni_dispatched=1 on confirmed success.
    # Prior code unconditionally marked dispatched=1 even on OMNI timeout/error —
    # the concordance was silently lost with no retry path.
    # Now: success → mark dispatched; failure → leave omni_dispatched=0 for retry loop.
    if omni_ok:
        mark_omni_dispatched(
            db_path       = settings.prime_db_path,
            window_id     = window_id,
            ticker        = body.ticker,
            direction     = body.direction,
            omni_response = omni_resp,
        )
    else:
        logger.error(
            "OMNI dispatch FAILED for Prime %s/%s (pathway=%s) — queued for retry",
            body.ticker, body.direction, concordance.pathway,
        )

    return JSONResponse({
        "accepted":         True,
        "window_id":        window_id,
        "agent":            body.agent,
        "ticker":           body.ticker,
        "direction":        body.direction,
        "score":            body.score,
        "agents_in_window": len(all_subs),
        "concordance":      concordance_dict,
        "omni_dispatched":  omni_ok,
        "message":          f"Prime concordance {concordance.pathway} formed — dispatched to OMNI.",
    })


@app.get("/status")
def buffer_status(
    x_nexus_prime_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Return current Prime buffer state: circuit breaker, current window, submissions.

    Returns:
        Status payload with circuit breaker, window_id, and per-ticker counts.
    """
    verify_secret(x_nexus_prime_secret)

    window_id     = current_window_id()
    cb_status     = evaluate_circuit_breaker(settings.prime_db_path)
    cb_raw        = get_circuit_breaker_state(settings.prime_db_path)
    ticker_pairs  = get_all_tickers_in_window(settings.prime_db_path, window_id)

    ticker_status = {}
    for ticker, direction in ticker_pairs:
        subs        = get_window_submissions(settings.prime_db_path, window_id, ticker, direction)
        key         = f"{ticker}/{direction}"
        concordance = get_concordance_result(settings.prime_db_path, window_id, ticker, direction)
        ticker_status[key] = {
            "agents_submitted": [s["agent"] for s in subs],
            "count":            len(subs),
            "scores":           {s["agent"]: s["score"] for s in subs},
            "concordance":      concordance["pathway"] if concordance else None,
            "omni_dispatched":  concordance["omni_dispatched"] if concordance else False,
        }

    return JSONResponse({
        "service":              "prime-buffer",
        "window_id":            window_id,
        "circuit_breaker":      cb_status.to_dict(),
        "raw_cb_state": {
            "consecutive_losses": cb_raw.get("consecutive_losses", 0),
            "daily_trades":       cb_raw.get("daily_trades", 0),
            "daily_wins":         cb_raw.get("daily_wins", 0),
            "daily_losses":       cb_raw.get("daily_losses", 0),
            "daily_pnl_pct":      cb_raw.get("daily_pnl_pct", 0.0),
        },
        "solo_entries_enabled": settings.solo_entries_enabled,
        "current_window":       ticker_status,
        "checked_at":           datetime.now(ET).isoformat(),
    })


@app.post("/trade-outcome")
def trade_outcome(
    body: TradeOutcomeRequest,
    x_nexus_prime_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Record a closed Prime trade result and update circuit breaker state.

    Called by the Prime execution engine when a swing trade closes.

    Args:
        body: TradeOutcomeRequest with ticker, won flag, and pnl_pct.
    """
    verify_secret(x_nexus_prime_secret)

    record_trade_outcome(
        db_path = settings.prime_db_path,
        won     = body.won,
        pnl_pct = body.pnl_pct,
    )

    logger.info(
        "Trade outcome recorded (Prime): %s %s | pnl=%.1f%%",
        "WIN" if body.won else "LOSS",
        body.ticker,
        body.pnl_pct * 100,
    )

    return JSONResponse({
        "recorded":  True,
        "ticker":    body.ticker,
        "result":    "win" if body.won else "loss",
        "pnl_pct":   body.pnl_pct,
    })


@app.post("/circuit-breaker/reset")
def circuit_breaker_reset(
    x_nexus_prime_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Ahmed manual circuit breaker reset for Prime system.

    Clears all counters and returns Prime to NORMAL state.
    """
    verify_secret(x_nexus_prime_secret)
    reset_circuit_breaker(settings.prime_db_path, note="Manual reset via Prime API")
    logger.info("Prime circuit breaker MANUALLY RESET")

    return JSONResponse({
        "reset":   True,
        "status":  "NORMAL",
        "message": "Prime circuit breaker reset to NORMAL. All counters cleared.",
    })
