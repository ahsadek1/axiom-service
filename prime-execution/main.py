"""
main.py — Prime Execution Engine FastAPI Entry Point

Receives GO signals from OMNI. Places swing equity orders via Alpaca.
Monitors exits and reconciles positions every 30 minutes.

Auth: X-Nexus-Prime-Secret header → NEXUS_PRIME_SECRET.
Port: 8006 (local), $PORT (Railway).
"""

import logging
import os
import re
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
from config import ALPACA_PAPER_URL
from buffer_reporter import BufferReporter
from config import MAX_CONCURRENT_POSITIONS, MAX_NEW_PER_DAY, load_settings
import sys as _sys_p
import os.path as _osp
_sys_p.path.insert(0, _osp.join(_osp.dirname(_osp.abspath(__file__)), ".."))
from shared.service_state import ServiceStateWriter as _ServiceStateWriter
from shared.watchdog import Watchdog
from shared.resilience.state import FreshValue as _FreshValue
from datetime import timedelta as _timedelta

# Capital Router Gate (OMNI May 20, 2026)
import sys as _sys_capital_prime
_sys_capital_prime.path.insert(0, '/Users/ahmedsadek/.openclaw/workspace-omni')
try:
    from capital_gate_logic import CapitalGate
    CAPITAL_GATE_AVAILABLE = True
except ImportError:
    CAPITAL_GATE_AVAILABLE = False

# B3 — VIX cache with explicit timestamp TTL (5-min)
# Bug 1/Prime fix: FreshValue.get()==None is ambiguous (stale vs valid None from Axiom).
# Use explicit fetch timestamp so None values are cached correctly.
_prime_vix_last_fetch: float = 0.0
_prime_vix_last_value: float = 0.0
_prime_vix_lock = threading.Lock()
_PRIME_VIX_TTL = 300  # 5 minutes


def _get_prime_vix_cached(axiom_url: str, axiom_secret: str) -> float:
    """
    Return cached VIX for Prime gate check (5-min TTL).
    Falls back to live Axiom fetch on cache miss.
    Returns 0.0 (pass-through) on any error — existing fail-open for Prime preserved.
    Bug fix: uses timestamp-based TTL so VIX=None from Axiom is cached correctly
    and does not cause infinite re-fetching every /execute call.
    """
    global _prime_vix_last_fetch, _prime_vix_last_value
    import time as _t_pvix
    now = _t_pvix.time()

    with _prime_vix_lock:
        if _prime_vix_last_fetch > 0 and (now - _prime_vix_last_fetch) < _PRIME_VIX_TTL:
            return _prime_vix_last_value  # Cache hit — even if value is 0.0

    # Cache miss — fetch live
    try:
        import requests as _r
        resp = _r.get(f"{axiom_url}/health",
                      headers={"X-Axiom-Secret": axiom_secret}, timeout=4)
        vix = float(resp.json().get("vix", 0) or 0)
        with _prime_vix_lock:
            _prime_vix_last_fetch = _t_pvix.time()
            _prime_vix_last_value = vix
        return vix
    except Exception as _e:
        with _prime_vix_lock:
            _prime_vix_last_fetch = _t_pvix.time()  # Cache the failure too — don't hammer Axiom
            _prime_vix_last_value = 0.0
        return 0.0  # fail-open: Prime VIX gate is advisory
_svc_state = _ServiceStateWriter("prime-execution")
from database import (
    cancel_pending_position,
    close_stale_position,
    confirm_pending_position,
    count_new_positions_today,
    count_open_positions,
    flag_technical_stop,
    get_open_positions,
    get_position_by_id,
    init_db,
    reserve_position_slot,
    ticker_already_open,
    # save_position: replaced by reserve+confirm PENDING pattern (Cipher F1+F2 / OMNI H4)
)
from exit_monitor import evaluate_exits
from reconciler import run_reconciler

# P0-A: Stale deploy detection
import hashlib as _hashlib
import os as _os
import glob as _glob

# G7 NOTE (2026-05-03): canonical implementation lives in shared/module_hash.py.
# Local copy retained until sys.path insert is moved above this block.
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

# P3 fix: cache module hash with 60s TTL — avoids rehashing all .py files on every /health call
import time as _time_p
_cached_module_hash_p: str = _CODE_HASH
_cached_module_hash_ts_p: float = _time_p.monotonic()
_module_hash_lock_p = threading.Lock()


def _get_cached_module_hash_p() -> str:
    """Return module hash, recomputing at most once per 60 seconds."""
    global _cached_module_hash_p, _cached_module_hash_ts_p
    with _module_hash_lock_p:
        if _time_p.monotonic() - _cached_module_hash_ts_p > 60:
            _cached_module_hash_p = _compute_module_hash()
            _cached_module_hash_ts_p = _time_p.monotonic()
    return _cached_module_hash_p


# P1 fix: per-ticker lock (GAP-6 equivalent for Prime)
# Prevents exit_monitor + /execute race on same position
_ticker_locks_p: dict = {}
_ticker_locks_p_mutex = threading.Lock()


def _get_ticker_lock_p(ticker: str) -> threading.Lock:
    """Get (or lazily create) a per-ticker threading.Lock for Prime. Thread-safe."""
    with _ticker_locks_p_mutex:
        if ticker not in _ticker_locks_p:
            _ticker_locks_p[ticker] = threading.Lock()
        return _ticker_locks_p[ticker]

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("prime_exec.main")

ET       = pytz.timezone("America/New_York")
settings = load_settings()

app_state: dict = {
    "settings":                    settings,
    "svc_state":                   _svc_state,   # Bug 4 fix: expose for reconciler persistence
    "start_time":                  datetime.now(ET).isoformat(),
    "trades_today":                0,
    "execution_paused":            False,
    "last_reconcile_at":           None,
    "last_reconcile_mismatches":   0,
    "was_paused_for_reconcile":    False,
    "reconcile_paused_at":          None,   # P2 fix: timestamp when reconciler triggered pause (auto-clear after 4h)
    "auto_execute":  os.getenv("NEXUS_AUTO_EXECUTE", "false").lower() == "true",
    "mode":          "live" if os.getenv("NEXUS_AUTO_EXECUTE", "false").lower() == "true" else "dry_run",
    # Exit monitor state (S10 fix)
    "exit_monitor_last_run_at":      None,
    "exit_monitor_last_event_count": 0,
    "exit_monitor_last_error":       None,
    "exit_monitor_run_count":        0,
}
if not app_state["auto_execute"]:
    logger.warning("NEXUS_AUTO_EXECUTE=false — running in DRY_RUN mode. No Alpaca orders will be placed.")
else:
    logger.warning("NEXUS_AUTO_EXECUTE=true — LIVE TRADING MODE. Orders will be submitted to Alpaca.")

# GAP-002: Restore durable state from prior session (execution_paused only — Block 3 owns trades_today)
_prior_state_p = _svc_state.restore()
_STATE_RESTORED_P = bool(_prior_state_p)
if _prior_state_p.get("execution_paused"):        app_state["execution_paused"] = True
if _prior_state_p.get("was_paused_for_reconcile"): app_state["was_paused_for_reconcile"] = True
# Bug 4 fix: restore reconcile_paused_at from service_state so auto-clear survives restarts
if _prior_state_p.get("reconcile_paused_at"):
    try:
        app_state["reconcile_paused_at"] = float(_prior_state_p["reconcile_paused_at"])
    except (ValueError, TypeError):
        app_state["reconcile_paused_at"] = None
# Block 3: trades_today is derived from DB — do NOT seed from prior state (DB is truth)

# ── Block 2: Service mode ─────────────────────────────────────────────────────
_SERVICE_MODE: str = "active"   # "active" | "standby"
_standby_reason: str = ""
_standby_alerted: bool = False

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


# ── Block 2: Preflight gate ────────────────────────────────────────────────────────

def _preflight_check() -> tuple[bool, str]:
    """Block 2: Verify startup preconditions before accepting traffic.

    Checks:
      1. Alpaca reachable — GET /v2/account returns 200 with status ACTIVE.
      2. DB writable — test INSERT + ROLLBACK on prime DB.
      3. Market hours determination unambiguous (pytz loaded, ET confirmed).

    Returns:
        (ok: bool, reason: str) — reason is empty when ok=True.
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
        _conn = _sq.connect(settings.prime_db_path, timeout=5)
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
            return False, "pytz ET timezone returned naive datetime"
    except Exception as exc:
        return False, f"pytz timezone check failed: {exc}"

    return True, ""


def _run_preflight_retry_loop() -> None:
    """Block 2: Background thread — retry preflight every 30s until ACTIVE.

    Transitions _SERVICE_MODE from 'standby' to 'active' when all checks pass.
    Alerts Ahmed once on exit from standby.
    """
    global _SERVICE_MODE, _standby_reason, _standby_alerted
    import time as _time
    while True:
        _time.sleep(30)
        with _state_lock:
            if _SERVICE_MODE == "active":
                return
        ok, reason = _preflight_check()
        if ok:
            with _state_lock:
                _SERVICE_MODE = "active"
                _standby_reason = ""
            logger.info("Block 2: Preflight PASSED — transitioning to ACTIVE mode")
            try:
                from shared.notification_router import notify_info as _ni
                _ni("prime-execution", "PREFLIGHT PASSED — ACTIVE",
                    "All startup checks passed. Prime Execution is now ACTIVE.")
            except Exception:
                pass
            return


def _run_startup_reconciliation() -> None:
    """
    Startup reconciliation: close any DB-open positions absent from Alpaca.

    Logic:
      1. Fetch all DB positions with status IN ('open', 'pending').
      2. Fetch all live positions from Alpaca (best-effort).
      3. Build Alpaca ticker set.
      4. For each DB position:
         a. If window_id contains 'test' (case-insensitive) → always close;
            test entries must never survive in production position tracking.
         b. If ticker not in Alpaca live positions → close with reconcile reason.
      5. Log and report to SOVEREIGN for each closed position.

    Non-blocking: if Alpaca is unreachable, logs a warning and
    skips the live-mismatch check. Test-window cleanup still runs.

    GENESIS 2026-04-27: ported from alpha-execution startup reconciliation.
    Closes the GAP-3 dangling position root cause permanently.
    """
    try:
        db_positions = get_open_positions(settings.prime_db_path)

        # Also include pending slots left by a previous crash
        from database import get_conn as _get_conn
        with _get_conn(settings.prime_db_path) as _conn:
            _pending_rows = _conn.execute(
                "SELECT * FROM positions WHERE status='pending'"
            ).fetchall()
        pending_positions = [dict(r) for r in _pending_rows]
        all_db_positions  = db_positions + pending_positions

        if not all_db_positions:
            logger.info("Startup reconciliation: no open/pending positions in DB — nothing to check")
            return

        # Fetch live Alpaca tickers — best effort
        alpaca_tickers = None  # None = unreachable; set() = reachable but empty
        try:
            alpaca         = _get_alpaca()
            live           = alpaca.get_positions()
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

        closed_ids: list = []
        for pos in all_db_positions:
            pos_id = pos["id"]
            ticker = (pos.get("ticker") or "").upper()
            window = (pos.get("window_id") or "")

            # Rule A: test-window entries are NEVER production positions
            if "test" in window.lower():
                reason = f"reconciled_test_window: window_id='{window}' is a test entry"
                close_stale_position(settings.prime_db_path, pos_id, reason)
                closed_ids.append(f"#{pos_id} {ticker} (test window)")
                logger.warning(
                    "Startup reconciliation: closed test entry #%d %s window=%s",
                    pos_id, ticker, window,
                )
                continue

            # Rule B: DB position not in Alpaca live set → potential phantom
            # GAP-004 Option C: Order-centric verification before closing.
            # Before declaring this a phantom, verify via the ORDER that placed it.
            # If the order says "filled", the position is REAL — Alpaca's /positions
            # API just hasn't propagated yet (up to 2s lag on equities).
            # Only close if the order itself confirms no fill, or order not found.
            if alpaca_tickers is not None and ticker not in alpaca_tickers:
                order_id = (pos.get("alpaca_order_id") or "").strip()
                order_confirms_fill = False

                if order_id:
                    try:
                        alpaca_order = alpaca.get_order(order_id)
                        if alpaca_order:
                            order_status = alpaca_order.get("status", "")
                            filled_qty   = float(alpaca_order.get("filled_qty") or 0)
                            if order_status == "filled" and filled_qty > 0:
                                order_confirms_fill = True
                                logger.info(
                                    "GAP-004: %s (pos #%d) not in Alpaca /positions "
                                    "but order %s is FILLED (qty=%.1f) — position is REAL, "
                                    "skipping phantom close (Alpaca position API lag)",
                                    ticker, pos_id, order_id[:8], filled_qty,
                                )
                    except Exception as _oc_err:
                        logger.debug(
                            "GAP-004 order check failed for %s: %s — defaulting to position check",
                            ticker, _oc_err,
                        )

                if order_confirms_fill:
                    # Position is real — order says filled. Don't close it.
                    # It will appear in /positions on next reconciler cycle.
                    continue

                # Order not found, not filled, or no order_id — genuine phantom
                reason = f"reconciled_missing_from_alpaca: ticker={ticker} not in live positions"
                close_stale_position(settings.prime_db_path, pos_id, reason)
                closed_ids.append(f"#{pos_id} {ticker} (not in Alpaca)")
                logger.warning(
                    "Startup reconciliation: closed leaked position #%d %s — "
                    "not in Alpaca live set and order does not confirm fill",
                    pos_id, ticker,
                )

        if closed_ids:
            from shared.sovereign_comms import report
            report("prime_execution", "WARN", {
                "event":       "startup_reconciliation",
                "closed_count": len(closed_ids),
                "closed":       closed_ids,
                "action":      "Stale/test positions cleaned. Execution unblocked.",
            })
            logger.warning(
                "Startup reconciliation: closed %d stale position(s): %s",
                len(closed_ids), ", ".join(closed_ids),
            )
        else:
            logger.info("Startup reconciliation: all DB positions confirmed in Alpaca — clean")

        # ── 2026-04-28 FIX: Reverse orphan check ────────────────────────────────────────
        # Alpaca may hold positions that have NO DB record (e.g. opened during a
        # restart window where execution succeeded but DB write failed, or a
        # previous reconciler incorrectly removed the DB entry while the Alpaca
        # position survived).
        # These orphaned Alpaca positions have NO exit monitoring — they are
        # invisible to the exit monitor and will never be closed by Prime.
        # Action: log critical alert + report to SOVEREIGN for each orphan.
        # We do NOT auto-close them — only SOVEREIGN / Ahmed can decide exit.
        # ───────────────────────────────────────────────────────────────────────────────
        if alpaca_tickers is not None:
            db_tickers = {(pos.get("ticker") or "").upper() for pos in all_db_positions}
            try:
                alpaca         = _get_alpaca()
                live_full      = alpaca.get_positions()
                alpaca_orphans = [
                    p for p in live_full
                    if str(p.get("symbol", "")).upper() not in db_tickers
                ]
                if alpaca_orphans:
                    orphan_summaries = []
                    for op in alpaca_orphans:
                        sym   = str(op.get("symbol", "")).upper()
                        qty   = op.get("qty", "?")
                        entry = op.get("avg_entry_price", "?")
                        pl    = op.get("unrealized_pl", "?")
                        orphan_summaries.append(f"{sym} qty={qty} entry=${entry} pnl=${pl}")
                        logger.critical(
                            "REVERSE ORPHAN: Alpaca holds %s qty=%s entry=$%s pnl=$%s "
                            "but NO DB record exists — exit monitoring BLIND.",
                            sym, qty, entry, pl,
                        )
                    try:
                        from shared.sovereign_comms import report as _sov_report
                        _sov_report("prime_execution", "CRITICAL", {
                            "event":   "reverse_orphan_detected",
                            "orphans": orphan_summaries,
                            "action":  "Alpaca positions exist with no DB record. "
                                       "Exit monitoring is blind. Manual review required.",
                        })
                    except Exception as _re:
                        logger.warning("Failed to notify SOVEREIGN of reverse orphan: %s", _re)
                else:
                    logger.info("Startup reconciliation: no reverse orphans — all Alpaca positions tracked in DB.")
            except Exception as _oe:
                logger.warning("Reverse orphan check failed (non-fatal): %s", _oe)
        # ───────────────────────────────────────────────────────────────────────────────

    except Exception as e:
        # Never block startup
        logger.error("Startup reconciliation failed (non-fatal): %s", e)


def _get_reporter() -> BufferReporter:
    return BufferReporter(settings.prime_buffer_url, settings.nexus_prime_secret)


def _is_market_hours() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return now.replace(hour=9, minute=30, second=0, microsecond=0) <= now <= \
           now.replace(hour=16, minute=0, second=0, microsecond=0)


_nns_watchdog = Watchdog("prime-execution")

@asynccontextmanager
async def lifespan(app: "FastAPI") -> AsyncGenerator[None, None]:
    """Initialize DB and start exit monitor + reconciler scheduler."""
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
    from shared.db_guard import assert_unique_db_path  # S4: collision guard
    assert_unique_db_path("prime-execution", settings.prime_db_path)
    logger.info("Prime Execution starting...")
    # G9 SYS-2: Auth registry validation
    from shared.auth_registry import validate_service_auth_config, AuthConfigError
    try:
        validate_service_auth_config("prime-execution")
        logger.info("Auth registry validation passed for prime-execution")
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
    init_db(settings.prime_db_path)

    # ── Block 2: Startup Preflight Gate ────────────────────────────────────────────────
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
            from shared.resilience.alerts import alert_ahmed as _aa
            _aa(
                f"Prime Execution entered STANDBY.\nReason: {_pf_reason}\nRetrying every 30s.",
                key="prime-exec-standby",
                severity="CRITICAL",
            )
        except Exception:
            pass
        import threading as _th
        _th.Thread(
            target=_run_preflight_retry_loop, daemon=True,
            name="prime-exec-preflight-retry"
        ).start()
    else:
        logger.info("Block 2: Preflight PASSED — service starting in ACTIVE mode")
    # ───────────────────────────────────────────────────────────────────

    # GAP-3 fix: Startup reconciliation — close any DB-open positions absent from Alpaca.
    # Prevents dangling 'open'/'pending' positions from blocking all future GO verdicts.
    # Runs once synchronously before scheduler starts. Safe — non-blocking on Alpaca failure.
    # GENESIS 2026-04-27.
    _run_startup_reconciliation()

    # OMNI H-NEW-1 fix: start AILS outcome retry worker
    from ails_reporter import start_retry_worker as _start_ails_retry
    _start_ails_retry()

    scheduler = BackgroundScheduler(timezone=ET)

    # Exit monitor every 1 min during market hours (upgraded 2026-04-28 — commercial grade)
    # Original: 15-min (CRITICAL per o3-mini Pass A). Fixed to 5-min 2026-04-12.
    # Upgraded to 1-min 2026-04-28 to match alpha-execution standard.
    # Rationale: swing equity can gap -18% in a single candle; 5-min = up to -25% before stop fires.
    # 1-minute eliminates that gap entirely. Approved: Ahmed Sadek.
    scheduler.add_job(
        func    = lambda: _run_exit_monitor(),
        trigger = CronTrigger(hour="9-15", minute="*", timezone=ET),
        id      = "prime_exit_monitor",
        replace_existing=True,
    )

    # Reconciler every 4 hours during market hours — swing trades need time to develop.
    # 30-min was forcing premature exits on multi-hour swing positions (OMNI gap 7 fix).
    # Approved: Ahmed Sadek 2026-05-02. Next review: after first clean trading week.
    scheduler.add_job(
        func    = lambda: _run_reconciler(),
        trigger = CronTrigger(hour="9,13", minute="30", timezone=ET),
        id      = "prime_reconciler",
        replace_existing=True,
    )

    scheduler.start()
    app_state["scheduler"] = scheduler
    logger.info("Prime Execution ready")
    from shared.sovereign_comms import get_instructions, report
    report("prime_execution", "INFO", {"event": "started"})
    _instr = get_instructions("prime_execution")
    if _instr:
        logger.info("Prime Execution: %d instruction(s) from SOVEREIGN on startup", len(_instr))

    # ── TCA Tracker schema init (Institutional Gap 3) ────────────────────────
    try:
        from tca_tracker import init_tca_schema
        init_tca_schema(settings.prime_db_path)
        logger.info("[TCA] Schema ready")
    except Exception as _tca_err:
        logger.warning("[TCA] Schema init failed (non-fatal): %s", _tca_err)
    _nns_watchdog.start()

    yield
    scheduler.shutdown(wait=False)
    logger.info("Prime Execution stopped")


def _run_exit_monitor() -> None:
    """Scheduled exit monitor tick. Updates app_state for /exit-monitor/status."""
    if not _is_market_hours():
        return
    app_state["exit_monitor_last_run_at"] = datetime.now(ET).isoformat()
    app_state["exit_monitor_run_count"]  += 1
    try:
        events = evaluate_exits(
            db_path             = settings.prime_db_path,
            alpaca_client       = _get_alpaca(),
            buffer_reporter     = _get_reporter(),
            bot_token           = settings.telegram_bot_token,
            chat_id             = settings.ahmed_chat_id,
            ticker_lock_getter  = _get_ticker_lock_p,  # P1 fix: per-ticker lock (prevents execute/exit race)
        )
        app_state["exit_monitor_last_event_count"] = len(events)
        app_state["exit_monitor_last_error"]       = None
        if events:
            logger.info("Prime exit monitor: %d events", len(events))
    except Exception as e:
        app_state["exit_monitor_last_error"] = str(e)[:200]
        logger.error("Prime exit monitor failed: %s", e)


def _run_reconciler() -> None:
    if not _is_market_hours():
        return
    try:
        # G2 FIX (2026-05-03): Removed external _state_lock wrapping.
        # reconciler.py acquires _reconciler_lock *internally* on every call
        # (OMNI Pass 3 PROBE-I2 fix). Holding _state_lock here created a nested
        # lock order _state_lock -> _reconciler_lock. Any future path that
        # acquires _reconciler_lock first and then needs _state_lock would
        # produce an ABBA deadlock. The internal lock in reconciler.py is the
        # authoritative guard on app_state["execution_paused"] mutations.
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
    """Health check with real Alpaca connectivity probe."""
    # Block 2: standby gate
    if _SERVICE_MODE == "standby":
        return JSONResponse({"status": "standby", "reason": _standby_reason,
                             "service": "prime-execution", "code_hash": _CODE_HASH})
    try:
        acct = _get_alpaca().get_account()
        alpaca_ok = acct.get("status") == "ACTIVE"
    except Exception:
        alpaca_ok = False
    with _state_lock:
        app_state["alpaca_reachable"] = alpaca_ok
    # Block 3: trades_today derived from DB — in-memory counter is display-only cache
    trades_today_db = count_new_positions_today(settings.prime_db_path)
    with _state_lock:
        app_state["trades_today"] = trades_today_db
    execution_valid = alpaca_ok and not app_state.get("execution_paused", False)
    # shared/resilience HealthReport — standard surface for VECTOR + monitoring
    try:
        import sys as _sys_hr, os as _os_hr
        _sys_hr.path.insert(0, _os_hr.path.join(_os_hr.path.dirname(__file__), ".."))
        from shared.resilience.health import HealthReport
        _hr = HealthReport(agent="prime-execution", version="3.0.0")
        _hr.add_source("alpaca", ok=alpaca_ok)
        _hr.add_source("db", ok=_os_hr.path.exists(settings.prime_db_path))
        if not execution_valid:
            _hr.add_source("execution", ok=False, error="paused or alpaca unreachable")
        _resilience = _hr.to_dict()
    except Exception as _he:
        _resilience = {"error": str(_he)}
    return JSONResponse({
        "status":              "standby" if _SERVICE_MODE == "standby" else ("healthy" if execution_valid else "degraded"),
        "service":             "prime-execution",
        "version":             "3.0.0",
        "trades_today":        trades_today_db,
        "open_positions":      count_open_positions(settings.prime_db_path),
        "execution_valid":     execution_valid,
        "alpaca_reachable":    alpaca_ok,
        "execution_paused":    app_state["execution_paused"],
        "last_reconcile_at":   app_state["last_reconcile_at"],
        "uptime_since":        app_state["start_time"],
        "auto_execute":        app_state.get("auto_execute", False),
        "mode":                app_state.get("mode", "dry_run"),
        "state_restored":      _STATE_RESTORED_P,
        "code_hash":           _CODE_HASH,
        # P1 fix: /tmp sentinel is cleared on reboot — rely on hash comparison only (P3: cached 60s TTL)
        "stale_deploy":        _CODE_HASH != _get_cached_module_hash_p(),
        "resilience_status":   _resilience,
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

    # Block 2: STANDBY gate — reject execution while preflight is retrying
    if _SERVICE_MODE == "standby":
        return JSONResponse(
            status_code=503,
            content={"executed": False, "reason": f"Service in STANDBY: {_standby_reason}"},
        )

    # Market hours gate — never submit orders outside 9:30 AM – 4:00 PM ET.
    # Prevents after-hours order submission caused by service restarts, stale
    # queue flushes, or OMNI signals that arrive outside trading hours.
    if not _is_market_hours():
        logger.warning(
            "Prime execution rejected: outside market hours — ticker=%s window=%s",
            body.ticker, body.window_id,
        )
        return JSONResponse(
            status_code=422,
            content={
                "executed": False,
                "reason":   "Outside market hours (9:30 AM – 4:00 PM ET, weekdays only)",
            },
        )

    # Pipeline Sentinel — execution received this signal
    _ps_trace_id = f"{body.window_id}:{body.ticker}"
    try:
        from pipeline_client import trace_hop as _trace_hop
        _trace_hop(_ps_trace_id, "execution_received", "prime-execution", body.ticker, "prime")
    except Exception:
        pass

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

    # ── Long-Only Gate: Prime is equity-long only — reject bearish picks ──────
    # Fix deployed 2026-04-28 by OMNI: bearish direction was being translated to
    # Alpaca side='sell', which Alpaca rejects for fractional orders as a short-sell.
    # Prime does not short equities. Bearish concordances are silently rejected here.
    if body.direction == "bearish":
        logger.warning(
            "Prime execution rejected: bearish direction not supported (long-only system) "
            "— ticker=%s pathway=%s",
            body.ticker, body.pathway,
        )
        return JSONResponse(
            status_code=422,
            content={
                "executed": False,
                "reason":   "Prime is long-only — bearish direction not supported. "
                            "Bearish picks are not executed in Prime equity system.",
            },
        )

    # ── C-11: Prime VIX Gate — block new equity positions above VIX halt threshold ──────
    # B3: Uses FreshValue cache (5-min TTL) — no live Axiom call on every /execute
    try:
        from config import VIX_PAUSE_THRESHOLD_PRIME, AXIOM_URL_PRIME, AXIOM_SECRET_PRIME
        _vix = _get_prime_vix_cached(AXIOM_URL_PRIME, AXIOM_SECRET_PRIME)
        if _vix >= VIX_PAUSE_THRESHOLD_PRIME:
            logger.warning(
                "C-11: Prime VIX gate — VIX=%.1f >= %.1f, blocking new position for %s",
                _vix, VIX_PAUSE_THRESHOLD_PRIME, body.ticker,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "executed": False,
                    "reason":   f"prime_vix_gate: VIX={_vix:.1f} >= {VIX_PAUSE_THRESHOLD_PRIME} — new positions blocked",
                },
            )
        app_state["vix_brake_prime"] = "CLEAR"
    except Exception as _ve:
        logger.warning("C-11: VIX check failed (%s) — proceeding (fail-open for prime)", _ve)
        app_state.setdefault("vix_brake_prime", "UNKNOWN")

    # ── Reconciler pause check with P2 auto-clear (4h max) ─────────────────────
    if app_state.get("execution_paused"):
        # P2 fix: auto-clear after 4h — prevents indefinite stall over weekends
        paused_at = app_state.get("reconcile_paused_at")
        if paused_at:
            import time as _t_chk
            paused_hours = (_t_chk.time() - paused_at) / 3600
            if paused_hours >= 4.0:
                with _state_lock:
                    app_state["execution_paused"] = False
                    app_state["reconcile_paused_at"] = None
                    _svc_state.write("execution_paused", False)
                try:
                    from shared.resilience.alerts import alert_ahmed as _aa
                    _aa(
                        f"Prime auto-resumed after {paused_hours:.1f}h reconciler pause.\n"
                        f"Verify positions are correct before market open.",
                        key="prime-reconcile-auto-resume",
                        severity="WARNING",
                    )
                except Exception:
                    pass
                logger.warning("P2: Prime auto-resumed after %.1fh reconciler pause", paused_hours)
            else:
                return JSONResponse(
                    status_code = 503,
                    content     = {
                        "executed": False,
                        "reason":   f"Execution paused (reconciler mismatch, {paused_hours:.1f}h ago). "
                                    "Auto-clears at 4h or send RESUME directive.",
                    },
                )
        else:
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

    # ── Correlation Gate (GENESIS 2026-05-07) ────────────────────────────────────
    # Check incoming ticker against all open positions. Adjust size or block.
    try:
        from correlation_tracker import evaluate_correlation_gate, init_correlation_schema
        init_correlation_schema(settings.prime_db_path)
        _open_tickers_p = [p["ticker"] for p in get_open_positions(settings.prime_db_path)]
        _corr_result_p = evaluate_correlation_gate(
            new_ticker            = body.ticker,
            direction             = body.direction,
            open_position_tickers = _open_tickers_p,
            db_path               = settings.prime_db_path,
            polygon_api_key       = os.getenv("POLYGON_API_KEY", ""),
            telegram_bot_token    = settings.telegram_bot_token,
            telegram_chat_id      = settings.ahmed_chat_id,
        )
        if _corr_result_p.gate_decision == "BLOCK":
            logger.warning(
                "CORRELATION GATE BLOCK: %s max_corr=%.3f — order rejected",
                body.ticker, _corr_result_p.max_correlation,
            )
            return JSONResponse(
                status_code=422,
                content={
                    "executed":        False,
                    "reason":          f"Correlation BLOCK: {body.ticker} has max_corr={_corr_result_p.max_correlation:.3f} with open positions",
                    "gate":            "correlation",
                    "max_correlation": _corr_result_p.max_correlation,
                    "existing_tickers": _corr_result_p.existing_tickers,
                },
            )
        if _corr_result_p.size_adjustment < 1.0:
            _old_shares = shares
            shares = round(shares * _corr_result_p.size_adjustment, 2)
            shares = max(1.0, shares)  # minimum 1 share always
            _position_size_usd_p = body.position_size_usd * _corr_result_p.size_adjustment
            logger.info(
                "CORRELATION GATE REDUCE: %s max_corr=%.3f → shares %.2f→%.2f size_adj=%.2fx",
                body.ticker, _corr_result_p.max_correlation, _old_shares, shares,
                _corr_result_p.size_adjustment,
            )
    except Exception as _corr_exc_p:
        logger.warning(
            "Correlation gate failed (non-fatal, execution continues): %s", _corr_exc_p
        )

    # ── Atomic limit check + PENDING slot reservation (Cipher Finding 1+2 fix) ─
    import json as _json
    pending_id = None
    with _execute_lock:
        open_count  = count_open_positions(settings.prime_db_path)  # includes 'pending'
        today_count = count_new_positions_today(settings.prime_db_path)

        # Per-ticker duplicate guard (2026-04-24): reject if ticker already open.
        # Concordances fire per-window; a strong ticker can generate new P1 events
        # in subsequent windows. Without this guard, the same ticker gets opened
        # multiple times. Must be inside the lock to be race-condition safe.
        if ticker_already_open(settings.prime_db_path, body.ticker):
            logger.info(
                "Prime execution blocked: %s already has an open position", body.ticker
            )
            return JSONResponse(
                status_code=429,
                content={
                    "executed": False,
                    "reason":   f"{body.ticker} already has an open position — no duplicates allowed",
                },
            )

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

    # ── AUTO_EXECUTE Gate — dry-run if kill-switch is off ──────────────────────
    if not app_state.get("auto_execute", False):
        order_preview = {
            "ticker":    body.ticker,
            "direction": body.direction,
            "qty":       shares,
            "verdict":   body.verdict,
        }
        logger.info("DRY_RUN: would have executed %s %s (verdict=%s)", body.ticker, body.direction, body.verdict)
        if pending_id:
            cancel_pending_position(settings.prime_db_path, pending_id)
        # Send dry-run Telegram alert
        try:
            from shared.notification_router import notify_info as _ni
            _ni("prime-execution", "[DRY RUN] Would execute",
                f"{body.direction} | verdict={body.verdict} | price=${current_price}",
                ticker=body.ticker)
        except Exception:
            pass
        return JSONResponse(content={"executed": False, "mode": "dry_run", "order_preview": order_preview})

    # Block 4: Pre-submission order existence check
    # Check Alpaca directly before submitting — prevents duplicate equity orders
    # after restart or OMNI retry on the same window.
    # P2 fix: validate COID format — Alpaca requires alphanumeric + hyphens only
    _verdict_slug = re.sub(r"[^A-Za-z0-9]", "", body.verdict[:4]) if body.verdict else "go"
    if not _verdict_slug:
        _verdict_slug = "go"
    _prime_coid = f"nexus-prime-{re.sub(r'[^A-Za-z0-9]', '', body.ticker)}-{_verdict_slug}"
    _prime_coid = _prime_coid[:48]  # Alpaca COID max length
    _existing_prime_order = alpaca.get_order_by_client_id(_prime_coid)
    if _existing_prime_order is not None:
        _ep_status = _existing_prime_order.get("status", "unknown")
        if _ep_status not in ("cancelled", "expired", "rejected"):
            logger.info(
                "Block 4: Prime order already exists for %s (status=%s) — skipping",
                body.ticker, _ep_status,
            )
            if pending_id:
                alpaca_order_id = _existing_prime_order.get("id")
                confirm_pending_position(settings.prime_db_path, pending_id, alpaca_order_id)
            return JSONResponse(content={
                "executed": False,
                "status": "recovered",
                "reason": f"order_already_exists_on_alpaca: {_ep_status}",
                "ticker": body.ticker,
            })

    # ── Capital Gate (OMNI May 20, 2026): Cross-system position + allocation checks ──
    # Prevents: (1) doubled positions on same ticker (Alpha + Prime race)
    #           (2) capital double-allocation after position rotation
    _capital_gate = None
    _allocation_id = None
    if CAPITAL_GATE_AVAILABLE:
        try:
            _capital_gate = CapitalGate(router_url="http://localhost:9100", timeout_sec=3)
            _gate_allowed, _gate_result = _capital_gate.pre_execution_check(
                ticker=body.ticker,
                system="prime",
                amount_usd=body.position_size_usd
            )
            logger.info(
                "Capital gate result for %s: allowed=%s blocked_by=%s",
                body.ticker, _gate_allowed, _gate_result.get("blocked_by", "none")
            )
            if not _gate_allowed:
                logger.warning("Capital gate blocked execution: %s", _gate_result.get("blocked_by"))
                if pending_id:
                    cancel_pending_position(settings.prime_db_path, pending_id)
                return JSONResponse(
                    status_code=403,
                    content={
                        "executed": False,
                        "reason": _gate_result.get("blocked_by", "capital_gate_blocked"),
                        "gate_details": _gate_result
                    }
                )
            _allocation_id = _gate_result.get("allocation_id")
        except Exception as e:
            logger.warning("Capital gate check failed: %s — continuing with caution", str(e))
    
    try:
        order           = alpaca.place_order(body.ticker, shares, side, client_order_id=_prime_coid)
        alpaca_order_id = order.get("id")
        logger.info(
            "Prime order placed: %s %s %.0f shares @ ~$%.2f | order_id=%s",
            side.upper(), body.ticker, shares, current_price, alpaca_order_id,
        )
        try:
            from pipeline_client import trace_hop as _trace_hop
            _trace_hop(_ps_trace_id, "alpaca_submitted", "prime-execution", body.ticker, "prime")
        except Exception:
            pass
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

    # ── G10 SYS-3: OSI-Aware Fill Confirmation ───────────────────────────────
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
        order_id     = alpaca_order_id or "",
        ticker       = body.ticker,
        expected_qty = float(shares),
    )
    if _fill.status == _FillStatus.VOID:
        logger.error(
            "Trade VOID for %s: %s (elapsed: %.1fs)",
            body.ticker, _fill.void_reason, _fill.elapsed_sec,
        )
        if pending_id:
            cancel_pending_position(settings.prime_db_path, pending_id)
        try:
            from shared.notification_router import notify_warn as _nw
            _nw("prime-execution", "VOID trade", _fill.void_reason, ticker=body.ticker)
        except Exception:
            pass
        return JSONResponse(
            status_code=503,
            content={"status": "void", "reason": _fill.void_reason, "executed": False},
        )

    # ── Alpaca confirmed — promote pending slot to open position ─────────────
    _confirmed_price = _fill.fill_price if _fill.fill_price is not None else current_price
    # GAP-004 Option C: Record confirmed order status in logs for audit trail.
    # The order_id is already stored in confirm_pending_position as alpaca_order_id.
    # Any future reconciler check can verify via /orders/{order_id} — this is
    # the source of truth that survives Alpaca /positions propagation lag.
    logger.info(
        "GAP-004: Position %s (id=%d) confirmed via order %s — "
        "order-centric tracking active (immune to /positions lag)",
        body.ticker, pending_id, (alpaca_order_id or "?")[:12],
    )
    confirm_pending_position(
        db_path          = settings.prime_db_path,
        position_id      = pending_id,
        alpaca_order_id  = alpaca_order_id or "",
        entry_price      = _confirmed_price,
        shares_remaining = shares,
    )
    
    # ── Release Capital Gate Allocation (OMNI May 20, 2026) ──
    # Order is now confirmed on Alpaca. Release the capital allocation lock
    # so other systems can claim the allocated capital if needed.
    if _allocation_id and _capital_gate:
        try:
            _capital_gate.release_allocation(_allocation_id)
            logger.info("Capital allocation released: %s for %s", _allocation_id, body.ticker)
        except Exception as e:
            logger.warning("Failed to release capital allocation %s: %s", _allocation_id, str(e))
    
    position_id = pending_id
    with _state_lock:
        app_state["trades_today"] += 1
    _svc_state.write("trades_today", app_state["trades_today"])  # GAP-002

    # Pipeline Sentinel — Alpaca order confirmed, position live
    try:
        from pipeline_client import trace_hop as _trace_hop
        _trace_hop(_ps_trace_id, "alpaca_confirmed", "prime-execution", body.ticker, "prime")
    except Exception:
        pass

    _notify_entry(settings.telegram_bot_token, settings.ahmed_chat_id,
                  body, position_id, shares, current_price, alpaca_order_id)

    # Discord mirror — fill confirmed to #execution-monitoring
    try:
        import sys as _sys
        _sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
        from discord_client import post_to_discord as _disc
        _disc(
            channel     = "execution-monitoring",
            title       = f"💎 PRIME FILL — {body.ticker}",
            description = f"{body.direction.upper()} equity. {round(shares,2)} shares @ ${current_price:.2f}.",
            color       = "teal",
            fields      = [
                {"name": "Ticker",    "value": body.ticker,                    "inline": True},
                {"name": "Direction", "value": body.direction.upper(),         "inline": True},
                {"name": "Shares",    "value": str(round(shares, 2)),          "inline": True},
                {"name": "Price",     "value": f"${current_price:.2f}",        "inline": True},
                {"name": "Size",      "value": f"${body.position_size_usd:,.0f}", "inline": True},
                {"name": "Position",  "value": f"#{position_id}",              "inline": True},
            ],
            footer      = "Prime Execution",
            agent       = "prime-execution",
        )
    except Exception as _disc_exc:
        logger.warning(f"Discord push failed (non-fatal): {_disc_exc}")

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


@app.get("/trades")
def get_trades_summary(x_nexus_prime_secret: str = Header(default="")) -> JSONResponse:
    """
    Return Prime execution summary for SOVEREIGN observability.

    Exposes: session go verdicts, Alpaca orders submitted/filled/rejected,
    open positions, and buying power state.  SOVEREIGN polls this at
    market open, midday, and close every session.
    """
    verify_secret(x_nexus_prime_secret)

    # Count positions by status from DB
    import sqlite3 as _sqlite3
    orders_submitted = 0
    orders_filled = 0
    orders_rejected = 0
    buying_power_used = 0.0
    try:
        conn = _sqlite3.connect(settings.prime_db_path)
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            "SELECT status, position_size_usd FROM positions WHERE DATE(opened_at)=DATE('now')"
        ).fetchall()
        conn.close()
        for row in rows:
            orders_submitted += 1
            st = row["status"]
            if st in ("open", "closed", "partial_exit"):
                orders_filled += 1
            elif st == "rejected":
                orders_rejected += 1
            buying_power_used += float(row["position_size_usd"] or 0)
    except Exception:
        pass

    open_positions = get_open_positions(settings.prime_db_path)

    return JSONResponse({
        "service":               "prime-execution",
        "session_date":          __import__("datetime").date.today().isoformat(),
        "go_verdicts":           app_state.get("trades_today", 0),
        "alpaca_orders_submitted": orders_submitted,
        "alpaca_orders_filled":  orders_filled,
        "alpaca_orders_rejected": orders_rejected,
        "open_positions_count":  len(open_positions),
        "open_positions":        open_positions,
        "buying_power_used_usd": round(buying_power_used, 2),
        "execution_paused":      app_state.get("execution_paused", False),
        "auto_execute":          app_state.get("auto_execute", False),
        "alpaca_reachable":      app_state.get("alpaca_reachable", False),
    })


@app.get("/exit-monitor/status")
def exit_monitor_status(x_nexus_prime_secret: str = Header(default="")) -> JSONResponse:
    """
    Return current Prime exit monitor scheduler state.

    Reports last run timestamp, last event count, any errors, and scheduler job status.
    The exit monitor runs every 1 min during market hours (9–15 ET) via APScheduler.
    """
    verify_secret(x_nexus_prime_secret)
    scheduler = app_state.get("scheduler")
    next_run_at = None
    scheduler_running = False
    if scheduler:
        scheduler_running = scheduler.running
        try:
            job = scheduler.get_job("prime_exit_monitor")
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
        _svc_state.write("execution_paused", False)  # GAP-002
    logger.info("Execution resumed via /resume (was_paused=%s)", was_paused)
    return JSONResponse({"resumed": True, "was_paused": was_paused})


@app.post("/reconcile")
def manual_reconcile(x_nexus_prime_secret: str = Header(default="")) -> JSONResponse:
    """Manually trigger a reconciliation pass.

    G2 FIX (2026-05-03): Removed external _state_lock wrapping. reconciler.py
    owns _reconciler_lock internally — concurrent scheduled + manual runs are
    safely serialized there. The external lock created an ABBA gradient.
    """
    verify_secret(x_nexus_prime_secret)
    result = run_reconciler(
        settings.prime_db_path, _get_alpaca(),
        settings.telegram_bot_token, settings.ahmed_chat_id,
        app_state,
    )
    return JSONResponse(result)



# ── SOVEREIGN Push Endpoints ──────────────────────────────────────────────────

class _SovDirective(BaseModel):
    directive: str
    data: dict = {}
    from_agent: str = "sovereign"


@app.post("/sovereign/directive", status_code=200)
def sovereign_directive(
    body: _SovDirective,
    x_nexus_prime_secret: str = Header(default=""),
) -> JSONResponse:
    """SOVEREIGN pushes a directive. Zero polling lag."""
    verify_secret(x_nexus_prime_secret)
    d = body.directive.strip().upper()
    logger.info("SOVEREIGN direct push: %s", d)

    if d == "PING":
        report("prime_execution", "ack", {"directive": "PING", "status": "alive"})
        return JSONResponse({"ok": True, "directive": "PING", "status": "alive"})
    elif d == "HALT":
        with _state_lock:
            app_state["execution_paused"] = True
            _svc_state.write("execution_paused", True)  # GAP-002
        report("prime_execution", "ack", {"directive": "HALT", "status": "applied", "paused": True})
        return JSONResponse({"ok": True, "directive": "HALT", "paused": True})
    elif d == "RESUME":
        with _state_lock:
            app_state["execution_paused"] = False
            _svc_state.write("execution_paused", False)  # GAP-002
        report("prime_execution", "ack", {"directive": "RESUME", "status": "applied", "paused": False})
        return JSONResponse({"ok": True, "directive": "RESUME", "paused": False})
    elif d in ("FLUSH", "RESET_DAY"):
        with _state_lock:
            app_state["trades_today"] = 0
        report("prime_execution", "ack", {"directive": d, "status": "applied"})
        return JSONResponse({"ok": True, "directive": d})
    elif d == "STATUS":
        with _state_lock:
            snap = {
                "directive": "STATUS", "service": "prime_execution",
                "paused": app_state.get("execution_paused", False),
                "auto_execute": app_state.get("auto_execute", False),
                "trades_today": app_state.get("trades_today", 0),
            }
        report("prime_execution", "status", snap)
        return JSONResponse({"ok": True, **snap})
    else:
        report("prime_execution", "ack", {"directive": d, "status": "unrecognized"})
        return JSONResponse({"ok": False, "error": f"unrecognized directive: {d}"}, status_code=400)




# ── TCA (Institutional Gap 3) ───────────────────────────────────────────────
@app.get("/tca")
def prime_tca_endpoint(
    secret: str = Header(None, alias="X-Nexus-Prime-Secret")
):
    """Transaction Cost Analysis: recent records + today's summary."""
    if secret != settings.nexus_prime_secret:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        from tca_tracker import get_recent_records, get_daily_summary
        return {
            "summary": get_daily_summary(settings.prime_db_path),
            "records": get_recent_records(settings.prime_db_path, limit=50),
        }
    except Exception as e:
        return {"error": str(e)}


# ── GAP-006: /log_tail — Runtime log access (2026-04-27) ─────────────────────
# Gives OMNI, integrity checker, and SOVEREIGN visibility into recent logs
# without requiring SSH or Railway dashboard access. Authenticated.
@app.get("/log_tail")
def log_tail(
    lines:                int = 50,
    filter_str:           str = "",
    x_nexus_prime_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Return the last N lines of the service stderr log.
    Optional filter_str narrows to lines containing that string.
    Max 200 lines per call to avoid abuse.
    """
    verify_secret(x_nexus_prime_secret)
    import os as _os, pathlib as _pl
    lines = min(lines, 200)
    log_path = _os.getenv("STDERR_LOG_PATH", "")
    if not log_path or not _pl.Path(log_path).exists():
        candidates = [
            "/Users/ahmedsadek/nexus/logs/alpha-execution/stderr.log",
            "/Users/ahmedsadek/nexus/logs/prime-execution/stderr.log",
            "/Users/ahmedsadek/nexus/logs/prime-execution/stderr.log",
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


@app.get("/sovereign/status", status_code=200)
def sovereign_status(
    x_nexus_prime_secret: str = Header(default=""),
) -> JSONResponse:
    """SOVEREIGN queries full prime-execution state on-demand."""
    verify_secret(x_nexus_prime_secret)
    with _state_lock:
        return JSONResponse({
            "ok": True, "service": "prime_execution",
            "paused": app_state.get("execution_paused", False),
            "auto_execute": app_state.get("auto_execute", False),
            "trades_today": app_state.get("trades_today", 0),
            "total_trades": app_state.get("total_trades", 0),
            "port": int(__import__("os").getenv("PORT", "8006")),
        })


def _notify_entry(bot_token, chat_id, body, position_id, shares, price, order_id):
    emoji = "📈" if body.direction == "bullish" else "📉"
    try:
        from shared.notification_router import notify_info as _ni
        _ni(
            "prime-execution",
            f"{emoji} PRIME TRADE OPENED — {body.ticker}",
            (
                f"Direction: {body.direction.upper()} | {shares:.0f} shares @ ${price:.2f}\n"
                f"Size: ${body.position_size_usd:,.0f} | Pathway: {body.pathway}\n"
                f"Score: {body.weighted_score:.1f} | Position #{position_id}"
            ),
            ticker=body.ticker,
        )
    except Exception:
        pass
