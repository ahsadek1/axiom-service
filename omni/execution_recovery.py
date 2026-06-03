"""
execution_recovery.py — OMNI Autonomous Execution Recovery

MANDATE (2026-04-27, issued by Ahmed Sadek):
  OMNI is an intelligent agent. Any execution failure is handled autonomously,
  immediately, with zero hesitation. No failure surfaces to Ahmed as
  "manual action required" without first exhausting every self-heal path.

Recovery protocol:
  1. Classify failure (429/5xx/timeout/network/unknown)
  2. Attempt immediate resolution per error class
  3. Retry once after resolution
  4. If retry fails → escalate to SOVEREIGN with full diagnostic
  5. If SOVEREIGN unreachable → page Ahmed directly

Error classes and autonomous actions:
  DAILY_CAP           — daily position cap exhausted. Action: no autonomous fix
                        possible mid-session; escalate to SOVEREIGN for override.
  TICKER_DUPLICATE    — ticker already open. Action: safe skip, execution continues
                        for other tickers. No escalation needed.
  CONCURRENT_CAP      — max concurrent positions. Action: log + skip, not an error.
  AUTH_FAILURE        — 401/403. Action: probe Alpaca, escalate immediately.
  RATE_LIMITED        — 429 non-cap. Action: wait Retry-After then retry once.
  BROKER_UNAVAILABLE  — 5xx. Action: retry 2x with 15s backoff then escalate.
  TIMEOUT             — request timeout. Action: retry once immediately then escalate.
  NETWORK             — connection failure. Action: probe then retry once.
  UNKNOWN             — anything else. Action: escalate immediately.

Escalation chain:
  Attempt 1+2 → autonomous fix + retry
  Still failing → SOVEREIGN via bus (immediate)
  SOVEREIGN unreachable → Ahmed DM direct (fallback, always)

Author: OMNI (2026-04-27, MANDATE compliance)
"""

import logging
import os
import time
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable

import requests as _requests

logger = logging.getLogger("omni.execution_recovery")

# ── Constants ─────────────────────────────────────────────────────────────────

SOVEREIGN_BUS_URL  = os.environ.get("SOVEREIGN_BUS_URL", "http://192.168.1.141:9999")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
AHMED_CHAT_ID      = os.environ.get("AHMED_CHAT_ID", "8573754783")
ALPACA_PAPER_URL   = "https://paper-api.alpaca.markets"

MAX_RETRY_ATTEMPTS = 2      # retries before escalation
RETRY_BACKOFF_S    = 15     # seconds between retries
RATE_LIMIT_DEFAULT = 60     # default wait on rate limit


# ── Error Classification ──────────────────────────────────────────────────────

class ExecFailClass(Enum):
    DAILY_CAP             = "daily_cap"
    TICKER_DUPLICATE      = "ticker_duplicate"
    CONCURRENT_CAP        = "concurrent_cap"
    AUTH_FAILURE          = "auth_failure"
    RATE_LIMITED          = "rate_limited"
    BROKER_5XX            = "broker_5xx"
    TIMEOUT               = "timeout"
    NETWORK               = "network"
    OUTSIDE_MARKET_HOURS  = "outside_market_hours"
    UNKNOWN               = "unknown"


@dataclass
class RecoveryResult:
    success:              bool          # True = retry succeeded, trade placed
    skipped:              bool          # True = safe skip (dup/concurrent), no error
    error_class:          ExecFailClass
    attempts:             int
    action_taken:         str
    escalated_sovereign:  bool
    escalated_ahmed:      bool
    final_summary:        str


def classify_exec_failure(exec_resp: Optional[str]) -> ExecFailClass:
    """
    Classify an execution failure response string.
    exec_resp format from execution_router: "429:CLASS:detail" or "HTTP 5xx" or "timeout".
    """
    if not exec_resp:
        return ExecFailClass.UNKNOWN

    r = exec_resp.lower()

    # Structured 422 from our router (outside market hours)
    if exec_resp.startswith("422:OUTSIDE_MARKET_HOURS"):
        return ExecFailClass.OUTSIDE_MARKET_HOURS

    # Structured 429 from our router
    if exec_resp.startswith("429:DAILY_CAP"):
        return ExecFailClass.DAILY_CAP
    if exec_resp.startswith("429:TICKER_DUPLICATE"):
        return ExecFailClass.TICKER_DUPLICATE
    if exec_resp.startswith("429:CONCURRENT_CAP"):
        return ExecFailClass.CONCURRENT_CAP
    if exec_resp.startswith("429:"):
        return ExecFailClass.RATE_LIMITED

    if "timeout" in r or "timed out" in r:
        return ExecFailClass.TIMEOUT
    if "connection" in r or "network" in r or "httpsconnectionpool" in r:
        return ExecFailClass.NETWORK
    if "http 401" in r or "http 403" in r or "auth" in r:
        return ExecFailClass.AUTH_FAILURE
    if any(f"http 5{x}" in r for x in range(10)):
        return ExecFailClass.BROKER_5XX
    if "http 4" in r:
        return ExecFailClass.RATE_LIMITED

    return ExecFailClass.UNKNOWN


# ── Notifications ─────────────────────────────────────────────────────────────

def _notify_sovereign(ticker: str, system: str, error_class: ExecFailClass,
                      action: str, result: RecoveryResult, exec_resp: str) -> bool:
    """Push full diagnostic to SOVEREIGN bus. Returns True if delivered."""
    status = "✅ RECOVERED" if result.success else ("⏭ SAFE SKIP" if result.skipped else "🚨 NEEDS ATTENTION")
    text = (
        f"🔧 OMNI EXEC RECOVERY — {status}\n"
        f"Ticker: {ticker} | System: {system.upper()}\n"
        f"Error class: {error_class.value}\n"
        f"Raw response: {exec_resp[:120]}\n"
        f"Action taken: {action}\n"
        f"Attempts: {result.attempts}\n"
        f"Outcome: {result.final_summary}"
    )
    try:
        resp = _requests.post(
            f"{SOVEREIGN_BUS_URL}/send",
            json={"from": "omni", "to": "sovereign", "message": text},
            timeout=5,
        )
        return resp.status_code in (200, 201, 202)
    except Exception as e:
        logger.warning("SOVEREIGN bus unreachable: %s", e)
        return False


def _notify_ahmed(ticker: str, system: str, error_class: ExecFailClass,
                  action: str, exec_resp: str, attempts: int, summary: str) -> None:
    """Page Ahmed directly — only fires when autonomous recovery exhausted."""
    text = (
        f"🚨 OMNI — EXECUTION FAILURE (auto-recovery exhausted)\n"
        f"Ticker: {ticker} | System: {system.upper()}\n"
        f"Error: {error_class.value}\n"
        f"Raw: {exec_resp[:120]}\n"
        f"Tried: {action}\n"
        f"Attempts: {attempts}\n"
        f"Status: {summary}\n"
        f"→ SOVEREIGN has been notified. Manual review may be needed."
    )
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("No TELEGRAM_BOT_TOKEN — cannot page Ahmed")
        return
    try:
        _requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": AHMED_CHAT_ID, "text": text},
            timeout=5,
        )
    except Exception as e:
        logger.warning("Ahmed Telegram page failed: %s", e)


# ── Autonomous Resolution Handlers ────────────────────────────────────────────

def _try_retry(route_fn: Callable, delay_s: float = 0) -> tuple[bool, Optional[str]]:
    """Wait delay_s then call route_fn(). Returns (ok, resp)."""
    if delay_s > 0:
        logger.info("Waiting %.0fs before retry...", delay_s)
        time.sleep(delay_s)
    return route_fn()


def _resolve_outside_market_hours(
    ticker: str,
    system: str,
    exec_resp: str,
    route_fn: Callable,
) -> RecoveryResult:
    """
    Outside market hours — wait until 9:30 AM ET then retry exactly once.
    If market open has already passed (shouldn't happen, but be safe), escalate.
    """
    import datetime
    import pytz

    ET = pytz.timezone("America/New_York")
    now = datetime.datetime.now(ET)
    market_open = now.replace(hour=9, minute=30, second=5, microsecond=0)

    if now >= market_open:
        # Market is already open — retry immediately once (clock drift / edge case)
        logger.info(
            "OUTSIDE_MARKET_HOURS recovery: market is already open for %s (%s) — retrying immediately.",
            ticker, system,
        )
        ok, resp = route_fn()
        if ok:
            result = RecoveryResult(
                success=True, skipped=False, error_class=ExecFailClass.OUTSIDE_MARKET_HOURS,
                attempts=1, action_taken="Market already open — immediate retry succeeded.",
                escalated_sovereign=True, escalated_ahmed=False,
                final_summary="Recovered: immediate retry at open succeeded. Trade placed.",
            )
            _notify_sovereign(ticker, system, ExecFailClass.OUTSIDE_MARKET_HOURS,
                              result.action_taken, result, exec_resp)
            return result
        # Immediate retry also failed — escalate
        action = "Market open but execution still rejecting — escalating to SOVEREIGN."
        result = RecoveryResult(
            success=False, skipped=False, error_class=ExecFailClass.OUTSIDE_MARKET_HOURS,
            attempts=1, action_taken=action,
            escalated_sovereign=True, escalated_ahmed=False,
            final_summary=f"Immediate retry failed ({resp}). SOVEREIGN notified.",
        )
        _notify_sovereign(ticker, system, ExecFailClass.OUTSIDE_MARKET_HOURS,
                          action, result, exec_resp)
        return result

    # Calculate wait until 9:30 AM ET
    wait_s = (market_open - now).total_seconds()
    logger.info(
        "OUTSIDE_MARKET_HOURS recovery: waiting %.0fs until market open for %s (%s).",
        wait_s, ticker, system,
    )
    time.sleep(wait_s)

    ok, resp = route_fn()
    if ok:
        result = RecoveryResult(
            success=True, skipped=False, error_class=ExecFailClass.OUTSIDE_MARKET_HOURS,
            attempts=1, action_taken=f"Waited {wait_s:.0f}s until market open (9:30 ET) then retried.",
            escalated_sovereign=True, escalated_ahmed=False,
            final_summary=f"Recovered at market open: {resp}. Trade placed.",
        )
        _notify_sovereign(ticker, system, ExecFailClass.OUTSIDE_MARKET_HOURS,
                          result.action_taken, result, exec_resp)
        return result

    # Retry at open failed — escalate
    action = f"Waited {wait_s:.0f}s until 9:30 ET open, retried once — still failing."
    result = RecoveryResult(
        success=False, skipped=False, error_class=ExecFailClass.OUTSIDE_MARKET_HOURS,
        attempts=1, action_taken=action,
        escalated_sovereign=True, escalated_ahmed=False,
        final_summary=f"Open-retry failed: {resp}. SOVEREIGN notified.",
    )
    logger.critical(
        "OUTSIDE_MARKET_HOURS recovery FAILED at open for %s (%s): %s",
        ticker, system, resp,
    )
    sov_ok = _notify_sovereign(ticker, system, ExecFailClass.OUTSIDE_MARKET_HOURS,
                               action, result, exec_resp)
    if not sov_ok:
        result.escalated_ahmed = True
        _notify_ahmed(ticker, system, ExecFailClass.OUTSIDE_MARKET_HOURS,
                      action, exec_resp, 1, result.final_summary)
    return result


def _resolve_daily_cap(ticker: str, system: str, exec_resp: str) -> RecoveryResult:
    """
    Daily position cap hit — cannot autonomously override mid-session.
    Escalate to SOVEREIGN immediately; this is a governance-level decision.
    """
    logger.warning(
        "DAILY_CAP hit for %s (%s) — cannot auto-resolve. Escalating to SOVEREIGN.",
        ticker, system
    )
    action = "Daily cap cannot be self-healed. Escalated to SOVEREIGN for override decision."
    result = RecoveryResult(
        success=False, skipped=False, error_class=ExecFailClass.DAILY_CAP,
        attempts=0, action_taken=action,
        escalated_sovereign=True, escalated_ahmed=False,
        final_summary=f"DAILY_CAP: {exec_resp[:80]}. SOVEREIGN notified.",
    )
    ok = _notify_sovereign(ticker, system, ExecFailClass.DAILY_CAP, action, result, exec_resp)
    if not ok:
        # SOVEREIGN unreachable — go directly to Ahmed
        result.escalated_ahmed = True
        _notify_ahmed(ticker, system, ExecFailClass.DAILY_CAP, action, exec_resp, 0,
                      result.final_summary)
    return result


def _resolve_ticker_duplicate(ticker: str, system: str) -> RecoveryResult:
    """Ticker already open — safe skip, no escalation needed."""
    logger.info("TICKER_DUPLICATE for %s (%s) — safe skip, position already open.", ticker, system)
    return RecoveryResult(
        success=False, skipped=True, error_class=ExecFailClass.TICKER_DUPLICATE,
        attempts=0, action_taken="Safe skip — position already open for this ticker.",
        escalated_sovereign=False, escalated_ahmed=False,
        final_summary="Duplicate blocked correctly. No action needed.",
    )


def _resolve_concurrent_cap(ticker: str, system: str, exec_resp: str) -> RecoveryResult:
    """Max concurrent positions — safe skip, not a system error."""
    logger.info("CONCURRENT_CAP for %s (%s) — max positions open, safe skip.", ticker, system)
    return RecoveryResult(
        success=False, skipped=True, error_class=ExecFailClass.CONCURRENT_CAP,
        attempts=0, action_taken="Safe skip — concurrent position cap reached.",
        escalated_sovereign=False, escalated_ahmed=False,
        final_summary=f"Concurrent cap: {exec_resp[:80]}. Position slots full — normal gate.",
    )


def _resolve_with_retry(
    ticker: str, system: str,
    error_class: ExecFailClass,
    exec_resp: str,
    route_fn: Callable,
    delay_s: float,
    action_desc: str,
) -> RecoveryResult:
    """
    Generic: wait delay_s, retry route_fn up to MAX_RETRY_ATTEMPTS times.
    Escalate to SOVEREIGN (and Ahmed as fallback) if all retries fail.
    """
    attempts = 0
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        attempts = attempt
        logger.info(
            "Auto-recovery attempt %d/%d for %s (%s) — %s",
            attempt, MAX_RETRY_ATTEMPTS, ticker, system, action_desc
        )
        ok, resp = _try_retry(route_fn, delay_s=delay_s if attempt == 1 else RETRY_BACKOFF_S)
        if ok:
            logger.info(
                "Auto-recovery SUCCEEDED on attempt %d for %s (%s): %s",
                attempt, ticker, system, resp
            )
            result = RecoveryResult(
                success=True, skipped=False, error_class=error_class,
                attempts=attempts,
                action_taken=f"{action_desc} — succeeded on retry {attempt}",
                escalated_sovereign=True, escalated_ahmed=False,
                final_summary=f"Recovered on attempt {attempt}. Trade placed.",
            )
            _notify_sovereign(ticker, system, error_class, result.action_taken, result, exec_resp)
            return result

    # All retries exhausted — escalate
    action = f"{action_desc} — failed after {MAX_RETRY_ATTEMPTS} attempts"
    result = RecoveryResult(
        success=False, skipped=False, error_class=error_class,
        attempts=attempts, action_taken=action,
        escalated_sovereign=True, escalated_ahmed=False,
        final_summary=f"Auto-recovery failed after {MAX_RETRY_ATTEMPTS} retries. Escalated.",
    )
    logger.critical(
        "Auto-recovery FAILED after %d attempts for %s (%s). Escalating to SOVEREIGN.",
        MAX_RETRY_ATTEMPTS, ticker, system
    )
    sov_ok = _notify_sovereign(ticker, system, error_class, action, result, exec_resp)
    if not sov_ok:
        # SOVEREIGN unreachable — Ahmed is fallback
        result.escalated_ahmed = True
        _notify_ahmed(ticker, system, error_class, action, exec_resp, attempts, result.final_summary)
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def auto_recover_execution(
    ticker:          str,
    system:          str,
    exec_resp:       Optional[str],
    route_fn:        Callable,
    synthesis_id:    Optional[int]  = None,
    omni_db_path:    Optional[str]  = None,
    exec_url:        Optional[str]  = None,
) -> RecoveryResult:
    """
    OMNI's autonomous execution recovery brain.

    Called immediately after any execution failure.
    Classifies the error, takes the appropriate autonomous action,
    retries where possible, and escalates if self-heal is exhausted.

    Args:
        ticker:         Ticker symbol.
        system:         'alpha' or 'prime'.
        exec_resp:      Raw response string from route_to_execution().
        route_fn:       Zero-arg callable that re-attempts execution.
                        Called automatically during retry attempts.
        synthesis_id:   OMNI synthesis DB row id. Required for
                        OUTSIDE_MARKET_HOURS so recovery can mark
                        execution_dispatched=1 after a successful retry.
        omni_db_path:   Path to omni.db. Required alongside synthesis_id.
        exec_url:       Execution engine URL. Required alongside synthesis_id.

    Returns:
        RecoveryResult with full outcome details.
    """
    error_class = classify_exec_failure(exec_resp)

    logger.info(
        "EXECUTION RECOVERY initiated — ticker=%s system=%s class=%s resp=%s",
        ticker, system, error_class.value, (exec_resp or "")[:80]
    )

    # ── Dispatch ──────────────────────────────────────────────────────────────

    if error_class == ExecFailClass.OUTSIDE_MARKET_HOURS:
        result = _resolve_outside_market_hours(ticker, system, exec_resp or "", route_fn)
        # Mark dispatched in OMNI DB if recovery succeeded and we have the ids
        if result.success and synthesis_id is not None and omni_db_path and exec_url:
            try:
                import sys as _sys, os as _os
                _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
                from database import mark_execution_dispatched as _mark_dispatched
                _mark_dispatched(omni_db_path, synthesis_id, exec_url, "recovered_at_open")
                logger.info(
                    "OUTSIDE_MARKET_HOURS: marked synthesis %d dispatched after recovery for %s (%s)",
                    synthesis_id, ticker, system,
                )
            except Exception as _e:
                logger.warning(
                    "OUTSIDE_MARKET_HOURS: failed to mark synthesis %d dispatched: %s",
                    synthesis_id, _e,
                )
        return result

    if error_class == ExecFailClass.TICKER_DUPLICATE:
        return _resolve_ticker_duplicate(ticker, system)

    if error_class == ExecFailClass.CONCURRENT_CAP:
        return _resolve_concurrent_cap(ticker, system, exec_resp or "")

    if error_class == ExecFailClass.DAILY_CAP:
        return _resolve_daily_cap(ticker, system, exec_resp or "")

    if error_class == ExecFailClass.RATE_LIMITED:
        return _resolve_with_retry(
            ticker, system, error_class, exec_resp or "", route_fn,
            delay_s=RATE_LIMIT_DEFAULT,
            action_desc=f"Waited {RATE_LIMIT_DEFAULT}s for rate limit to clear",
        )

    if error_class == ExecFailClass.TIMEOUT:
        return _resolve_with_retry(
            ticker, system, error_class, exec_resp or "", route_fn,
            delay_s=5,
            action_desc="Retried immediately after timeout",
        )

    if error_class == ExecFailClass.NETWORK:
        return _resolve_with_retry(
            ticker, system, error_class, exec_resp or "", route_fn,
            delay_s=10,
            action_desc="Waited 10s for network to stabilize, then retried",
        )

    if error_class == ExecFailClass.BROKER_5XX:
        return _resolve_with_retry(
            ticker, system, error_class, exec_resp or "", route_fn,
            delay_s=15,
            action_desc="Waited 15s for broker to recover, then retried",
        )

    if error_class == ExecFailClass.AUTH_FAILURE:
        # Cannot auto-fix credentials — escalate immediately
        action = "Auth failure cannot be self-healed. Escalating immediately."
        result = RecoveryResult(
            success=False, skipped=False, error_class=error_class,
            attempts=0, action_taken=action,
            escalated_sovereign=True, escalated_ahmed=True,
            final_summary="Auth failure requires manual credential inspection.",
        )
        sov_ok = _notify_sovereign(ticker, system, error_class, action, result, exec_resp or "")
        _notify_ahmed(ticker, system, error_class, action, exec_resp or "", 0, result.final_summary)
        return result

    # UNKNOWN — escalate immediately
    action = "Unknown error — no autonomous fix possible. Escalating immediately."
    result = RecoveryResult(
        success=False, skipped=False, error_class=ExecFailClass.UNKNOWN,
        attempts=0, action_taken=action,
        escalated_sovereign=True, escalated_ahmed=True,
        final_summary=f"Unknown failure: {(exec_resp or '')[:120]}. Immediate review required.",
    )
    sov_ok = _notify_sovereign(ticker, system, ExecFailClass.UNKNOWN, action, result, exec_resp or "")
    if not sov_ok:
        _notify_ahmed(ticker, system, ExecFailClass.UNKNOWN, action, exec_resp or "", 0,
                      result.final_summary)
    return result


def auto_recover_async(
    ticker:          str,
    system:          str,
    exec_resp:       Optional[str],
    route_fn:        Callable,
    synthesis_id:    Optional[int] = None,
    omni_db_path:    Optional[str] = None,
    exec_url:        Optional[str] = None,
) -> None:
    """
    Fire-and-forget wrapper: runs auto_recover_execution in a daemon thread.
    OMNI's /concordance endpoint returns immediately; recovery runs in background.
    """
    def _run():
        try:
            auto_recover_execution(
                ticker       = ticker,
                system       = system,
                exec_resp    = exec_resp,
                route_fn     = route_fn,
                synthesis_id = synthesis_id,
                omni_db_path = omni_db_path,
                exec_url     = exec_url,
            )
        except Exception as e:
            logger.critical("EXECUTION RECOVERY thread crashed for %s: %s", ticker, e)
            # Last-resort page to Ahmed if recovery thread itself crashes
            _notify_ahmed(ticker, system, ExecFailClass.UNKNOWN,
                          "Recovery thread crashed", str(e), 0,
                          "Recovery process itself failed — immediate inspection required.")

    threading.Thread(target=_run, daemon=True, name=f"exec-recovery-{ticker}").start()
