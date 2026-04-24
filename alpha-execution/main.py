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
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.sovereign_comms import EscalationLevel, get_instructions, report


def _report_exec_event(
    event: str,
    ticker: str,
    direction: str,
    reason: str,
    pathway: str = "",
    extra: Optional[dict] = None,
) -> None:
    """
    Fire-and-forget execution event to SOVEREIGN bus.

    Used for every execution attempt outcome — success, error, or rejection.
    Never raises.

    Args:
        event:     Event type: 'execution_error', 'execution_rejected', 'trade_placed'.
        ticker:    Ticker symbol.
        direction: 'bullish' or 'bearish'.
        reason:    Human-readable outcome description.
        pathway:   Concordance pathway (P1/P2/P3/P4) — optional.
        extra:     Additional key/value pairs to include in the payload.
    """
    payload: dict = {
        "event":     event,
        "ticker":    ticker,
        "direction": direction,
        "reason":    reason,
    }
    if pathway:
        payload["pathway"] = pathway
    if extra:
        payload.update(extra)
    report("alpha_execution", "alert", payload, escalation=EscalationLevel.CRITICAL)

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from alpaca_client import AlpacaClient
from config import ALPACA_PAPER_URL
from buffer_reporter import BufferReporter
from config import (
    EARNINGS_BLOCK_DAYS,
    MAX_CONCURRENT_POSITIONS,
    MAX_NEW_PER_DAY,
    TARGET_DTE,
    VIX_BRAKE_ELEVATED,
    VIX_BRAKE_FULL,
    load_settings,
)
from contract_resolver import resolve_spread, resolve_available_contract
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
    "settings":            settings,
    "start_time":          datetime.now(ET).isoformat(),
    "trades_today":        0,
    "total_trades":        0,
    "execution_paused":    False,
    "first_exec_failed":   False,
    "alpaca_reachable":    None,  # None=unchecked, True=ok, False=unreachable
    "auto_execute":  os.getenv("NEXUS_AUTO_EXECUTE", "false").lower() == "true",
    "mode":          "live" if os.getenv("NEXUS_AUTO_EXECUTE", "false").lower() == "true" else "dry_run",
    # Exit monitor state (S10 fix)
    "exit_monitor_last_run_at":     None,
    "exit_monitor_last_event_count": 0,
    "exit_monitor_last_error":      None,
    "exit_monitor_run_count":       0,
}
if not app_state["auto_execute"]:
    logger.warning("NEXUS_AUTO_EXECUTE=false — running in DRY_RUN mode. No Alpaca orders will be placed.")
else:
    logger.warning("NEXUS_AUTO_EXECUTE=true — LIVE TRADING MODE. Orders will be submitted to Alpaca.")
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


def _get_current_vix() -> Optional[float]:
    """
    Fetch current VIX from Axiom's /health endpoint.
    Returns None if Axiom is unreachable or VIX is unavailable.
    Non-blocking — fails gracefully.
    """
    try:
        import requests as _req
        resp = _req.get(
            f"{settings.axiom_url}/health",
            headers={"X-Axiom-Secret": settings.axiom_secret},
            timeout=3,
        )
        if resp.status_code == 200:
            vix = resp.json().get("vix")
            return float(vix) if vix is not None else None
    except Exception as e:
        logger.warning("VIX fetch from Axiom failed: %s", e)
    return None


def _check_vix_brake(vix: Optional[float], direction: str) -> Optional[str]:
    """
    Check VIX brake gates.

    Returns:
        None if trade is allowed.
        Rejection reason string if trade should be blocked.
    """
    if vix is None:
        return None   # Can't check — allow trade with warning logged

    if vix >= VIX_BRAKE_FULL:
        return f"VIX FULL BRAKE: VIX={vix:.1f} ≥ {VIX_BRAKE_FULL} — ALL new positions blocked"

    if vix >= VIX_BRAKE_ELEVATED:
        # Debit positions blocked at elevated VIX; credit spreads may continue
        # Alpha trades credit spreads — allow. Flag in response.
        logger.info("VIX ELEVATED (%.1f) — credit spreads allowed, debit positions blocked", vix)
        return None   # Credit spreads are fine at elevated VIX

    return None


def _check_earnings_gate(ticker: str, expiration_date: str, polygon_key: str) -> Optional[str]:
    """
    Check if ticker has earnings within DTE + EARNINGS_BLOCK_DAYS days.

    Uses Polygon.io earnings calendar. Returns rejection reason or None if clear.

    Args:
        ticker:          Stock ticker symbol.
        expiration_date: Options expiry date 'YYYY-MM-DD'.
        polygon_key:     Polygon API key. If empty, gate is skipped with warning.

    Returns:
        None if clear to trade.
        Rejection reason string if earnings gate fires.
    """
    if not polygon_key:
        logger.warning("EARNINGS GATE: No Polygon API key — gate skipped for %s", ticker)
        return None

    try:
        import requests as _req
        from datetime import date as _date, timedelta as _td

        today = _date.today()
        expiry = _date.fromisoformat(expiration_date)
        block_until = expiry + _td(days=EARNINGS_BLOCK_DAYS)

        # Polygon earnings calendar endpoint
        url = (
            f"https://api.polygon.io/v2/reference/financials/{ticker}"
            f"?limit=4&type=Q&apiKey={polygon_key}"
        )
        resp = _req.get(url, timeout=8)
        if resp.status_code != 200:
            # Fallback: try the newer earnings endpoint
            url2 = (
                f"https://api.polygon.io/v3/reference/tickers/{ticker}"
                f"?apiKey={polygon_key}"
            )
            resp2 = _req.get(url2, timeout=5)
            if resp2.status_code != 200:
                logger.warning("EARNINGS GATE: Polygon returned %d for %s — gate skipped", resp.status_code, ticker)
                return None
            # Can't get earnings from ticker endpoint alone — skip
            return None

        data = resp.json()
        results = data.get("results", [])
        for record in results:
            # Next earnings date field
            report_date_str = record.get("reportDate") or record.get("report_date")
            if not report_date_str:
                continue
            try:
                report_date = _date.fromisoformat(report_date_str[:10])
            except ValueError:
                continue
            if today <= report_date <= block_until:
                return (
                    f"EARNINGS GATE: {ticker} has earnings on {report_date_str} "
                    f"which is within {EARNINGS_BLOCK_DAYS} days of expiry {expiration_date}"
                )

    except Exception as e:
        logger.warning("EARNINGS GATE: Check failed for %s: %s — gate skipped", ticker, e)

    return None


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
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
    from shared.db_guard import assert_unique_db_path  # S4: collision guard
    assert_unique_db_path("alpha-execution", settings.alpha_db_path)
    logger.info("Alpha Execution starting...")
    # G9 SYS-2: Auth registry validation
    from shared.auth_registry import validate_service_auth_config, AuthConfigError
    try:
        validate_service_auth_config("alpha-execution")
        logger.info("Auth registry validation passed for alpha-execution")
    except AuthConfigError as e:
        logger.critical("Auth config invalid: %s", e)
    # G11 SYS-4: Validate Alpaca API key at startup
    from shared.api_key_validator import ApiKeyValidator
    _validator = ApiKeyValidator()
    _alpaca_result = _validator.validate_alpaca(
        settings.alpaca_api_key, settings.alpaca_secret_key, ALPACA_PAPER_URL
    )
    if _alpaca_result.status == "failed":
        logger.critical("API key probe: alpaca → FAILED (%s)", _alpaca_result.message)
    else:
        logger.info("API key probe: alpaca → %s (%s)", _alpaca_result.status, _alpaca_result.message)
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

    def _dispatch_sovereign_instruction(instr: dict) -> None:
        """Execute a SOVEREIGN instruction. Supported: HALT, RESUME, STATUS, FLUSH."""
        raw = instr.get("message", "").strip()
        directive = raw.split(":", 1)[0].strip().upper() if ":" in raw else raw.upper()
        logger.info("SOVEREIGN directive received — raw: %s", raw[:200])

        if directive == "HALT":
            with _state_lock:
                app_state["execution_paused"] = True
            logger.warning("SOVEREIGN DIRECTIVE: HALT — execution engine paused")
            report("alpha_execution", "ack", {"directive": "HALT", "status": "applied", "paused": True})
        elif directive == "RESUME":
            with _state_lock:
                app_state["execution_paused"] = False
            logger.info("SOVEREIGN DIRECTIVE: RESUME — execution engine resumed")
            report("alpha_execution", "ack", {"directive": "RESUME", "status": "applied", "paused": False})
        elif directive == "STATUS":
            with _state_lock:
                snap = {
                    "directive": "STATUS",
                    "paused": app_state.get("execution_paused", False),
                    "auto_execute": app_state.get("auto_execute", False),
                    "trades_today": app_state.get("trades_today", 0),
                    "total_trades": app_state.get("total_trades", 0),
                    "alpaca_reachable": app_state.get("alpaca_reachable"),
                }
            report("alpha_execution", "status", snap)
            logger.info("SOVEREIGN DIRECTIVE: STATUS — reported back")
        elif directive == "FLUSH":
            with _state_lock:
                app_state["trades_today"] = 0
                app_state["first_exec_failed"] = False
            logger.info("SOVEREIGN DIRECTIVE: FLUSH — daily trade counter reset")
            report("alpha_execution", "ack", {"directive": "FLUSH", "status": "applied"})
        else:
            logger.warning("SOVEREIGN DIRECTIVE: unrecognized '%s'", directive[:100])
            report("alpha_execution", "ack", {"directive": directive[:100], "status": "unrecognized"})

    _alpha_exec_last_heartbeat = [0.0]   # mutable cell for closure

    def _poll_sovereign_instructions() -> None:
        """Poll SOVEREIGN inbox every 30s; push heartbeat every 5 minutes."""
        try:
            instr = get_instructions("alpha_execution")
            if instr:
                logger.info("Alpha Execution: %d instruction(s) from SOVEREIGN", len(instr))
                for _i in instr:
                    _dispatch_sovereign_instruction(_i)
            import time as _time
            now = _time.monotonic()
            if now - _alpha_exec_last_heartbeat[0] >= 300:
                with _state_lock:
                    snap = {
                        "event":         "heartbeat",
                        "paused":        app_state.get("execution_paused", False),
                        "auto_execute":  app_state.get("auto_execute", False),
                        "trades_today":  app_state.get("trades_today", 0),
                        "total_trades":  app_state.get("total_trades", 0),
                        "ts":            __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    }
                report("alpha_execution", "AUTONOMOUS", snap)
                _alpha_exec_last_heartbeat[0] = now
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alpha Execution: SOVEREIGN poll error: %s", exc)

    from apscheduler.triggers.interval import IntervalTrigger
    scheduler.add_job(
        func             = _poll_sovereign_instructions,
        trigger          = IntervalTrigger(seconds=30),
        id               = "sovereign_poll",
        replace_existing = True,
    )
    scheduler.start()
    app_state["scheduler"] = scheduler
    logger.info("Alpha Execution ready")
    report("alpha_execution", "status", {"event": "started", "auto_execute": os.getenv("NEXUS_AUTO_EXECUTE", "false")})
    _instr = get_instructions("alpha_execution")
    if _instr:
        logger.info("Alpha Execution: %d instruction(s) from SOVEREIGN on startup", len(_instr))
        for _i in _instr:
            _dispatch_sovereign_instruction(_i)
    yield
    scheduler.shutdown(wait=False)
    logger.info("Alpha Execution stopped")


def _run_exit_monitor() -> None:
    """Scheduled exit monitor tick. Updates app_state for /exit-monitor/status."""
    if not _is_market_hours():
        return
    app_state["exit_monitor_last_run_at"] = datetime.now(ET).isoformat()
    app_state["exit_monitor_run_count"]  += 1
    try:
        events = evaluate_exits(
            db_path         = settings.alpha_db_path,
            alpaca_client   = _get_alpaca(),
            buffer_reporter = _get_reporter(),
            bot_token       = settings.telegram_bot_token,
            chat_id         = settings.ahmed_chat_id,
        )
        app_state["exit_monitor_last_event_count"] = len(events)
        app_state["exit_monitor_last_error"]       = None
        if events:
            logger.info("Exit monitor: %d events", len(events))
    except Exception as e:
        app_state["exit_monitor_last_error"] = str(e)[:200]
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
    """Health check with real Alpaca connectivity probe and VIX brake status."""
    # Probe Alpaca — real connectivity check, not just a ping
    try:
        acct = _get_alpaca().get_account()
        alpaca_ok = acct.get("status") == "ACTIVE"
    except Exception:
        alpaca_ok = False
    with _state_lock:
        app_state["alpaca_reachable"] = alpaca_ok

    # VIX brake status — non-blocking, best effort
    current_vix = _get_current_vix()
    vix_brake_elevated = (current_vix is not None and current_vix >= VIX_BRAKE_ELEVATED)
    vix_brake_full     = (current_vix is not None and current_vix >= VIX_BRAKE_FULL)
    vix_brake_status   = (
        "FULL_HALT"  if vix_brake_full else
        "ELEVATED"   if vix_brake_elevated else
        "CLEAR"      if current_vix is not None else
        "UNKNOWN"
    )

    execution_valid = alpaca_ok and not app_state.get("execution_paused", False) and not vix_brake_full
    return JSONResponse({
        "status":             "healthy" if execution_valid else "degraded",
        "service":            "alpha-execution",
        "version":            "3.0.0",
        "trades_today":       app_state["trades_today"],
        "open_positions":     count_open_positions(settings.alpha_db_path),
        "uptime_since":       app_state["start_time"],
        "execution_valid":    execution_valid,
        "alpaca_reachable":   alpaca_ok,
        "execution_paused":   app_state.get("execution_paused", False),
        "first_exec_failed":  app_state.get("first_exec_failed", False),
        "vix":                current_vix,
        "vix_brake":          vix_brake_status,
        "vix_brake_elevated": vix_brake_elevated,
        "vix_brake_full":     vix_brake_full,
        "max_positions":      MAX_CONCURRENT_POSITIONS,
        "max_new_per_day":    MAX_NEW_PER_DAY,
        "auto_execute":       app_state.get("auto_execute", False),
        "mode":               app_state.get("mode", "dry_run"),
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

    # Market hours gate — never submit orders outside 9:30 AM – 4:00 PM ET.
    # Prevents after-hours order submission caused by service restarts, stale
    # queue flushes, or OMNI signals that arrive outside trading hours.
    # _is_market_hours() is used by the exit monitor but was previously absent here.
    if not _is_market_hours():
        logger.warning(
            "Alpha execution rejected: outside market hours — ticker=%s window=%s",
            body.ticker, body.window_id,
        )
        return JSONResponse(
            status_code=422,
            content={
                "executed": False,
                "reason":   "Outside market hours (9:30 AM – 4:00 PM ET, weekdays only)",
            },
        )

    # First-failure pause gate — if execution has been paused, reject immediately
    if app_state.get("execution_paused", False):
        logger.warning("Alpha execution PAUSED — rejecting %s signal", body.ticker)
        return JSONResponse(
            status_code=503,
            content={
                "executed": False,
                "reason":   "Execution paused due to prior failure — manual resume required (/resume)",
            },
        )

    # Pipeline Sentinel — execution received this signal
    _ps_trace_id = f"{body.window_id}:{body.ticker}"
    try:
        from pipeline_client import trace_hop as _trace_hop
        _trace_hop(_ps_trace_id, "execution_received", "alpha-execution", body.ticker, "alpha")
    except Exception:
        pass

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

    # ── VIX Brake Gate ────────────────────────────────────────────────────────
    # Check before any DB write or Alpaca call. Non-blocking Axiom probe.
    current_vix = _get_current_vix()
    vix_rejection = _check_vix_brake(current_vix, body.direction)
    if vix_rejection:
        logger.warning("VIX BRAKE FIRED for %s: %s", body.ticker, vix_rejection)
        try:
            import requests as _vix_req
            _vix_req.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": settings.ahmed_chat_id, "text": f"🚨 VIX BRAKE\n{vix_rejection}\nTicker: {body.ticker}"},
                timeout=5,
            )
        except Exception:
            pass
        return JSONResponse(
            status_code=422,
            content={"executed": False, "reason": vix_rejection, "vix_brake": True},
        )

    alpaca = _get_alpaca()

    # ── Get Current Price (outside lock — network call, no state mutation) ────
    current_price = alpaca.get_latest_price(body.ticker)
    if not current_price:
        logger.warning("Cannot get price for %s — execution rejected", body.ticker)
        _report_exec_event(
            "execution_error", body.ticker, body.direction,
            reason=f"Cannot fetch price for {body.ticker}",
            pathway=body.pathway or "",
        )
        return JSONResponse(
            status_code = 503,
            content     = {"executed": False, "reason": f"Cannot fetch price for {body.ticker}"},
        )

    # ── Resolve Spread Parameters ─────────────────────────────────────────────
    try:
        spread = resolve_available_contract(
            ticker        = body.ticker,
            direction     = body.direction,
            current_price = current_price,
            alpaca_client = alpaca,
        )
    except ValueError as e:
        _report_exec_event(
            "execution_error", body.ticker, body.direction,
            reason=f"Spread resolution failed: {str(e)[:200]}",
            pathway=body.pathway or "",
        )
        logger.warning("SPREAD RESOLUTION FAILED for %s/%s: %s", body.ticker, body.direction, e)
        # Alert Ahmed — GO verdict reached execution but no tradeable contract exists
        try:
            import requests as _sr
            _sr.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": settings.ahmed_chat_id,
                    "text": (
                        f"⚠️ <b>SPREAD RESOLUTION FAILED</b>\n"
                        f"Ticker: <b>{body.ticker}</b> | {body.direction} | {body.pathway}\n"
                        f"Verdict was GO — but no tradeable contract found.\n"
                        f"Reason: {str(e)[:200]}"
                    ),
                    "parse_mode": "HTML",
                },
                timeout=5,
            )
        except Exception:
            pass
        return JSONResponse(
            status_code = 422,
            content     = {"executed": False, "reason": str(e)},
        )

    # ── Earnings Gate ─────────────────────────────────────────────────────────
    # Check after spread resolution (need expiration_date). Fires if earnings
    # fall within expiration + EARNINGS_BLOCK_DAYS (5 days).
    earnings_rejection = _check_earnings_gate(
        body.ticker, spread.expiration_date, settings.polygon_api_key
    )
    if earnings_rejection:
        logger.warning("EARNINGS GATE FIRED for %s: %s", body.ticker, earnings_rejection)
        _report_exec_event(
            "execution_rejected", body.ticker, body.direction,
            reason=earnings_rejection,
            pathway=body.pathway or "",
            extra={"gate": "earnings"},
        )
        return JSONResponse(
            status_code=422,
            content={"executed": False, "reason": earnings_rejection, "earnings_gate": True},
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
            _report_exec_event(
                "execution_rejected", body.ticker, body.direction,
                reason=f"Max concurrent positions reached ({open_count}/{MAX_CONCURRENT_POSITIONS})",
                pathway=body.pathway or "",
                extra={"gate": "concurrent_limit"},
            )
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
            _report_exec_event(
                "execution_rejected", body.ticker, body.direction,
                reason=f"Max new positions today reached ({today_count}/{MAX_NEW_PER_DAY})",
                pathway=body.pathway or "",
                extra={"gate": "daily_limit"},
            )
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

    # ── AUTO_EXECUTE Gate — dry-run if kill-switch is off ──────────────────────
    if not app_state.get("auto_execute", False):
        order_preview = {
            "ticker":           body.ticker,
            "direction":        body.direction,
            "verdict":          body.verdict,
            "spread_details":   spread.__dict__ if hasattr(spread, "__dict__") else str(spread),
            "current_price":    current_price,
        }
        logger.info("DRY_RUN: would have executed %s %s (verdict=%s)", body.ticker, body.direction, body.verdict)
        if pending_id:
            cancel_pending_position(settings.alpha_db_path, pending_id)
        # Send dry-run Telegram alert
        try:
            import requests as _dr_req
            _dr_req.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": settings.ahmed_chat_id, "text": f"[DRY RUN] Would execute: {body.ticker} {body.direction} | verdict={body.verdict} | price=${current_price}"},
                timeout=5,
            )
        except Exception:
            pass
        return JSONResponse(content={"executed": False, "mode": "dry_run", "order_preview": order_preview})

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
        # Credit floor: 1.5% of spot price, minimum $0.50 per contract.
        # Paper account options spreads require realistic credit to achieve fills.
        # Previous 0.3% (~$0.60 for NVDA) was too low — orders never filled.
        estimated_credit = max(round(current_price * 0.015, 2), 0.50)
        order = alpaca.place_spread_order(
            legs        = legs,
            qty         = contracts,
            order_type  = "limit",
            limit_debit = estimated_credit,
        )
        short_order_id = order.get("id")
        long_order_id  = order.get("id")   # spread is a single multi-leg order
        logger.info("Alpha spread order placed: %s | order_id=%s", spread.leg_description(), short_order_id)
        try:
            from pipeline_client import trace_hop as _trace_hop
            _trace_hop(_ps_trace_id, "alpaca_submitted", "alpha-execution", body.ticker, "alpha")
        except Exception:
            pass
    except Exception as e:
        order_error = str(e)[:200]
        logger.error("Alpaca spread order FAILED for %s: %s — cancelling pending slot", body.ticker, e)
        # Cancel the reservation so the slot is freed for the next request.
        if pending_id:
            cancel_pending_position(settings.alpha_db_path, pending_id)
        # Always report every Alpaca failure to SOVEREIGN — regardless of whether
        # execution is already paused. SOVEREIGN needs the full error history.
        _report_exec_event(
            "execution_error", body.ticker, body.direction,
            reason=f"Alpaca order placement failed: {order_error}",
            pathway=body.pathway or "",
            extra={"error": order_error, "gate": "alpaca_order"},
        )
        # First-failure pause: halt all execution and alert Ahmed immediately.
        # One Alpaca failure may mean connectivity loss, account issue, or options
        # routing problem — all are Tier 1. Do NOT continue submitting blind.
        with _state_lock:
            if not app_state.get("first_exec_failed", False):
                app_state["first_exec_failed"] = True
                app_state["execution_paused"]  = True
                logger.critical(
                    "FIRST EXECUTION FAILURE — pausing Alpha Execution. "
                    "Ticker=%s Error=%s. Resume via /resume after diagnosis.",
                    body.ticker, order_error,
                )
                report("alpha_execution", "alert", {
                    "event":  "execution_paused",
                    "ticker": body.ticker,
                    "error":  order_error,
                    "reason": "first_execution_failure",
                }, escalation=EscalationLevel.CRITICAL)
                try:
                    import requests as _req
                    _req.post(
                        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                        json={
                            "chat_id": settings.ahmed_chat_id,
                            "text": (
                                f"🚨 ALPHA EXECUTION PAUSED\n"
                                f"First live order failed — halting all execution.\n"
                                f"Ticker: {body.ticker}\n"
                                f"Error: {order_error}\n"
                                f"Resume: POST /execute-engine/resume with auth header"
                            ),
                        },
                        timeout=5,
                    )
                except Exception:
                    pass
        return JSONResponse(
            status_code = 503,
            content     = {
                "executed":    False,
                "reason":      f"Alpaca order placement failed: {order_error}",
                "position_id": None,
            },
        )

    # ── G10 SYS-3: OSI-Aware Fill Confirmation ───────────────────────────────
    # Poll Alpaca to confirm fill before writing to DB.
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
    from shared.order_reconciler import OrderReconciler, FillStatus as _FillStatus
    _reconciler = OrderReconciler(
        alpaca_base_url    = ALPACA_PAPER_URL,
        alpaca_key         = settings.alpaca_api_key,
        alpaca_secret      = settings.alpaca_secret_key,
        dry_run            = os.getenv("RECONCILE_ORDERS", "").lower() != "true",
    )
    _fill = _reconciler.confirm_fill(
        order_id     = short_order_id or "",
        ticker       = body.ticker,
        expected_qty = float(contracts),
    )
    if _fill.status == _FillStatus.VOID:
        logger.error(
            "Trade VOID for %s: %s (elapsed: %.1fs)",
            body.ticker, _fill.void_reason, _fill.elapsed_sec,
        )
        if pending_id:
            cancel_pending_position(settings.alpha_db_path, pending_id)
        _report_exec_event(
            "execution_error", body.ticker, body.direction,
            reason=f"Trade void: {_fill.void_reason}",
            pathway=body.pathway or "",
            extra={"error": _fill.void_reason, "gate": "fill_confirmation", "elapsed_sec": _fill.elapsed_sec},
        )
        try:
            import requests as _vr
            _vr.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": settings.ahmed_chat_id,
                      "text": f"⚠️ VOID trade: {body.ticker} — {_fill.void_reason}"},
                timeout=5,
            )
        except Exception:
            pass
        return JSONResponse(
            status_code=503,
            content={"status": "void", "reason": _fill.void_reason, "executed": False},
        )

    # ── Alpaca confirmed — promote pending slot to open position ──────────────
    # Cipher Finding 1+2 fix: position_id was reserved inside the lock as 'pending'.
    # confirm_pending_position() sets status='open' + Alpaca fields. No new INSERT.
    _confirmed_price = _fill.fill_price if _fill.fill_price is not None else current_price
    confirm_pending_position(
        db_path               = settings.alpha_db_path,
        position_id           = pending_id,
        short_alpaca_order_id = short_order_id or "",
        long_alpaca_order_id  = long_order_id or "",
        short_contract_symbol = short_symbol or "",
        long_contract_symbol  = long_symbol or "",
        entry_price           = _confirmed_price,
    )
    position_id = pending_id

    # Update in-memory counters (DB is the authoritative source — these are display only)
    # OMNI M-NEW-3 fix: protect app_state counter increments with _state_lock.
    # Prime Execution received this fix; Alpha was missed. Race condition on
    # display-only counters — DB is authoritative, but inconsistency must be resolved.
    with _state_lock:
        app_state["trades_today"] += 1
        app_state["total_trades"] += 1

    # Pipeline Sentinel — Alpaca order confirmed, position live
    try:
        from pipeline_client import trace_hop as _trace_hop
        _trace_hop(_ps_trace_id, "alpaca_confirmed", "alpha-execution", body.ticker, "alpha")
    except Exception:
        pass

    report("alpha_execution", "alert", {
        "event":       "trade_placed",
        "ticker":      body.ticker,
        "direction":   body.direction,
        "verdict":     body.verdict,
        "order_id":    short_order_id or "",
        "position_id": position_id,
        "contracts":   contracts,
        "size_usd":    body.position_size_usd,
    }, escalation=EscalationLevel.CRITICAL)

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


@app.get("/trades")
def get_trades_summary(x_nexus_secret: str = Header(default="")) -> JSONResponse:
    """
    Return Alpha execution summary for SOVEREIGN observability.

    Exposes: session trades, Alpaca orders submitted/filled/rejected,
    open positions, and buying power state.  SOVEREIGN polls this at
    market open, midday, and close every session.
    """
    verify_secret(x_nexus_secret)

    import sqlite3 as _sqlite3
    orders_submitted = 0
    orders_filled = 0
    orders_rejected = 0
    buying_power_used = 0.0
    try:
        conn = _sqlite3.connect(settings.alpha_db_path)
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            "SELECT status, position_size_usd FROM positions WHERE DATE(opened_at)=DATE('now')"
        ).fetchall()
        conn.close()
        for row in rows:
            orders_submitted += 1
            st = (row["status"] or "").lower()
            if st in ("open", "closed", "partial_exit"):
                orders_filled += 1
            elif "reject" in st or "fail" in st:
                orders_rejected += 1
            buying_power_used += float(row["position_size_usd"] or 0)
    except Exception:
        pass

    open_pos = count_open_positions(settings.alpha_db_path)

    return JSONResponse({
        "service":                "alpha-execution",
        "session_date":           __import__("datetime").date.today().isoformat(),
        "go_verdicts":            app_state.get("trades_today", 0),
        "alpaca_orders_submitted": orders_submitted,
        "alpaca_orders_filled":   orders_filled,
        "alpaca_orders_rejected": orders_rejected,
        "open_positions_count":   open_pos,
        "buying_power_used_usd":  round(buying_power_used, 2),
        "execution_paused":       app_state.get("execution_paused", False),
        "auto_execute":           app_state.get("auto_execute", False),
        "alpaca_reachable":       app_state.get("alpaca_reachable", False),
        "vix_brake":              app_state.get("vix_brake_status", "UNKNOWN"),
    })


@app.get("/exit-monitor/status")
def exit_monitor_status(x_nexus_secret: str = Header(default="")) -> JSONResponse:
    """
    Return current exit monitor scheduler state.

    Reports last run timestamp, last event count, any errors, and scheduler job status.
    The exit monitor runs every 1 min during market hours (9–15 ET) via APScheduler.
    """
    verify_secret(x_nexus_secret)
    scheduler = app_state.get("scheduler")
    next_run_at = None
    scheduler_running = False
    if scheduler:
        scheduler_running = scheduler.running
        try:
            job = scheduler.get_job("exit_monitor")
            if job and job.next_run_time:
                next_run_at = job.next_run_time.isoformat()
        except Exception:
            pass

    return JSONResponse({
        "exit_monitor": {
            "scheduler_running":  scheduler_running,
            "schedule":           "every 1 min during market hours (09:00–15:59 ET)",
            "last_run_at":        app_state.get("exit_monitor_last_run_at"),
            "last_event_count":   app_state.get("exit_monitor_last_event_count", 0),
            "last_error":         app_state.get("exit_monitor_last_error"),
            "run_count":          app_state.get("exit_monitor_run_count", 0),
            "next_run_at":        next_run_at,
        }
    })


@app.post("/resume")
def resume_execution(x_nexus_secret: str = Header(default="")) -> JSONResponse:
    """Resume execution after a failure-triggered pause.

    Clears execution_paused and first_exec_failed flags so new orders can proceed.
    Guardian Angel calls this endpoint on recovery.
    """
    verify_secret(x_nexus_secret)
    with _state_lock:
        was_paused = app_state.get("execution_paused", False)
        app_state["execution_paused"]  = False
        app_state["first_exec_failed"] = False
    logger.info("Alpha execution RESUMED (was_paused=%s)", was_paused)
    return JSONResponse({"resumed": True, "was_paused": was_paused})



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
    """SOVEREIGN pushes a directive. Zero polling lag."""
    verify_secret(x_nexus_secret)
    d = body.directive.strip().upper()
    logger.info("SOVEREIGN direct push: %s", d)

    if d == "PING":
        report("alpha_execution", "ack", {"directive": "PING", "status": "alive"})
        return JSONResponse({"ok": True, "directive": "PING", "status": "alive"})
    elif d == "HALT":
        with _state_lock:
            app_state["execution_paused"] = True
        report("alpha_execution", "ack", {"directive": "HALT", "status": "applied", "paused": True})
        return JSONResponse({"ok": True, "directive": "HALT", "paused": True})
    elif d == "RESUME":
        with _state_lock:
            app_state["execution_paused"] = False
            app_state["first_exec_failed"] = False
        report("alpha_execution", "ack", {"directive": "RESUME", "status": "applied", "paused": False})
        return JSONResponse({"ok": True, "directive": "RESUME", "paused": False})
    elif d in ("FLUSH", "RESET_DAY"):
        with _state_lock:
            app_state["trades_today"] = 0
            app_state["first_exec_failed"] = False
        report("alpha_execution", "ack", {"directive": d, "status": "applied"})
        return JSONResponse({"ok": True, "directive": d})
    elif d == "STATUS":
        with _state_lock:
            snap = {
                "directive": "STATUS", "service": "alpha_execution",
                "paused": app_state.get("execution_paused", False),
                "auto_execute": app_state.get("auto_execute", False),
                "trades_today": app_state.get("trades_today", 0),
                "alpaca_reachable": app_state.get("alpaca_reachable"),
            }
        report("alpha_execution", "status", snap)
        return JSONResponse({"ok": True, **snap})
    else:
        report("alpha_execution", "ack", {"directive": d, "status": "unrecognized"})
        return JSONResponse({"ok": False, "error": f"unrecognized directive: {d}"}, status_code=400)


@app.get("/sovereign/status", status_code=200)
def sovereign_status(
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """SOVEREIGN queries full alpha-execution state on-demand."""
    verify_secret(x_nexus_secret)
    with _state_lock:
        return JSONResponse({
            "ok": True, "service": "alpha_execution",
            "paused": app_state.get("execution_paused", False),
            "auto_execute": app_state.get("auto_execute", False),
            "trades_today": app_state.get("trades_today", 0),
            "total_trades": app_state.get("total_trades", 0),
            "alpaca_reachable": app_state.get("alpaca_reachable"),
            "port": int(__import__("os").getenv("PORT", "8005")),
        })


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
