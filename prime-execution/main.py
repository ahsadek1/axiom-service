"""
main.py — Prime Execution Engine FastAPI Entry Point

Receives GO signals from OMNI. Places swing equity orders via Alpaca.
Monitors exits and reconciles positions every 30 minutes.

Auth: X-Nexus-Prime-Secret header → NEXUS_PRIME_SECRET.
Port: 8006 (local), $PORT (Railway).
"""

import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from alpaca_client import AlpacaClient
from buffer_reporter import BufferReporter
from config import MAX_CONCURRENT_POSITIONS, MAX_NEW_PER_DAY, load_settings
from database import (
    cancel_pending_position,
    confirm_pending_position,
    count_new_positions_today,
    count_open_positions,
    flag_technical_stop,
    get_open_positions,
    get_position_by_id,
    init_db,
    reserve_position_slot,
    # save_position: replaced by reserve+confirm PENDING pattern (Cipher F1+F2 / OMNI H4)
)
from exit_monitor import evaluate_exits
from reconciler import run_reconciler

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("prime_exec.main")

ET       = pytz.timezone("America/New_York")
settings = load_settings()

app_state: dict = {
    "settings":                    settings,
    "start_time":                  datetime.now(ET).isoformat(),
    "trades_today":                0,
    "execution_paused":            False,
    "last_reconcile_at":           None,
    "last_reconcile_mismatches":   0,
    "was_paused_for_reconcile":    False,
}
# Lock for position-limit checks and DB writes during /execute
_execute_lock = threading.Lock()
# Separate lock for all app_state read-modify-write operations (adversarial fix #4)
_state_lock = threading.Lock()


def verify_secret(x_nexus_prime_secret: str) -> None:
    """Verify X-Nexus-Prime-Secret header using constant-time comparison (timing-safe)."""
    if not x_nexus_prime_secret or not secrets.compare_digest(
        x_nexus_prime_secret, settings.nexus_prime_secret
    ):
        raise HTTPException(status_code=403, detail="Forbidden")


def _get_alpaca() -> AlpacaClient:
    return AlpacaClient(settings.alpaca_api_key, settings.alpaca_secret_key)


def _get_reporter() -> BufferReporter:
    return BufferReporter(settings.prime_buffer_url, settings.nexus_prime_secret)


def _is_market_hours() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return now.replace(hour=9, minute=30, second=0, microsecond=0) <= now <= \
           now.replace(hour=16, minute=0, second=0, microsecond=0)


@asynccontextmanager
async def lifespan(app: "FastAPI") -> AsyncGenerator[None, None]:
    """Initialize DB and start exit monitor + reconciler scheduler."""
    logger.info("Prime Execution starting...")
    init_db(settings.prime_db_path)

    # OMNI H-NEW-1 fix: start AILS outcome retry worker
    from ails_reporter import start_retry_worker as _start_ails_retry
    _start_ails_retry()

    scheduler = BackgroundScheduler(timezone=ET)

    # Exit monitor every 5 min during market hours (approved Ahmed Sadek 2026-04-12)
    # o3-mini Pass A flagged 15-min as CRITICAL for equity swing positions.
    # 5-minute provides tight enough protection without options-level granularity.
    scheduler.add_job(
        func    = lambda: _run_exit_monitor(),
        trigger = CronTrigger(hour="9-15", minute="0,5,10,15,20,25,30,35,40,45,50,55", timezone=ET),
        id      = "prime_exit_monitor",
        replace_existing=True,
    )

    # Reconciler every 30 min during market hours
    scheduler.add_job(
        func    = lambda: _run_reconciler(),
        trigger = CronTrigger(hour="9-15", minute="0,30", timezone=ET),
        id      = "prime_reconciler",
        replace_existing=True,
    )

    scheduler.start()
    app_state["scheduler"] = scheduler
    logger.info("Prime Execution ready")
    yield
    scheduler.shutdown(wait=False)
    logger.info("Prime Execution stopped")


def _run_exit_monitor() -> None:
    if not _is_market_hours():
        return
    try:
        events = evaluate_exits(
            db_path         = settings.prime_db_path,
            alpaca_client   = _get_alpaca(),
            buffer_reporter = _get_reporter(),
            bot_token       = settings.telegram_bot_token,
            chat_id         = settings.ahmed_chat_id,
        )
        if events:
            logger.info("Prime exit monitor: %d events", len(events))
    except Exception as e:
        logger.error("Prime exit monitor failed: %s", e)


def _run_reconciler() -> None:
    if not _is_market_hours():
        return
    try:
        # Reconciler writes directly to app_state dict.
        # Adversarial fix #4: wrap the reconciler call with _state_lock so that
        # any compound read-modify-write of app_state is fully atomic.
        with _state_lock:
            run_reconciler(
                db_path       = settings.prime_db_path,
                alpaca_client = _get_alpaca(),
                bot_token     = settings.telegram_bot_token,
                chat_id       = settings.ahmed_chat_id,
                app_state     = app_state,
            )
    except Exception as e:
        logger.error("Prime reconciler failed: %s", e)


app = FastAPI(
    title    = "Prime Execution Engine",
    version  = "3.0.0",
    lifespan = lifespan,
)


class ExecuteRequest(BaseModel):
    """OMNI execution signal payload for Prime swing equity."""

    ticker:            str
    direction:         str
    pathway:           str
    weighted_score:    float
    agent_scores:      dict
    verdict:           str
    sizing_mult:       float
    position_size_usd: float
    window_id:         str
    echo_chamber:      bool  = False
    axiom_risk_score:  Optional[float] = None

    @field_validator("ticker")
    @classmethod
    def norm_ticker(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("direction")
    @classmethod
    def norm_direction(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("bullish", "bearish"):
            raise ValueError("direction must be 'bullish' or 'bearish'")
        return v

    @field_validator("position_size_usd")
    @classmethod
    def validate_position_size(cls, v: float) -> float:
        """Adversarial fix #9: reject negative, zero, or absurd position sizes."""
        if v <= 0:
            raise ValueError("position_size_usd must be positive")
        if v > 500_000:
            raise ValueError("position_size_usd exceeds $500K safety cap")
        return v

    @field_validator("sizing_mult")
    @classmethod
    def validate_sizing_mult(cls, v: float) -> float:
        """Adversarial fix #9: reject non-positive or dangerously large multipliers."""
        if v <= 0:
            raise ValueError("sizing_mult must be positive")
        if v > 10:
            raise ValueError("sizing_mult exceeds 10x safety cap")
        return v

    @field_validator("weighted_score")
    @classmethod
    def validate_weighted_score(cls, v: float) -> float:
        """Adversarial fix #9: score must be in valid range [0, 100]."""
        if not (0 <= v <= 100):
            raise ValueError("weighted_score must be between 0 and 100")
        return v


class TechnicalStopRequest(BaseModel):
    """Flag a position as technically invalidated."""
    position_id: int
    reason:      str


@app.get("/health")
def health() -> JSONResponse:
    """Health check. Always 200."""
    return JSONResponse({
        "status":              "healthy",
        "service":             "prime-execution",
        "version":             "3.0.0",
        "trades_today":        app_state["trades_today"],
        "open_positions":      count_open_positions(settings.prime_db_path),
        "execution_paused":    app_state["execution_paused"],
        "last_reconcile_at":   app_state["last_reconcile_at"],
        "uptime_since":        app_state["start_time"],
    })


@app.post("/execute")
def execute(
    body: ExecuteRequest,
    x_nexus_prime_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Receive a GO signal from OMNI and open a Prime swing equity position.

    Full entry pipeline:
      1. Verify auth
      2. Check reconciler pause flag
      3. Check position limits
      4. Get current price
      5. Calculate share count from dollar size
      6. Place buy/sell order via Alpaca
      7. Persist position to DB
      8. Notify Ahmed

    Returns:
        JSON with execution result and position details.
    """
    verify_secret(x_nexus_prime_secret)

    # Cipher Finding 7: verdict whitelist — execution bridge last-line gate.
    if body.verdict not in ("GO", "STRONG_GO"):
        logger.warning(
            "Prime execution rejected: verdict '%s' not in executable whitelist", body.verdict
        )
        return JSONResponse(
            status_code=422,
            content={
                "executed": False,
                "reason":   f"Verdict '{body.verdict}' is not executable — whitelist: GO, STRONG_GO",
            },
        )

    # ── Reconciler pause check (read-only — no lock needed) ───────────────────
    if app_state.get("execution_paused"):
        return JSONResponse(
            status_code = 503,
            content     = {
                "executed": False,
                "reason":   "Execution paused — position reconciliation mismatch detected. "
                            "Resolve positions then wait for next reconciler cycle.",
            },
        )

    alpaca = _get_alpaca()

    # ── Get Current Price (network call — outside lock) ───────────────────────
    current_price = alpaca.get_latest_price(body.ticker)
    if not current_price or current_price <= 0:
        return JSONResponse(
            status_code=503,
            content={"executed": False, "reason": f"Cannot fetch valid price for {body.ticker}"},
        )

    # ── Calculate Share Count ─────────────────────────────────────────────────
    # Adversarial fix #8: Alpaca paper supports fractional shares (2 decimal places).
    # Round to 2 dp; enforce minimum of 1 share to prevent micro-lot orders.
    shares = round(max(1.0, body.position_size_usd / current_price), 2)

    # ── Atomic limit check + PENDING slot reservation (Cipher Finding 1+2 fix) ─
    import json as _json
    pending_id = None
    with _execute_lock:
        open_count  = count_open_positions(settings.prime_db_path)  # includes 'pending'
        today_count = count_new_positions_today(settings.prime_db_path)

        if open_count >= MAX_CONCURRENT_POSITIONS:
            return JSONResponse(
                status_code=429,
                content={"executed": False,
                         "reason": f"Max concurrent positions reached ({open_count}/{MAX_CONCURRENT_POSITIONS})"},
            )
        if today_count >= MAX_NEW_PER_DAY:
            return JSONResponse(
                status_code=429,
                content={"executed": False,
                         "reason": f"Max new positions today reached ({today_count}/{MAX_NEW_PER_DAY})"},
            )
        # Reserve slot inside lock — visible to next concurrent request immediately.
        pending_id = reserve_position_slot(
            db_path           = settings.prime_db_path,
            ticker            = body.ticker,
            direction         = body.direction,
            pathway           = body.pathway,
            shares            = shares,
            position_size_usd = body.position_size_usd,
            window_id         = body.window_id or "",
            agent_scores      = _json.dumps(body.agent_scores or {}),
            verdict           = body.verdict,
        )

    # ── Place Equity Order OUTSIDE lock (network call) ────────────────────────
    side            = "buy" if body.direction == "bullish" else "sell"
    alpaca_order_id = None
    order_error     = None

    try:
        order           = alpaca.place_order(body.ticker, shares, side)
        alpaca_order_id = order.get("id")
        logger.info(
            "Prime order placed: %s %s %.0f shares @ ~$%.2f | order_id=%s",
            side.upper(), body.ticker, shares, current_price, alpaca_order_id,
        )
    except Exception as e:
        order_error = str(e)[:200]
        logger.error("Alpaca order FAILED for %s: %s — cancelling pending slot", body.ticker, e)
        if pending_id:
            cancel_pending_position(settings.prime_db_path, pending_id)
        return JSONResponse(
            status_code=503,
            content={"executed": False,
                     "reason": f"Alpaca order placement failed: {order_error}",
                     "position_id": None},
        )

    # ── Alpaca confirmed — promote pending slot to open position ─────────────
    confirm_pending_position(
        db_path          = settings.prime_db_path,
        position_id      = pending_id,
        alpaca_order_id  = alpaca_order_id or "",
        entry_price      = current_price,
        shares_remaining = shares,
    )
    position_id = pending_id
    with _state_lock:
        app_state["trades_today"] += 1

    _notify_entry(settings.telegram_bot_token, settings.ahmed_chat_id,
                  body, position_id, shares, current_price, alpaca_order_id)

    return JSONResponse({
        "executed":      True,
        "position_id":   position_id,
        "ticker":        body.ticker,
        "direction":     body.direction,
        "side":          side,
        "shares":        round(shares, 2),
        "entry_price":   current_price,
        "size_usd":      body.position_size_usd,
        "order_id":      alpaca_order_id,
        "order_error":   order_error,
    })


@app.post("/technical-stop")
def technical_stop(
    body: TechnicalStopRequest,
    x_nexus_prime_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Flag a Prime position as technically invalidated.

    The position will be closed on the next exit monitor cycle (within 15 min).
    Technical invalidation is the PRIMARY stop mechanism for Prime swing trades.

    Args:
        body: TechnicalStopRequest with position_id and reason.
    """
    verify_secret(x_nexus_prime_secret)

    pos = get_position_by_id(settings.prime_db_path, body.position_id)
    if pos is None:
        raise HTTPException(status_code=404, detail=f"Position {body.position_id} not found")
    if pos["status"] != "open":
        raise HTTPException(status_code=409, detail=f"Position {body.position_id} is already closed")

    flag_technical_stop(settings.prime_db_path, body.position_id)
    logger.info("Technical stop flagged: position %d (%s) — %s", body.position_id, pos["ticker"], body.reason)

    return JSONResponse({
        "flagged":      True,
        "position_id":  body.position_id,
        "ticker":       pos["ticker"],
        "reason":       body.reason,
        "message":      "Position will be closed on next exit monitor cycle (within 15 min).",
    })


@app.get("/positions")
def get_positions(x_nexus_prime_secret: str = Header(default="")) -> JSONResponse:
    """Return all open Prime positions."""
    verify_secret(x_nexus_prime_secret)
    positions = get_open_positions(settings.prime_db_path)
    return JSONResponse({
        "count":               len(positions),
        "execution_paused":    app_state["execution_paused"],
        "positions":           positions,
    })


@app.post("/resume")
def resume_execution(x_nexus_prime_secret: str = Header(default="")) -> JSONResponse:
    """Clear execution_paused flag.

    Cipher P2-5 fix: Guardian Angel calls /resume to auto-clear paused state
    after detecting reconciler-clean conditions. Previously this endpoint did
    not exist — every Guardian Angel auto-resume call returned 404 silently.
    """
    verify_secret(x_nexus_prime_secret)
    with _state_lock:
        was_paused = app_state.get("execution_paused", False)
        app_state["execution_paused"] = False
    logger.info("Execution resumed via /resume (was_paused=%s)", was_paused)
    return JSONResponse({"resumed": True, "was_paused": was_paused})


@app.post("/reconcile")
def manual_reconcile(x_nexus_prime_secret: str = Header(default="")) -> JSONResponse:
    """Manually trigger a reconciliation pass.

    OMNI M-NEW-2 fix: wrap with _state_lock. The scheduled reconciler also holds
    _state_lock — without this, a manual trigger concurrent with a scheduled run
    creates a race on app_state["execution_paused"].
    """
    verify_secret(x_nexus_prime_secret)
    with _state_lock:
        result = run_reconciler(
            settings.prime_db_path, _get_alpaca(),
            settings.telegram_bot_token, settings.ahmed_chat_id,
            app_state,
        )
    return JSONResponse(result)


def _notify_entry(bot_token, chat_id, body, position_id, shares, price, order_id):
    import requests
    emoji = "📈" if body.direction == "bullish" else "📉"
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "parse_mode": "HTML",
                "text": (
                    f"{emoji} <b>PRIME TRADE OPENED — {body.ticker}</b>\n"
                    f"Direction: {body.direction.upper()} | {shares:.0f} shares @ ${price:.2f}\n"
                    f"Size: ${body.position_size_usd:,.0f} | Pathway: {body.pathway}\n"
                    f"Score: {body.weighted_score:.1f} | Position #{position_id}"
                ),
            },
            timeout=8,
        )
    except Exception:
        pass
