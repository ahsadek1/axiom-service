"""
main.py — Alpha Execution Engine FastAPI Entry Point

Receives GO signals from OMNI. Resolves options contracts.
Places trades via Alpaca paper trading. Monitors exits.

Auth: X-Nexus-Secret header → NEXUS_WEBHOOK_SECRET.
Port: 8005 (local), $PORT (Railway).
"""

import asyncio
from typing import Optional, List, Dict, Any
import glob as _glob
import hashlib as _hashlib
import logging
import os
import os as _os
import secrets
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator, Optional


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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.sovereign_comms import EscalationLevel, get_instructions, report
from shared.service_state import ServiceStateWriter as _StateWriter
_svc_state = _StateWriter("alpha-execution")  # GAP-002: durable state across restarts


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
from fastapi import Depends, FastAPI, Header, HTTPException
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
    close_stale_position,
    confirm_pending_position,
    count_new_positions_today,
    count_open_positions,
    get_open_positions,
    init_db,
    reserve_position_slot,
    ticker_already_open,
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
# GAP-2: _skipped_tickers is now a dict {ticker: datetime_added} with 30-min TTL.
# Replaces the old permanent set — a ticker banned for one bad order should not stay
# banned all day. Use _is_ticker_skipped() to check + auto-expire.
_skipped_tickers: dict = {}      # ticker → datetime added (ET); 30-min TTL per GAP-2
_ticker_fail_counts: dict = {}   # GAP-CB: consecutive execution failures per ticker
_TICKER_FAIL_THRESHOLD = 3       # GAP-CB: auto-skip after this many consecutive failures
_SKIPPED_TICKER_TTL_S  = 1800   # GAP-2: 30 minutes

# GAP-6: Per-ticker locks — prevent exit_monitor/execute race condition on same position
_ticker_locks: dict = {}          # ticker → threading.Lock
_ticker_locks_mutex = threading.Lock()  # guards the dict itself


def _get_ticker_lock(ticker: str) -> threading.Lock:
    """Get (or lazily create) the per-ticker threading.Lock. Thread-safe (GAP-6)."""
    with _ticker_locks_mutex:
        if ticker not in _ticker_locks:
            _ticker_locks[ticker] = threading.Lock()
        return _ticker_locks[ticker]


def _is_ticker_skipped(ticker: str) -> bool:
    """Check if ticker is in skip list. Expires entries older than 30 min (GAP-2 TTL)."""
    from datetime import timedelta
    now = datetime.now(ET)
    cutoff = now - timedelta(seconds=_SKIPPED_TICKER_TTL_S)
    # Expire stale entries inline (best-effort, no lock needed — worst case: brief extra skip)
    expired = [t for t, added in list(_skipped_tickers.items()) if added < cutoff]
    for t in expired:
        _skipped_tickers.pop(t, None)
        logger.info("GAP-2: skip-list TTL expired for %s (was added at %s)", t, _skipped_tickers.get(t))
    return ticker.upper() in _skipped_tickers

# ── Block 2: Startup Preflight Gate ──────────────────────────────────────────
# Module-level mode flag.  "active" = normal; "standby" = preflight failed.
# Written under _state_lock; read by /health and /execute.
_SERVICE_MODE: str = "active"
_standby_reason: str = ""
_standby_alerted: bool = False     # rate-limit: alert Ahmed only once on entry

# GAP-002: Restore durable state from prior session
_prior_state = _svc_state.restore()
_STATE_RESTORED = bool(_prior_state)  # GAP-002: expose in /health
if _prior_state.get("execution_paused"):    app_state["execution_paused"] = True
# Block 3: trades_today is derived from DB (count_new_positions_today) — do not seed from prior state
# GAP-2: restore skipped_tickers as dict with current time as added-time.
# Prior state stored only ticker names (list); restore with now() so the 30-min TTL
# applies from the moment of restart, not from an unknown prior time.
if _prior_state.get("skipped_tickers"):
    _restore_ts = datetime.now(ET)
    _skipped_tickers.update({t: _restore_ts for t in _prior_state["skipped_tickers"]})
if _prior_state.get("ticker_fail_counts"):  _ticker_fail_counts.update(_prior_state["ticker_fail_counts"])

# FLAW 4 fix: Validate restored state against live Alpaca before trusting it.
# Restored ticker_fail_counts / skipped_tickers may reference orders that Alpaca
# has already settled or rejected. Restoring them blind caused duplicate
# client_order_id 422 errors (GOOGL incident 2026-05-01).
def _validate_restored_state() -> None:
    """Clear ticker-level counters for tickers with no live Alpaca position.
    Runs once at startup, best-effort — never raises, never blocks."""
    try:
        _alpaca = AlpacaClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        live_positions = {p["symbol"] for p in _alpaca.get_positions()}
        # Clear fail counts for tickers not in live positions
        stale_fails = [t for t in list(_ticker_fail_counts.keys()) if t not in live_positions]
        for t in stale_fails:
            del _ticker_fail_counts[t]
            logger.info("FLAW4: cleared stale ticker_fail_count for %s (no live Alpaca position)", t)
        # Clear skipped tickers not in live positions either (GAP-2: _skipped_tickers is now a dict)
        stale_skipped = {t for t in list(_skipped_tickers.keys()) if t not in live_positions}
        for t in stale_skipped:
            _skipped_tickers.pop(t, None)
        if stale_skipped:
            logger.info("FLAW4: cleared stale skipped_tickers: %s", stale_skipped)
        if stale_fails or stale_skipped:
            _svc_state.write_many({"ticker_fail_counts": dict(_ticker_fail_counts),
                                   "skipped_tickers": list(_skipped_tickers.keys())})
        logger.info("FLAW4: restore validation complete. live_positions=%s", live_positions)
    except Exception as _e:
        logger.warning("FLAW4: restore validation failed (non-fatal): %s", _e)

if _prior_state:  # Only validate if there was something to restore
    _validate_restored_state()

# ── Token Bucket Rate Limiter — /execute endpoint ─────────────────────────────────────
# Commercial-grade protection: max 10 /execute calls per 60s globally.
# Protects against brute-force and runaway OMNI signal loops.
# Pure Python — no external deps. GENESIS 2026-04-27 — GAP-5.
import time as _time


class _TokenBucket:
    """Thread-safe token bucket rate limiter."""

    def __init__(self, capacity: int, refill_rate: float) -> None:
        """
        Args:
            capacity:    Max tokens (burst limit).
            refill_rate: Tokens added per second.
        """
        self._capacity    = capacity
        self._tokens      = float(capacity)
        self._refill_rate = refill_rate
        self._last_refill = _time.monotonic()
        self._lock        = threading.Lock()

    def consume(self, tokens: int = 1) -> bool:
        """Return True if request is allowed, False if rate-limited."""
        with self._lock:
            now              = _time.monotonic()
            elapsed          = now - self._last_refill
            self._tokens     = min(
                float(self._capacity),
                self._tokens + elapsed * self._refill_rate,
            )
            self._last_refill = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


# 10 requests per 60s burst; refill 1 token per 6s
_execute_rate_limiter = _TokenBucket(capacity=10, refill_rate=1.0 / 6.0)


def verify_secret(x_nexus_secret: str) -> None:
    """Verify X-Nexus-Secret header. Raises 403 if invalid.
    Uses constant-time comparison to prevent timing attacks.
    """
    if not x_nexus_secret or not secrets.compare_digest(
        x_nexus_secret, settings.nexus_webhook_secret
    ):
        raise HTTPException(status_code=403, detail="Forbidden")


def _auth_dependency(x_nexus_secret: str = Header(default="", alias="X-Nexus-Secret")) -> None:
    """GENESIS-STRESS-F4-001: FastAPI Depends-based auth that fires BEFORE body parsing.
    This ensures 403 is returned for invalid credentials before Pydantic 422 validation,
    preventing information leakage about request body structure to unauthenticated callers.
    """
    verify_secret(x_nexus_secret)


def _get_alpaca() -> AlpacaClient:
    return AlpacaClient(settings.alpaca_api_key, settings.alpaca_secret_key)


def _get_reporter() -> BufferReporter:
    return BufferReporter(settings.alpha_buffer_url, settings.nexus_webhook_secret)


def _get_current_vix() -> Optional[float]:
    """
    Fetch current VIX from Axiom's /health endpoint.
    Returns 999.0 (brake-trigger value) if Axiom is unreachable — fail SAFE (GAP-1).
    Non-blocking — never raises.
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
        # GAP-1: Fail SAFE — if Axiom is unreachable we cannot verify VIX is below brake.
        # Return 999.0 so _check_vix_brake() fires the FULL brake and halts new positions.
        # "fail open" (return None) is not acceptable — the system must not trade blind.
        logger.warning(
            "VIX fetch from Axiom failed — failing SAFE (returning 999.0 to trigger VIX brake): %s", e
        )
        return 999.0
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
        # M-08 fix: block if earnings happen DURING option life (today → expiry)
        # OR if earnings are imminent (within EARNINGS_BLOCK_DAYS from today).
        # Old logic (expiry + BLOCK_DAYS) blocked trades for earnings AFTER expiry — nonsensical.
        imminent_cutoff = today + _td(days=EARNINGS_BLOCK_DAYS)

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
            # Block if earnings fall during option life OR are imminent from today
            if today <= report_date <= expiry or report_date <= imminent_cutoff:
                return (
                    f"EARNINGS GATE: {ticker} has earnings on {report_date_str} "
                    f"(option expires {expiration_date}, imminent cutoff {imminent_cutoff})"
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


def _preflight_check() -> tuple[bool, str]:
    """Block 2: Verify startup preconditions before accepting traffic.

    Checks:
      1. Alpaca reachable — GET /v2/account returns 200 with status ACTIVE.
      2. DB writable — test INSERT + ROLLBACK on alpha DB.
      3. Market hours determination is unambiguous (pytz loaded, ET timezone confirmed).

    Returns:
        (ok: bool, reason: str)  — reason is empty when ok=True.
    """
    # Check 1: Alpaca reachable
    try:
        _a = AlpacaClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        acct = _a.get_account()
        if acct.get("status") != "ACTIVE":
            return False, f"Alpaca account status is '{acct.get('status')}' (expected ACTIVE)"
    except Exception as exc:
        return False, f"Alpaca unreachable at startup: {exc}"

    # Check 2: DB writable
    try:
        import sqlite3 as _sq
        _conn = _sq.connect(settings.alpha_db_path, timeout=5)
        _conn.execute("BEGIN")
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS _preflight_probe "
            "(id INTEGER PRIMARY KEY, ts TEXT)"
        )
        _conn.execute("INSERT INTO _preflight_probe (ts) VALUES (?)", (datetime.now(ET).isoformat(),))
        _conn.execute("ROLLBACK")
        _conn.close()
    except Exception as exc:
        return False, f"DB not writable at startup: {exc}"

    # Check 3: Market hours timezone unambiguous
    try:
        _tz = pytz.timezone("America/New_York")
        _now = datetime.now(_tz)
        if _now.tzinfo is None:
            return False, "pytz ET timezone returned naive datetime — timezone load failed"
    except Exception as exc:
        return False, f"pytz timezone check failed: {exc}"

    return True, ""


def _run_preflight_retry_loop() -> None:
    """Block 2: Background thread — retry preflight every 30s until ACTIVE.

    Transitions _SERVICE_MODE from 'standby' to 'active' when all checks pass.
    Alerts Ahmed once on entering standby, once on exiting.
    """
    global _SERVICE_MODE, _standby_reason, _standby_alerted
    import time as _time
    while True:
        _time.sleep(30)
        with _state_lock:
            if _SERVICE_MODE == "active":
                return  # already active — nothing to do
        ok, reason = _preflight_check()
        if ok:
            with _state_lock:
                _SERVICE_MODE = "active"
                _standby_reason = ""
            logger.info("Block 2: Preflight PASSED — transitioning to ACTIVE mode")
            try:
                from shared.notification_router import notify_info as _ni
                _ni("alpha-execution", "PREFLIGHT PASSED — ACTIVE",
                    "All startup checks passed. Alpha Execution is now ACTIVE.")
            except Exception:
                pass
            return


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

    # ── Block 2: Startup Preflight Gate ───────────────────────────────────────
    global _SERVICE_MODE, _standby_reason, _standby_alerted
    _pf_ok, _pf_reason = _preflight_check()
    if not _pf_ok:
        with _state_lock:
            _SERVICE_MODE = "standby"
            _standby_reason = _pf_reason
            _standby_alerted = True
        logger.critical(
            "Block 2: PREFLIGHT FAILED — entering STANDBY mode. Reason: %s", _pf_reason
        )
        try:
            from shared.notification_router import notify_escalate as _ne
            _ne("alpha-execution", "STANDBY MODE — PREFLIGHT FAILED",
                f"Alpha Execution is in STANDBY. Retrying every 30s.\nReason: {_pf_reason}")
        except Exception:
            pass
        import threading as _threading
        _threading.Thread(
            target=_run_preflight_retry_loop, daemon=True,
            name="alpha-exec-preflight-retry"
        ).start()
    else:
        logger.info("Block 2: Preflight PASSED — service starting in ACTIVE mode")

    # ── Startup Reconciliation ────────────────────────────────────────────────
    # On every service start, compare DB open positions against Alpaca.
    # If a DB position has no matching Alpaca position, it leaked — close it.
    # Root cause of the 2026-04-24 ghost: test entry with status='open' blocked
    # every GO verdict for 6 days because no startup guard existed.
    # Runs once, synchronously, before scheduler starts — safe.
    _run_startup_reconciliation()

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


def _run_startup_reconciliation() -> None:
    """
    Startup reconciliation: close any DB-open positions absent from Alpaca.

    Logic:
      1. Fetch all DB positions with status IN ('open','pending').
      2. Fetch all live positions from Alpaca.
      3. Build Alpaca ticker set.
      4. For each DB position:
         a. If window_id contains 'test' (case-insensitive) → always close;
            test entries must never survive in production position tracking.
         b. If ticker not in Alpaca live positions → close with reconcile reason.
      5. Log and alert Ahmed for each closed position.

    Non-blocking failure: if Alpaca is unreachable, logs a warning and
    skips the reconciliation rather than blocking startup.
    """
    try:
        db_positions = get_open_positions(settings.alpha_db_path)
        if not db_positions:
            logger.info("Startup reconciliation: no open positions in DB — nothing to check")
            return

        # Also include pending slots left by a previous crash
        from database import get_conn as _get_conn
        with _get_conn(settings.alpha_db_path) as _conn:
            _pending_rows = _conn.execute(
                "SELECT * FROM positions WHERE status='pending'"
            ).fetchall()
        pending_positions = [dict(r) for r in _pending_rows]

        all_db_positions = db_positions + pending_positions

        # Fetch live Alpaca tickers — best effort
        alpaca_tickers: set[str] = set()
        try:
            alpaca = _get_alpaca()
            live = alpaca.get_positions()   # returns list of {symbol: ..., ...}
            alpaca_tickers = {str(p.get("symbol", "")).upper() for p in live}
            logger.info(
                "Startup reconciliation: %d DB position(s), %d Alpaca position(s)",
                len(all_db_positions), len(alpaca_tickers),
            )
        except Exception as alpaca_err:
            logger.warning(
                "Startup reconciliation: Alpaca unreachable (%s) — "
                "will still close test-window entries; live-mismatch check skipped",
                alpaca_err,
            )
            alpaca_tickers = None   # type: ignore[assignment]

        closed_ids: list[str] = []
        for pos in all_db_positions:
            pos_id   = pos["id"]
            ticker   = (pos.get("ticker") or "").upper()
            window   = (pos.get("window_id") or "")
            status   = (pos.get("status") or "")

            # Rule A: test window entries are NEVER production positions
            if "test" in window.lower():
                reason = f"reconciled_test_window: window_id='{window}' is a test entry"
                close_stale_position(settings.alpha_db_path, pos_id, reason)
                closed_ids.append(f"#{pos_id} {ticker} (test window)")
                logger.warning(
                    "Startup reconciliation: closed test entry #%d %s window=%s",
                    pos_id, ticker, window,
                )
                continue

            # Rule B: live-mismatch check (only if Alpaca was reachable)
            if alpaca_tickers is not None and ticker not in alpaca_tickers:
                reason = (
                    f"reconciled_alpaca_empty: {ticker} is open in DB "
                    f"(status={status}) but absent from Alpaca live positions"
                )
                close_stale_position(settings.alpha_db_path, pos_id, reason)
                closed_ids.append(f"#{pos_id} {ticker} (not in Alpaca)")
                logger.warning(
                    "Startup reconciliation: closed stale position #%d %s — not in Alpaca",
                    pos_id, ticker,
                )

        if closed_ids:
            summary = ", ".join(closed_ids)
            logger.warning(
                "Startup reconciliation: closed %d stale position(s): %s",
                len(closed_ids), summary,
            )
            try:
                import sys as _sys
                _sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
                from shared.notification_router import notify_warn as _nw
                _nw(
                    "alpha-execution",
                    "Alpha Execution — Startup Reconciliation",
                    f"Closed {len(closed_ids)} stale position(s):\n" + "\n".join(f"  • {s}" for s in closed_ids),
                )
            except Exception:
                pass
        else:
            logger.info("Startup reconciliation: all DB positions confirmed in Alpaca — clean state")

    except Exception as e:
        # Never block startup — log and continue
        logger.error("Startup reconciliation failed (non-fatal): %s", e)


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
    # GENESIS-STRESS-F4-001: Apply auth dependency globally so 403 fires
    # before Pydantic body validation (422). Prevents body schema leakage.
    dependencies=[Depends(_auth_dependency)],
)


# ── Request Models ────────────────────────────────────────────────────────────

class ExecuteRequest(BaseModel):
    """OMNI execution signal payload.

    All numeric fields are Optional to prevent 422s when upstream senders
    (Railway webhook omni_scanner, legacy OMNI versions) omit fields or
    send null. Alpha-execution gates on its own risk logic — missing
    metadata is non-fatal for the execution pipeline.
    """

    ticker:            str
    direction:         str
    pathway:           str
    weighted_score:    Optional[float] = None
    agent_scores:      Optional[dict]  = None
    verdict:           Optional[str]   = None
    sizing_mult:       Optional[float] = None
    position_size_usd: Optional[float] = None
    window_id:         Optional[str]   = None
    echo_chamber:      bool            = False
    axiom_risk_score:  Optional[float] = None
    # Extra fields from Railway webhook / execution_router (ignored but accepted)
    system:            Optional[str]   = None
    votes_go:          Optional[int]   = None
    brain_summary:     Optional[dict]  = None
    auto_execute:      Optional[bool]  = None

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
    # Block 2: STANDBY fast path — return 200 with standby status so GA takes no action
    with _state_lock:
        _mode = _SERVICE_MODE
        _reason = _standby_reason
    if _mode == "standby":
        return JSONResponse({
            "status":           "standby",
            "service":          "alpha-execution",
            "version":          "3.0.0",
            "reason":           _reason,
            "execution_valid":  False,
            "alpaca_reachable": False,
        })

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

    # Block 3: derive trades_today from DB — in-memory counter is display-only cache
    trades_today_db = count_new_positions_today(settings.alpha_db_path)
    with _state_lock:
        app_state["trades_today"] = trades_today_db

    execution_valid = alpaca_ok and not app_state.get("execution_paused", False) and not vix_brake_full
    return JSONResponse({
        "status":             "healthy" if execution_valid else "degraded",
        "service":            "alpha-execution",
        "version":            "3.0.0",
        "trades_today":       trades_today_db,
        "open_positions":     count_open_positions(settings.alpha_db_path),
        "uptime_since":       app_state["start_time"],
        "uptime_sec":         int((datetime.now(ET) - datetime.fromisoformat(app_state["start_time"])).total_seconds()),
        "execution_valid":    execution_valid,
        "alpaca_reachable":   alpaca_ok,
        "execution_paused":      app_state.get("execution_paused", False),
        "first_exec_failed":     app_state.get("first_exec_failed", False),
        "skipped_tickers":       list(_skipped_tickers.keys()),  # GAP-2: TTL dict, show active keys
        "ticker_fail_counts":    dict(_ticker_fail_counts),  # GAP-CB: consecutive failures per ticker
        "circuit_breaker_threshold": _TICKER_FAIL_THRESHOLD,
        "vix":                   current_vix,
        "vix_brake":          vix_brake_status,
        "vix_brake_elevated": vix_brake_elevated,
        "vix_brake_full":     vix_brake_full,
        "max_positions":      MAX_CONCURRENT_POSITIONS,
        "max_new_per_day":    MAX_NEW_PER_DAY,
        "auto_execute":       app_state.get("auto_execute", False),
        "mode":               app_state.get("mode", "dry_run"),
        # P0-A: Stale deploy detection
        "state_restored":     _STATE_RESTORED,
        "code_hash":          _CODE_HASH,
        "stale_deploy":       (not _os.path.exists("/tmp/nexus_deploy_in_progress")) and _CODE_HASH != _compute_module_hash(),
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

    # ── Block 2: STANDBY gate — reject execution while preflight has not passed ──
    with _state_lock:
        if _SERVICE_MODE == "standby":
            return JSONResponse(
                status_code=503,
                content={"executed": False, "reason": f"Service in STANDBY: {_standby_reason}"},
            )

    # ── GAP-5: Rate Limit Gate ───────────────────────────────────────────────────
    # Max 10 /execute calls per 60s. Blocks brute-force and runaway OMNI loops.
    # GENESIS 2026-04-27.
    if not _execute_rate_limiter.consume():
        logger.warning(
            "Rate limit exceeded on /execute — dropping request (ticker=%s)",
            getattr(body, "ticker", "?"),
        )
        return JSONResponse(
            status_code=429,
            content={"executed": False, "reason": "Rate limit exceeded — max 10 requests per 60s"},
        )

    # ── Normalize Optional fields to safe defaults ────────────────────────────
    # Some callers (Railway webhook omni_scanner, older OMNI versions) may omit
    # fields that are now Optional.  Apply defaults here so all downstream code
    # can assume non-None values.
    _position_size_usd = body.position_size_usd if body.position_size_usd is not None else 1000.0
    _sizing_mult       = body.sizing_mult       if body.sizing_mult       is not None else 0.5
    _weighted_score    = body.weighted_score    if body.weighted_score    is not None else 0.0
    _verdict           = body.verdict           if body.verdict           is not None else "GO"
    _window_id         = body.window_id         if body.window_id         is not None else "unknown"
    _agent_scores      = body.agent_scores      if body.agent_scores      is not None else {}

    # Market hours gate — never submit orders outside 9:30 AM – 4:00 PM ET.
    # Prevents after-hours order submission caused by service restarts, stale
    # queue flushes, or OMNI signals that arrive outside trading hours.
    # _is_market_hours() is used by the exit monitor but was previously absent here.
    if not _is_market_hours():
        logger.warning(
            "Alpha execution rejected: outside market hours — ticker=%s window=%s",
            body.ticker, _window_id,
        )
        return JSONResponse(
            status_code=422,
            content={
                "executed": False,
                "reason":   "Outside market hours (9:30 AM – 4:00 PM ET, weekdays only)",
            },
        )

    # GAP-001/GAP-2: Skip gate — reject tickers in the 30-min skip list
    if _is_ticker_skipped(body.ticker.upper()):
        logger.warning("Ticker %s in session skip list (prior 422) — rejecting", body.ticker)
        return JSONResponse(
            status_code=422,
            content={
                "executed": False,
                "reason":   f"Ticker {body.ticker} skipped for session due to prior invalid payload error",
            },
        )

    # First-failure pause gate — if execution has been paused, reject immediately
    if app_state.get("execution_paused", False):
        logger.warning("Alpha execution PAUSED — rejecting %s signal", body.ticker)
        return JSONResponse(
            status_code=503,
            content={
                "executed": False,
                "reason":   "Execution paused — auto-heal in progress or manual resume required (/resume)",
            },
        )

    # Pipeline Sentinel — execution received this signal
    _ps_trace_id = f"{_window_id}:{body.ticker}"
    try:
        from pipeline_client import trace_hop as _trace_hop
        _trace_hop(_ps_trace_id, "execution_received", "alpha-execution", body.ticker, "alpha")
    except Exception:
        pass

    # Test window guard — test entries must NEVER touch the production position table.
    # Any window_id containing 'test' (case-insensitive) is rejected immediately,
    # before any DB write or Alpaca call. This is the root cause of the 2026-04-24
    # ghost that blocked execution for 6 days.
    if "test" in _window_id.lower():
        logger.warning(
            "Alpha execution BLOCKED: test window_id='%s' rejected for %s — "
            "test entries never create production positions",
            _window_id, body.ticker,
        )
        return JSONResponse(
            status_code=422,
            content={
                "executed": False,
                "reason":   f"window_id='{_window_id}' is a test entry — test windows cannot create production positions",
                "gate":     "test_window_guard",
            },
        )

    # Cipher Finding 7+8: verdict whitelist — last line of defense.
    # OMNI's can_execute() is the first gate. This is the execution bridge gate.
    # Prevents CONDITIONAL or any non-executable verdict from reaching Alpaca
    # via direct API call, future OMNI bug, or misconfiguration.
    if _verdict not in ("GO", "STRONG_GO"):
        logger.warning(
            "Alpha execution rejected: verdict '%s' not in executable whitelist", _verdict
        )
        return JSONResponse(
            status_code=422,
            content={
                "executed": False,
                "reason":   f"Verdict '{_verdict}' is not executable — whitelist: GO, STRONG_GO",
            },
        )

    # ── VIX Brake Gate ────────────────────────────────────────────────────────
    # Check before any DB write or Alpaca call. Non-blocking Axiom probe.
    current_vix = _get_current_vix()
    vix_rejection = _check_vix_brake(current_vix, body.direction)
    if vix_rejection:
        logger.warning("VIX BRAKE FIRED for %s: %s", body.ticker, vix_rejection)
        try:
            from shared.notification_router import notify_warn as _nw
            _nw("alpha-execution", "VIX BRAKE", vix_rejection, ticker=body.ticker)
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
            from shared.notification_router import notify_warn as _nw
            _nw(
                "alpha-execution",
                "SPREAD RESOLUTION FAILED",
                f"Verdict was GO but no tradeable contract found.\nDirection: {body.direction} | Pathway: {body.pathway}\nReason: {str(e)[:200]}",
                ticker=body.ticker,
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

    contracts = max(1, int(_position_size_usd / (current_price * 100)))

    # ── Atomic limit check + PENDING slot reservation (Cipher Finding 1+2 fix) ─
    # Lock guards check + reservation as a single atomic unit.
    # Writing a 'pending' DB record INSIDE the lock closes the TOCTOU race:
    # two concurrent requests both see open_count updated after the first
    # reservation, so only one can pass the limit check.
    pending_id = None
    with _execute_lock:
        # V2 C1 fix: Alpaca live query is combined cap ground truth across all services.
        # Local DB count is secondary. If Alpaca unreachable: fail closed (block execution).
        try:
            from database import count_open_positions_alpaca
            alpaca_live_count = count_open_positions_alpaca(
                settings.alpaca_url, settings.alpaca_api_key, settings.alpaca_secret_key
            )
            if alpaca_live_count >= MAX_CONCURRENT_POSITIONS:
                logger.info(
                    "Alpha execution blocked: Alpaca shows %d/%d live positions (combined cap)",
                    alpaca_live_count, MAX_CONCURRENT_POSITIONS
                )
                return JSONResponse(
                    status_code=429,
                    content={
                        "executed": False,
                        "reason": f"POSITION_CAP: Alpaca shows {alpaca_live_count}/{MAX_CONCURRENT_POSITIONS} open positions (combined across all services)",
                    },
                )
        except RuntimeError as _alpaca_err:
            logger.warning("Alpaca live position check failed: %s — blocking as safety measure", _alpaca_err)
            return JSONResponse(
                status_code=503,
                content={"executed": False, "reason": f"Cannot verify position count: {_alpaca_err}"},
            )

        open_count  = count_open_positions(settings.alpha_db_path)  # includes 'pending'
        today_count = count_new_positions_today(settings.alpha_db_path)

        # Per-ticker duplicate guard (2026-04-24): reject if ticker already open.
        # Concordances fire per-window; a strong ticker can generate new P1 events
        # in subsequent windows. Without this guard, the same ticker gets opened
        # multiple times. Must be inside the lock to be race-condition safe.
        if ticker_already_open(settings.alpha_db_path, body.ticker):
            logger.info(
                "Alpha execution blocked: %s already has an open position", body.ticker
            )
            _report_exec_event(
                "execution_rejected", body.ticker, body.direction,
                reason=f"{body.ticker} already has an open position",
                pathway=body.pathway or "",
                extra={"gate": "duplicate_ticker"},
            )
            return JSONResponse(
                status_code=429,
                content={
                    "executed": False,
                    "reason":   f"{body.ticker} already has an open position — no duplicates allowed",
                    "position_id": None,
                },
            )

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
            position_size_usd = _position_size_usd,
            window_id         = _window_id,
            agent_scores      = _json.dumps(_agent_scores),
            verdict           = _verdict,
        )

    # ── AUTO_EXECUTE Gate — dry-run if kill-switch is off ──────────────────────
    if not app_state.get("auto_execute", False):
        order_preview = {
            "ticker":           body.ticker,
            "direction":        body.direction,
            "verdict":          _verdict,
            "spread_details":   spread.__dict__ if hasattr(spread, "__dict__") else str(spread),
            "current_price":    current_price,
        }
        logger.info("DRY_RUN: would have executed %s %s (verdict=%s)", body.ticker, body.direction, _verdict)
        if pending_id:
            cancel_pending_position(settings.alpha_db_path, pending_id)
        # Send dry-run Telegram alert
        try:
            from shared.notification_router import notify_info as _ni
            _ni("alpha-execution", "[DRY RUN] Would execute",
                f"{body.direction} | verdict={_verdict} | price=${current_price}",
                ticker=body.ticker)
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
        # Pre-submission duplicate-leg guard (FIX 2026-04-27):
        # If contract_resolver snaps both strikes to the same symbol, reject cleanly
        # rather than sending a bad order to Alpaca and getting a 422 back.
        if short_symbol == long_symbol:
            logger.error(
                "Duplicate-leg detected before order submission: "
                "short=%s == long=%s for %s — rejecting order",
                short_symbol, long_symbol, body.ticker,
            )
            return JSONResponse(
                status_code=422,
                content={
                    "executed": False,
                    "reason":   f"Duplicate spread legs: both legs resolved to {short_symbol}. "
                                f"Contract resolver returned identical strikes.",
                },
            )

        legs = [
            {"symbol": short_symbol, "side": "sell", "ratio_qty": 1},
            {"symbol": long_symbol,  "side": "buy",  "ratio_qty": 1},
        ]
        # Use limit orders for spreads — market orders carry unacceptable slippage risk.
        # Credit floor: 1.5% of spot price, minimum $0.50 per contract.
        estimated_credit = max(round(current_price * 0.015, 2), 0.50)

        # Idempotency key — prevents duplicate orders on OMNI retry and enables
        # order recovery after network timeout (Bug 1 fix, 2026-04-24).
        # Format: nexus-{window_id}-{ticker}, truncated to 48 chars.
        _raw_coid = f"nexus-{_window_id}-{body.ticker}"
        _client_order_id = _raw_coid[:48].replace(" ", "-")

        # Block 4: Pre-submission existence check — if this COID already exists on
        # Alpaca (OMNI retry or duplicate signal), recover the existing order instead
        # of submitting a duplicate. Eliminates race-window duplicate fills.
        _existing_order = alpaca.get_order_by_client_id(_client_order_id)
        if _existing_order is not None:
            _existing_status = _existing_order.get("status", "unknown")
            logger.info(
                "Block 4: order already exists for client_order_id=%s (status=%s) — skipping submission",
                _client_order_id, _existing_status,
            )
            order = _existing_order
        else:
            order = alpaca.place_spread_order(
                legs             = legs,
                qty              = contracts,
                order_type       = "limit",
                limit_debit      = estimated_credit,
                client_order_id  = _client_order_id,
            )
            logger.info("Alpha spread order placed: %s | order_id=%s", spread.leg_description(), order.get("id"))
            try:
                from pipeline_client import trace_hop as _trace_hop
                _trace_hop(_ps_trace_id, "alpaca_submitted", "alpha-execution", body.ticker, "alpha")
            except Exception:
                pass
        short_order_id = order.get("id")
        long_order_id  = order.get("id")   # spread is a single multi-leg order
    except Exception as e:
        import requests as _req_mod
        order_error = str(e)[:200]

        # ── Bug 1 Fix: Network-timeout order recovery (2026-04-24) ──────────────────
        # Root cause of stuck_pending_no_order_placed:
        # requests.post() with timeout=10 raises Timeout if Alpaca accepts the order
        # but the response takes >10s (common on paper API). The order IS live on
        # Alpaca but we catch the exception, cancel the pending slot, and return 503.
        # That leaves an untracked live Alpaca order.
        #
        # Fix: on Timeout or ConnectionError, look up the order by client_order_id
        # before declaring failure. If found, treat as successfully placed.
        # Non-network exceptions (auth errors, bad request) skip recovery and fail fast.
        _is_network_error = isinstance(
            e, (_req_mod.exceptions.Timeout, _req_mod.exceptions.ConnectionError)
        )
        if _is_network_error and _client_order_id:
            logger.warning(
                "Alpaca order network error for %s (%s) — "
                "attempting recovery via client_order_id=%s",
                body.ticker, type(e).__name__, _client_order_id,
            )
            try:
                _recovered = alpaca._get_order_by_client_id(_client_order_id)
                if _recovered and _recovered.get("id"):
                    short_order_id = _recovered["id"]
                    long_order_id  = _recovered["id"]
                    logger.warning(
                        "Order RECOVERED after network error for %s: order_id=%s status=%s",
                        body.ticker, short_order_id[:8], _recovered.get("status"),
                    )
                    _report_exec_event(
                        "execution_error", body.ticker, body.direction,
                        reason=f"Network error on order submit but order found via client_order_id — recovered: {short_order_id[:8]}",
                        pathway=body.pathway or "",
                        extra={"gate": "order_recovery", "original_error": order_error},
                    )
                    # Jump past the error-handling block — order_error stays set but
                    # short_order_id is now valid. The reconciler will confirm fill status.
                    order_error = None
                else:
                    logger.warning(
                        "Order recovery failed for %s — no order found for client_order_id=%s",
                        body.ticker, _client_order_id,
                    )
            except Exception as _rec_err:
                logger.warning("Order recovery lookup failed for %s: %s", body.ticker, _rec_err)

        # If recovery succeeded, skip the error block (order_error cleared above)
        if order_error is None:
            pass  # recovered — fall through to fill confirmation below
        else:
            # Genuine failure — cancel pending slot and handle
            if pending_id:
                cancel_pending_position(settings.alpha_db_path, pending_id)

            _report_exec_event(
                "execution_error", body.ticker, body.direction,
                reason=f"Alpaca order placement failed: {order_error}",
                pathway=body.pathway or "",
                extra={"error": order_error, "gate": "alpaca_order"},
            )

            # ── Bug 3 Fix: Selective pause — only fatal errors halt execution (2026-04-24)
            # Root cause: every Alpaca exception (including transient Timeout) triggered
            # first_exec_failed=True → execution_paused=True, halting all trading for
            # the rest of the day. A 10s network blip took down the entire system.
            #
            # Fix: only pause on errors that indicate a systemic/fatal condition:
            #   - AlpacaError 401/403 → auth failure, always fatal
            #   - AlpacaError 403 account blocked → always fatal
            # Transient errors (Timeout, ConnectionError, 5xx) → log, alert, continue.
            # ── GAP-001: Auto-Heal Execution Recovery Loop ─────────────────
            # Replace manual pause-and-alert with autonomous diagnosis + recovery.
            # Classifies error into 6 known classes, attempts class-specific fix,
            # auto-resumes when possible, notifies SOVEREIGN always, pages Ahmed
            # only if the system cannot self-heal.
            from alpaca_client import AlpacaError as _AlpacaError
            from execution_healer import auto_heal_execution, classify_error, ExecutionErrorClass

            error_class = classify_error(e)

            # INVALID_PAYLOAD (422): never pause — skip ticker and continue immediately
            # FIX (2026-04-27): /execute is a sync endpoint — asyncio.create_task() is
            # unavailable here and raises NameError in a sync context.  Use
            # threading.Thread to launch the async healer in its own event loop instead.
            def _run_healer_sync(exc, ticker, app_state, state_lock, api_key, api_secret, skipped_tickers):
                import asyncio as _asyncio
                _asyncio.run(
                    auto_heal_execution(
                        exc=exc,
                        ticker=ticker,
                        app_state=app_state,
                        state_lock=state_lock,
                        api_key=api_key,
                        api_secret=api_secret,
                        skipped_tickers=skipped_tickers,
                    )
                )

            if error_class == ExecutionErrorClass.INVALID_PAYLOAD:
                if not _is_ticker_skipped(body.ticker.upper()):
                    threading.Thread(
                        target=_run_healer_sync,
                        args=(e, body.ticker, app_state, _state_lock,
                              settings.alpaca_api_key, settings.alpaca_secret_key,
                              _skipped_tickers),
                        daemon=True,
                    ).start()
            else:
                # All other errors: set paused flag first, then spawn healer
                with _state_lock:
                    if not app_state.get("first_exec_failed", False):
                        app_state["first_exec_failed"] = True
                        app_state["execution_paused"]  = True
                        _svc_state.write("execution_paused", True)  # GAP-002
                        logger.critical(
                            "EXECUTION ERROR — pausing and spawning auto-healer. "
                            "Ticker=%s class=%s error=%s",
                            body.ticker,
                            error_class.value,
                            order_error,
                        )
                        report("alpha_execution", "alert", {
                            "event":       "execution_paused_auto_heal",
                            "ticker":      body.ticker,
                            "error":       order_error,
                            "error_class": error_class.value,
                        }, escalation=EscalationLevel.CRITICAL)
                        threading.Thread(
                            target=_run_healer_sync,
                            args=(e, body.ticker, app_state, _state_lock,
                                  settings.alpaca_api_key, settings.alpaca_secret_key,
                                  _skipped_tickers),
                            daemon=True,
                        ).start()

            # GAP-CB: Circuit breaker — track consecutive failures per ticker
            _ticker = body.ticker.upper()
            with _state_lock:
                _ticker_fail_counts[_ticker] = _ticker_fail_counts.get(_ticker, 0) + 1
                fail_count = _ticker_fail_counts[_ticker]
                _svc_state.write("ticker_fail_counts", _ticker_fail_counts)  # GAP-002
                if fail_count >= _TICKER_FAIL_THRESHOLD:
                    _skipped_tickers.add(_ticker)
                    _svc_state.write("skipped_tickers", list(_skipped_tickers))  # GAP-002
                    logger.critical(
                        "CIRCUIT BREAKER TRIPPED: %s failed execution %dx — "
                        "auto-added to session skip list",
                        _ticker, fail_count,
                    )
                    from shared.sovereign_comms import report as _cb_report, EscalationLevel as _EL
                    _cb_report("alpha_execution", "alert", {
                        "event":      "circuit_breaker_tripped",
                        "ticker":     _ticker,
                        "fail_count": fail_count,
                        "action":     "ticker_skipped_for_session",
                    }, escalation=_EL.CRITICAL)
                else:
                    logger.warning(
                        "CIRCUIT BREAKER: %s execution fail %d/%d",
                        _ticker, fail_count, _TICKER_FAIL_THRESHOLD,
                    )

            return JSONResponse(
                status_code=503,
                content={
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
        # FIX (2026-04-27): was "!= true" (opt-in) — reconciler was always in dry_run.
        # Now "== skip" (opt-out): reconciliation is ON by default, disabled only explicitly.
        dry_run            = os.getenv("RECONCILE_ORDERS", "").lower() == "skip",
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
            from shared.notification_router import notify_warn as _nw
            _nw("alpha-execution", "VOID trade", _fill.void_reason, ticker=body.ticker)
        except Exception:
            pass
        return JSONResponse(
            status_code=503,
            content={"status": "void", "reason": _fill.void_reason, "executed": False},
        )

    # ── Alpaca confirmed — promote pending slot to open position ──────────────
    # Cipher Finding 1+2 fix: position_id was reserved inside the lock as 'pending'.
    # confirm_pending_position() sets status='open' + Alpaca fields. No new INSERT.
    #
    # Bug 2 Fix (2026-04-24): entry_price stores the spread's net premium from
    # Alpaca (fill_price), NOT the underlying stock price.
    # Root cause: `current_price` is the stock price (~$198 for NVDA). Storing
    # that as entry_price makes every P&L calculation that uses entry_price wrong.
    # The spread's fill_price from Alpaca is the net debit/credit per contract
    # (e.g. $0.04 net debit, $-0.16 net credit). This is the correct cost basis
    # for the spread position. If fill_price is None (dry-run or reconciler
    # unavailable), store 0.0 — never store the underlying price.
    # Note: the exit monitor's _get_current_pnl() uses Alpaca unrealized_pl
    # directly — it does NOT use entry_price for P&L math. entry_price is
    # for audit/display only. Storing the wrong value doesn't affect exits
    # but causes confusion in DB inspection and reporting.
    _confirmed_price = _fill.fill_price if _fill.fill_price is not None else 0.0
    # GAP-CB: Reset circuit breaker on successful execution
    with _state_lock:
        _ticker_fail_counts.pop(body.ticker.upper(), None)

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

    # ── Fix A: Post-confirmation position verification (2026-04-24) ──────────────
    # Root cause of phantom_no_fill_alpaca_order_not_found:
    # Paper options mleg spreads occasionally fill at zero net premium (credit
    # equals debit exactly), or Alpaca paper records the position then immediately
    # closes it at $0. In these cases the DB shows open but Alpaca has zero
    # position. Detect this at confirm time: query the order's leg positions.
    # If all leg positions are zero on Alpaca, close the DB position immediately
    # rather than leaving a phantom open that blocks all future verdicts.
    #
    # Failure-safe: if Alpaca is unreachable or the check throws, the position
    # stays open (don't close blindly on network error).
    try:
        _alpaca_check = _get_alpaca()
        _short_pos = _alpaca_check.get_position(short_symbol) if short_symbol else None
        _long_pos  = _alpaca_check.get_position(long_symbol)  if long_symbol  else None
        _short_qty = abs(float(_short_pos.get("qty", 0))) if _short_pos else 0.0
        _long_qty  = abs(float(_long_pos.get("qty", 0)))  if _long_pos  else 0.0
        if _short_qty == 0.0 and _long_qty == 0.0:
            # Both legs are zero on Alpaca immediately after fill confirmation.
            # This is the phantom scenario. Close the DB position now.
            from database import close_position as _close_pos
            _close_pos(
                db_path      = settings.alpha_db_path,
                position_id  = position_id,
                close_reason = "alpaca_zero_position_post_fill",
                pnl_pct      = 0.0,
                pnl_usd      = 0.0,
            )
            logger.warning(
                "Fix A: %s spread confirmed fill but zero Alpaca positions on both legs — "
                "phantom detected, DB position #%d closed immediately",
                body.ticker, position_id,
            )
            _report_exec_event(
                "execution_error", body.ticker, body.direction,
                reason="Phantom: order filled but both option legs have zero Alpaca position immediately after confirm",
                pathway=body.pathway or "",
                extra={"gate": "post_confirm_position_check", "position_id": position_id},
            )
            try:
                from shared.notification_router import notify_escalate as _ne
                _ne("alpha-execution",
                    "PHANTOM POSITION DETECTED",
                    f"Fill confirmed but zero Alpaca legs. Position #{position_id} auto-closed.",
                    ticker=body.ticker)
            except Exception:
                pass
            # Return 503 — the order technically filled but left no position
            return JSONResponse(
                status_code=503,
                content={
                    "executed":   False,
                    "reason":     "Phantom: spread filled but zero option positions on Alpaca",
                    "position_id": position_id,
                    "gate":        "post_confirm_position_check",
                },
            )
        else:
            logger.info(
                "Fix A: Post-confirm check OK for %s — short_qty=%.0f long_qty=%.0f",
                body.ticker, _short_qty, _long_qty,
            )
    except Exception as _pc_err:
        # Never block trade confirmation on a position check failure.
        # Log and continue — the exit monitor will catch real discrepancies later.
        logger.warning(
            "Fix A: Post-confirm position check failed for %s (non-fatal): %s",
            body.ticker, _pc_err,
        )

    # Update in-memory counters (DB is the authoritative source — these are display only)
    # OMNI M-NEW-3 fix: protect app_state counter increments with _state_lock.
    # Prime Execution received this fix; Alpha was missed. Race condition on
    # display-only counters — DB is authoritative, but inconsistency must be resolved.
    with _state_lock:
        app_state["trades_today"] += 1
        app_state["total_trades"] += 1
        _svc_state.write("trades_today", app_state["trades_today"])  # GAP-002

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
        "verdict":     _verdict,
        "order_id":    short_order_id or "",
        "position_id": position_id,
        "contracts":   contracts,
        "size_usd":    _position_size_usd,
    }, escalation=EscalationLevel.CRITICAL)

    # ── Telegram Notification ────────────────────────────────────────────────
    _send_entry_notification(
        settings.telegram_bot_token, settings.ahmed_chat_id,
        body, spread, position_id, contracts, current_price, short_order_id,
        position_size_usd=_position_size_usd,
        weighted_score=_weighted_score,
    )

    # Discord mirror — fill confirmed to #execution-monitoring
    try:
        import sys as _sys
        _sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
        from discord_client import post_to_discord as _disc
        _disc(
            channel     = "execution-monitoring",
            title       = f"⚡ ALPHA FILL — {body.ticker}",
            description = f"{body.direction.upper()} spread executed. {contracts} contract(s) @ ${_position_size_usd:,.0f}.",
            color       = "teal",
            fields      = [
                {"name": "Ticker",    "value": body.ticker,                   "inline": True},
                {"name": "Direction", "value": body.direction.upper(),        "inline": True},
                {"name": "Pathway",   "value": _verdict,                      "inline": True},
                {"name": "Size",      "value": f"${_position_size_usd:,.0f}", "inline": True},
                {"name": "Position",  "value": f"#{position_id}",             "inline": True},
            ],
            footer      = "Alpha Execution",
            agent       = "alpha-execution",
        )
    except Exception as _disc_exc:
        logger.warning(f"Discord push failed (non-fatal): {_disc_exc}")

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
        "size_usd":      _position_size_usd,
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




# ── GAP-006: /log_tail — Runtime log access (2026-04-27) ─────────────────────
# Gives OMNI, integrity checker, and SOVEREIGN visibility into recent logs
# without requiring SSH or Railway dashboard access. Authenticated.
@app.get("/log_tail")
def log_tail(
    lines:          int = 50,
    filter_str:     str = "",
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Return the last N lines of the service stderr log.
    Optional filter_str narrows to lines containing that string.
    Max 200 lines per call to avoid abuse.
    """
    verify_secret(x_nexus_secret)
    import os as _os, pathlib as _pl
    lines = min(lines, 200)
    log_path = _os.getenv("STDERR_LOG_PATH", "")
    if not log_path or not _pl.Path(log_path).exists():
        candidates = [
            "/Users/ahmedsadek/nexus/logs/alpha-execution/stderr.log",
            "/Users/ahmedsadek/nexus/logs/prime-execution/stderr.log",
            "/Users/ahmedsadek/nexus/logs/alpha-execution/stderr.log",
        ]
        for c in candidates:
            if _pl.Path(c).exists():
                log_path = c
                break
    if not log_path or not _pl.Path(log_path).exists():
        return JSONResponse({"ok": False, "error": "log file not found"})
    try:
        with open(log_path, "r", errors="replace") as _f:
            all_lines = _f.readlines()
        matched = [l for l in all_lines if filter_str.lower() in l.lower()] if filter_str else all_lines
        tail = matched[-lines:]
        return JSONResponse({
            "ok": True, "log_path": log_path,
            "lines": len(tail), "filter": filter_str or None,
            "content": "".join(tail),
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/admin/reconciler-status", status_code=200)
def admin_reconciler_status(
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Returns reconciler state for the Nexus dashboard.

    Dashboard checks: recon_ok = data is not None and not data.get('execution_paused').
    This endpoint provides that signal directly from live app_state.
    """
    verify_secret(x_nexus_secret)
    with _state_lock:
        paused       = app_state.get("execution_paused", False)
        alpaca_ok    = app_state.get("alpaca_reachable", False)
        trades_today = app_state.get("trades_today", 0)
        first_failed = app_state.get("first_exec_failed", False)

    reconciler_healthy = alpaca_ok and not paused and not first_failed

    return JSONResponse({
        "status":            "healthy" if reconciler_healthy else "degraded",
        "execution_paused":  paused,
        "alpaca_reachable":  alpaca_ok,
        "first_exec_failed": first_failed,
        "trades_today":      trades_today,
        "service":           "alpha-execution",
    })


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
    position_size_usd: float = 0.0,
    weighted_score: float = 0.0,
) -> None:
    """Send trade opened notification via router (INFO — SOVEREIGN + Discord only)."""
    direction_emoji = "📈" if body.direction == "bullish" else "📉"
    try:
        from shared.notification_router import notify_info as _ni
        _ni(
            "alpha-execution",
            f"{direction_emoji} ALPHA TRADE OPENED — {body.ticker}",
            (
                f"Direction: {body.direction.upper()}\n"
                f"Spread: {spread.leg_description()}\n"
                f"Contracts: {contracts} | Size: ${position_size_usd:,.0f}\n"
                f"Pathway: {body.pathway} | Score: {weighted_score:.1f}\n"
                f"Position #{position_id}"
            ),
            ticker=body.ticker,
        )
    except Exception:
        pass
