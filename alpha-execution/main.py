"""
main.py — Alpha Execution Engine FastAPI Entry Point

Receives GO signals from OMNI. Resolves options contracts.
Places trades via Alpaca paper trading. Monitors exits.

Auth: X-Nexus-Secret header → NEXUS_WEBHOOK_SECRET.
Port: 8005 (local), $PORT (Railway).
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
from config import (
    MAX_CONCURRENT_POSITIONS,
    MAX_NEW_PER_DAY,
    load_settings,
)
from contract_resolver import resolve_spread
from database import (
    cancel_pending_position,
    confirm_pending_position,
    count_new_positions_today,
    count_open_positions,
    get_open_positions,
    init_db,
    reserve_position_slot,
    # _DEPRECATED_save_position_DO_NOT_USE — intentionally not imported (OMNI H4 / Cipher F1+F2)
)
from exit_monitor import evaluate_exits

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("alpha_exec.main")

ET       = pytz.timezone("America/New_York")
settings = load_settings()

app_state: dict = {
    "settings":      settings,
    "start_time":    datetime.now(ET).isoformat(),
    "trades_today":  0,
    "total_trades":  0,
}
# Thread-safe lock for position-limit checks + app_state mutations
_execute_lock = threading.Lock()
_state_lock   = threading.Lock()  # OMNI M-NEW-3: guards app_state counter mutations


def verify_secret(x_nexus_secret: str) -> None:
    """Verify X-Nexus-Secret header. Raises 403 if invalid.
    Uses constant-time comparison to prevent timing attacks.
    """
    if not x_nexus_secret or not secrets.compare_digest(
        x_nexus_secret, settings.nexus_webhook_secret
    ):
        raise HTTPException(status_code=403, detail="Forbidden")


def _get_alpaca() -> AlpacaClient:
    return AlpacaClient(settings.alpaca_api_key, settings.alpaca_secret_key)


def _get_reporter() -> BufferReporter:
    return BufferReporter(settings.alpha_buffer_url, settings.nexus_webhook_secret)


def _is_market_hours() -> bool:
    """Return True if within market hours (9:30 AM – 4:00 PM ET, weekdays)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now <= close_t


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize DB and start exit monitor scheduler."""
    logger.info("Alpha Execution starting...")
    init_db(settings.alpha_db_path)

    # OMNI H-NEW-1 fix: start AILS outcome retry worker
    from ails_reporter import start_retry_worker as _start_ails_retry
    _start_ails_retry()

    scheduler = BackgroundScheduler(timezone=ET)
    # Exit monitor every 1 min during market hours (approved Ahmed Sadek 2026-04-12)
    # o3-mini Pass A flagged 15-min as CRITICAL for options — fast moves can exceed
    # +35% / +100% targets between checks. 1-minute eliminates that gap.
    scheduler.add_job(
        func    = lambda: _run_exit_monitor(),
        trigger = CronTrigger(hour="9-15", minute="*", timezone=ET),
        id      = "exit_monitor",
        replace_existing=True,
    )
    scheduler.start()
    app_state["scheduler"] = scheduler
    logger.info("Alpha Execution ready")
    yield
    scheduler.shutdown(wait=False)
    logger.info("Alpha Execution stopped")


def _run_exit_monitor() -> None:
    """Scheduled exit monitor tick."""
    if not _is_market_hours():
        return
    try:
        events = evaluate_exits(
            db_path         = settings.alpha_db_path,
            alpaca_client   = _get_alpaca(),
            buffer_reporter = _get_reporter(),
            bot_token       = settings.telegram_bot_token,
            chat_id         = settings.ahmed_chat_id,
        )
        if events:
            logger.info("Exit monitor: %d events", len(events))
    except Exception as e:
        logger.error("Exit monitor failed: %s", e)


app = FastAPI(
    title    = "Alpha Execution Engine",
    version  = "3.0.0",
    lifespan = lifespan,
)


# ── Request Models ────────────────────────────────────────────────────────────

class ExecuteRequest(BaseModel):
    """OMNI execution signal payload."""

    ticker:            str
    direction:         str
    pathway:           str
    weighted_score:    float
    agent_scores:      dict
    verdict:           str
    sizing_mult:       float
    position_size_usd: float
    window_id:         str
    echo_chamber:      bool = False
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


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> JSONResponse:
    """Health check. Always 200."""
    return JSONResponse({
        "status":      "healthy",
        "service":     "alpha-execution",
        "version":     "3.0.0",
        "trades_today": app_state["trades_today"],
        "open_positions": count_open_positions(settings.alpha_db_path),
        "uptime_since": app_state["start_time"],
    })


@app.post("/execute")
def execute(
    body: ExecuteRequest,
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Receive a GO signal from OMNI and place an options trade.

    Full entry pipeline:
      1. Verify auth
      2. Check position limits (concurrent + daily)
      3. Get current underlying price from Alpaca
      4. Resolve spread parameters (strike, expiry)
      5. Place spread order via Alpaca
      6. Persist position to DB
      7. Confirm to Ahmed via Telegram

    Returns:
        JSON with execution result, position_id, and spread details.
    """
    verify_secret(x_nexus_secret)

    # Cipher Finding 7+8: verdict whitelist — last line of defense.
    # OMNI's can_execute() is the first gate. This is the execution bridge gate.
    # Prevents CONDITIONAL or any non-executable verdict from reaching Alpaca
    # via direct API call, future OMNI bug, or misconfiguration.
    if body.verdict not in ("GO", "STRONG_GO"):
        logger.warning(
            "Alpha execution rejected: verdict '%s' not in executable whitelist", body.verdict
        )
        return JSONResponse(
            status_code=422,
            content={
                "executed": False,
                "reason":   f"Verdict '{body.verdict}' is not executable — whitelist: GO, STRONG_GO",
            },
        )

    alpaca = _get_alpaca()

    # ── Get Current Price (outside lock — network call, no state mutation) ────
    current_price = alpaca.get_latest_price(body.ticker)
    if not current_price:
        logger.warning("Cannot get price for %s — execution rejected", body.ticker)
        return JSONResponse(
            status_code = 503,
            content     = {"executed": False, "reason": f"Cannot fetch price for {body.ticker}"},
        )

    # ── Resolve Spread Parameters ─────────────────────────────────────────────
    try:
        spread = resolve_spread(
            ticker        = body.ticker,
            direction     = body.direction,
            current_price = current_price,
        )
    except ValueError as e:
        return JSONResponse(
            status_code = 422,
            content     = {"executed": False, "reason": str(e)},
        )

    contracts = max(1, int(body.position_size_usd / (current_price * 100)))

    # ── Atomic limit check + PENDING slot reservation (Cipher Finding 1+2 fix) ─
    # Lock guards check + reservation as a single atomic unit.
    # Writing a 'pending' DB record INSIDE the lock closes the TOCTOU race:
    # two concurrent requests both see open_count updated after the first
    # reservation, so only one can pass the limit check.
    pending_id = None
    with _execute_lock:
        open_count  = count_open_positions(settings.alpha_db_path)  # includes 'pending'
        today_count = count_new_positions_today(settings.alpha_db_path)

        if open_count >= MAX_CONCURRENT_POSITIONS:
            logger.info("Alpha execution blocked: max concurrent positions (%d)", MAX_CONCURRENT_POSITIONS)
            return JSONResponse(
                status_code = 429,
                content     = {
                    "executed":    False,
                    "reason":      f"Max concurrent positions reached ({open_count}/{MAX_CONCURRENT_POSITIONS})",
                    "position_id": None,
                },
            )

        if today_count >= MAX_NEW_PER_DAY:
            logger.info("Alpha execution blocked: max new positions today (%d)", MAX_NEW_PER_DAY)
            return JSONResponse(
                status_code = 429,
                content     = {
                    "executed":    False,
                    "reason":      f"Max new positions today reached ({today_count}/{MAX_NEW_PER_DAY})",
                    "position_id": None,
                },
            )

        # Reserve slot inside the lock — guarantees the next concurrent request
        # sees this slot in count_open_positions() before we release the lock.
        import json as _json
        pending_id = reserve_position_slot(
            db_path           = settings.alpha_db_path,
            ticker            = body.ticker,
            direction         = body.direction,
            pathway           = body.pathway,
            option_type       = spread.option_type,
            short_strike      = spread.short_strike,
            long_strike       = spread.long_strike,
            expiration_date   = spread.expiration_date,
            dte_at_open       = spread.target_dte,
            contracts         = contracts,
            position_size_usd = body.position_size_usd,
            window_id         = body.window_id or "",
            agent_scores      = _json.dumps(body.agent_scores or {}),
            verdict           = body.verdict,
        )

    # ── Place Order via Alpaca OUTSIDE lock (network call) ────────────────────
    short_symbol = None
    long_symbol  = None
    order_error  = None
    short_order_id = None
    long_order_id  = None

    try:
        short_symbol = _build_contract_symbol(
            spread.underlying, spread.expiration_date, spread.option_type, spread.short_strike
        )
        long_symbol = _build_contract_symbol(
            spread.underlying, spread.expiration_date, spread.option_type, spread.long_strike
        )
        legs = [
            {"symbol": short_symbol, "side": "sell", "ratio_qty": 1},
            {"symbol": long_symbol,  "side": "buy",  "ratio_qty": 1},
        ]
        # Use limit orders for spreads — market orders carry unacceptable slippage risk.
        estimated_credit = round(current_price * 0.003, 2)   # ~0.3% of spot as floor
        order = alpaca.place_spread_order(
            legs        = legs,
            order_type  = "limit",
            limit_debit = estimated_credit,
        )
        short_order_id = order.get("id")
        long_order_id  = order.get("id")   # spread is a single multi-leg order
        logger.info("Alpha spread order placed: %s | order_id=%s", spread.leg_description(), short_order_id)
    except Exception as e:
        order_error = str(e)[:200]
        logger.error("Alpaca spread order FAILED for %s: %s — cancelling pending slot", body.ticker, e)
        # Cancel the reservation so the slot is freed for the next request.
        if pending_id:
            cancel_pending_position(settings.alpha_db_path, pending_id)
        return JSONResponse(
            status_code = 503,
            content     = {
                "executed":    False,
                "reason":      f"Alpaca order placement failed: {order_error}",
                "position_id": None,
            },
        )

    # ── Alpaca confirmed — promote pending slot to open position ──────────────
    # Cipher Finding 1+2 fix: position_id was reserved inside the lock as 'pending'.
    # confirm_pending_position() sets status='open' + Alpaca fields. No new INSERT.
    confirm_pending_position(
        db_path               = settings.alpha_db_path,
        position_id           = pending_id,
        short_alpaca_order_id = short_order_id or "",
        long_alpaca_order_id  = long_order_id or "",
        short_contract_symbol = short_symbol or "",
        long_contract_symbol  = long_symbol or "",
        entry_price           = current_price,
    )
    position_id = pending_id

    # Update in-memory counters (DB is the authoritative source — these are display only)
    # OMNI M-NEW-3 fix: protect app_state counter increments with _state_lock.
    # Prime Execution received this fix; Alpha was missed. Race condition on
    # display-only counters — DB is authoritative, but inconsistency must be resolved.
    with _state_lock:
        app_state["trades_today"] += 1
        app_state["total_trades"] += 1

    # ── Telegram Notification ────────────────────────────────────────────────
    _send_entry_notification(
        settings.telegram_bot_token, settings.ahmed_chat_id,
        body, spread, position_id, contracts, current_price, short_order_id,
    )

    return JSONResponse({
        "executed":      True,
        "position_id":   position_id,
        "ticker":        body.ticker,
        "direction":     body.direction,
        "spread":        spread.leg_description(),
        "short_strike":  spread.short_strike,
        "long_strike":   spread.long_strike,
        "expiry":        spread.expiration_date,
        "dte":           spread.target_dte,
        "contracts":     contracts,
        "size_usd":      body.position_size_usd,
        "order_id":      short_order_id,
        "order_error":   order_error,
    })


@app.get("/positions")
def get_positions(x_nexus_secret: str = Header(default="")) -> JSONResponse:
    """Return all open Alpha positions."""
    verify_secret(x_nexus_secret)
    positions = get_open_positions(settings.alpha_db_path)
    return JSONResponse({
        "count":     len(positions),
        "positions": positions,
    })


@app.post("/resume")
def resume_execution(x_nexus_secret: str = Header(default="")) -> JSONResponse:
    """No-op resume endpoint for API parity with Prime Execution.

    Cipher P2-5 fix: Guardian Angel calls /resume on all execution services.
    Alpha Execution has no execution_paused state — always returns resumed=True.
    """
    verify_secret(x_nexus_secret)
    return JSONResponse({"resumed": True, "was_paused": False})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_contract_symbol(
    underlying: str,
    expiry:     str,   # 'YYYY-MM-DD'
    opt_type:   str,   # 'call' or 'put'
    strike:     float,
) -> str:
    """
    Build OCC option contract symbol.

    Format: SYMBOL + YYMMDD + C/P + 8-digit strike (strike * 1000, padded)
    Example: NVDA250522P00900000

    Adversarial fix #11: validates expiry format via datetime.strptime before
    building the symbol, raising ValueError on malformed input instead of
    producing a silently wrong symbol string.

    Args:
        underlying: Stock symbol.
        expiry:     Expiration date string 'YYYY-MM-DD'.
        opt_type:   'call' or 'put'.
        strike:     Strike price.

    Returns:
        OCC contract symbol string.

    Raises:
        ValueError: If expiry is not a valid 'YYYY-MM-DD' date string.
    """
    from datetime import datetime as _dt
    try:
        dt = _dt.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        raise ValueError(
            f"Invalid expiration date format '{expiry}' — expected YYYY-MM-DD"
        )
    yy        = dt.strftime("%y")
    mm        = dt.strftime("%m")
    dd        = dt.strftime("%d")
    type_char = "C" if opt_type == "call" else "P"
    # OCC format: strike * 1000, rounded (not truncated) to avoid symbol mismatch
    strike_int = round(strike * 1000)
    return f"{underlying}{yy}{mm}{dd}{type_char}{strike_int:08d}"


def _send_entry_notification(
    bot_token: str, chat_id: str,
    body, spread, position_id: int,
    contracts: int, current_price: float,
    order_id: Optional[str],
) -> None:
    """Send trade opened Telegram notification."""
    import requests
    direction_emoji = "📈" if body.direction == "bullish" else "📉"
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       (
                    f"{direction_emoji} <b>ALPHA TRADE OPENED — {body.ticker}</b>\n"
                    f"Direction: {body.direction.upper()}\n"
                    f"Spread: {spread.leg_description()}\n"
                    f"Contracts: {contracts} | Size: ${body.position_size_usd:,.0f}\n"
                    f"Pathway: {body.pathway} | Score: {body.weighted_score:.1f}\n"
                    f"Position #{position_id}"
                ),
                "parse_mode": "HTML",
            },
            timeout=8,
        )
    except Exception:
        pass
