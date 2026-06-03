"""
execution_healer.py — Auto-Heal Execution Recovery Loop (GAP-001)

Diagnoses fatal execution errors, attempts autonomous resolution per error class,
auto-resumes execution when possible, and notifies SOVEREIGN of every outcome.
Ahmed is only paged when the system cannot self-heal.

Error classes handled:
  AUTH_FAILURE       — 401/403: credentials invalid/expired → escalate (cannot auto-fix)
  ACCOUNT_BLOCKED    — 403 + account restriction: wait 60s + retry once, else escalate
  RATE_LIMITED       — 429: wait Retry-After (default 60s) → resume
  INVALID_PAYLOAD    — 422: skip ticker for session, execution continues immediately
  BROKER_UNAVAILABLE — 5xx: retry 3x with 30s backoff → resume or escalate
  TRANSIENT_NETWORK  — timeout/connection error: retry 2x with 15s backoff → resume or escalate
  UNKNOWN            — anything else: escalate immediately

Author: Cipher (GAP-001, 2026-04-27)
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import requests as _requests

logger = logging.getLogger("alpha_execution.healer")

ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
AHMED_CHAT_ID = os.environ.get("AHMED_CHAT_ID", "8573754783")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
NEXUS_BUS_URL = os.environ.get("NEXUS_BUS_URL", "http://192.168.1.141:9999")


# ── Error Classification ──────────────────────────────────────────────────────

class ExecutionErrorClass(Enum):
    AUTH_FAILURE       = "auth_failure"
    ACCOUNT_BLOCKED    = "account_blocked"
    RATE_LIMITED       = "rate_limited"
    INVALID_PAYLOAD    = "invalid_payload"
    BROKER_UNAVAILABLE = "broker_unavailable"
    TRANSIENT_NETWORK  = "transient_network"
    UNKNOWN            = "unknown"


@dataclass
class HealResult:
    resolved:              bool
    error_class:           ExecutionErrorClass
    action_taken:          str
    resume_approved:       bool
    escalate_to_sovereign: bool
    escalate_to_ahmed:     bool
    summary:               str


def classify_error(exc: Exception) -> ExecutionErrorClass:
    """
    Classify an execution exception into one of the 6 known error classes.
    Evaluated in strict priority order to avoid misclassification.
    """
    status = getattr(exc, "status_code", None)
    message = str(exc).lower()

    if status == 401:
        return ExecutionErrorClass.AUTH_FAILURE
    if status == 403:
        if "account" in message or "restricted" in message or "blocked" in message:
            return ExecutionErrorClass.ACCOUNT_BLOCKED
        return ExecutionErrorClass.AUTH_FAILURE
    if status == 429:
        return ExecutionErrorClass.RATE_LIMITED
    if status == 422:
        return ExecutionErrorClass.INVALID_PAYLOAD
    if status is not None and status >= 500:
        return ExecutionErrorClass.BROKER_UNAVAILABLE

    exc_type = type(exc).__name__
    if "Timeout" in exc_type or "ConnectionError" in exc_type or "timeout" in message or "connection" in message:
        return ExecutionErrorClass.TRANSIENT_NETWORK

    return ExecutionErrorClass.UNKNOWN


# ── Alpaca Probe ──────────────────────────────────────────────────────────────

def _probe_alpaca(api_key: str, api_secret: str) -> tuple[bool, Optional[str]]:
    """
    Probe Alpaca /v2/account to verify connectivity and auth.
    Returns (reachable, account_status_or_None).
    Never raises.
    """
    try:
        resp = _requests.get(
            f"{ALPACA_PAPER_URL}/v2/account",
            headers={
                "APCA-API-KEY-ID":     api_key,
                "APCA-API-SECRET-KEY": api_secret,
            },
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            return True, data.get("status", "ACTIVE")
        if resp.status_code in (401, 403):
            return False, None
        return True, None  # reachable but unexpected status
    except Exception as e:
        logger.warning("Alpaca probe failed: %s", e)
        return False, None


# ── Notifications ─────────────────────────────────────────────────────────────

def _notify_sovereign(result: HealResult, ticker: str, error_msg: str) -> None:
    """
    Send auto-heal outcome to SOVEREIGN via message bus.
    Fire-and-forget — never raises.
    """
    icon = "✅" if result.resolved else "🚨"
    status = "RESOLVED" if result.resolved else "ESCALATED"
    exec_status = "RESUMED" if result.resume_approved else "PAUSED (manual action required)"

    text = (
        f"🔧 ALPHA AUTO-HEAL — {status} {icon}\n"
        f"Ticker: {ticker}\n"
        f"Error class: {result.error_class.value}\n"
        f"Error: {error_msg[:200]}\n"
        f"Action taken: {result.action_taken}\n"
        f"Result: {result.summary}\n"
        f"Execution status: {exec_status}"
    )
    try:
        _requests.post(
            f"{NEXUS_BUS_URL}/send",
            json={"from": "alpha-execution", "to": "sovereign", "message": text},
            timeout=5,
        )
    except Exception as e:
        logger.warning("SOVEREIGN bus notification failed: %s", e)


def _notify_ahmed(result: HealResult, ticker: str, error_msg: str) -> None:
    """
    Page Ahmed via Telegram — only called when escalation is required.
    P1 fix: routes through alert_ahmed() with per-ticker+error-class dedup key.
    Prevents flooding Ahmed when healer retries the same error class repeatedly.
    Fire-and-forget — never raises.
    """
    text = (
        f"🚨 ALPHA EXECUTION — MANUAL ACTION REQUIRED\n"
        f"Ticker: {ticker}\n"
        f"Error class: {result.error_class.value}\n"
        f"Error: {error_msg[:200]}\n"
        f"What was tried: {result.action_taken}\n"
        f"Why it failed: {result.summary}\n"
        f"Execution is PAUSED. Resume via POST /resume after fixing the issue."
    )
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
        from shared.resilience.alerts import alert_ahmed as _aa
        _aa(
            text,
            key=f"alpha-healer-{ticker}-{result.error_class.value}",
            severity="CRITICAL",
        )
    except Exception as e:
        logger.warning("Ahmed alert_ahmed notification failed: %s", e)


# ── Resolution Handlers ───────────────────────────────────────────────────────

async def _resolve_auth_failure(exc: Exception, api_key: str, api_secret: str) -> HealResult:
    """401/403 — test Alpaca probe. Cannot auto-fix credentials → escalate."""
    reachable, _ = _probe_alpaca(api_key, api_secret)
    if reachable:
        # Reachable but still getting auth error — credentials are invalid
        action = "Probed Alpaca /v2/account — still returning auth error. Cannot auto-fix credentials."
    else:
        action = "Probed Alpaca /v2/account — unreachable. Credentials may be invalid or Alpaca is down."

    return HealResult(
        resolved=False,
        error_class=ExecutionErrorClass.AUTH_FAILURE,
        action_taken=action,
        resume_approved=False,
        escalate_to_sovereign=True,
        escalate_to_ahmed=True,
        summary="Auth failure cannot be auto-resolved. Credentials require manual inspection.",
    )


async def _resolve_account_blocked(exc: Exception, api_key: str, api_secret: str) -> HealResult:
    """403 account restriction — probe, wait 60s, retry once."""
    reachable, account_status = _probe_alpaca(api_key, api_secret)

    if account_status and account_status.upper() not in ("ACTIVE",):
        # Account has a non-active status — escalate
        return HealResult(
            resolved=False,
            error_class=ExecutionErrorClass.ACCOUNT_BLOCKED,
            action_taken=f"Probed Alpaca account — status: {account_status}. Non-active, cannot auto-recover.",
            resume_approved=False,
            escalate_to_sovereign=True,
            escalate_to_ahmed=True,
            summary=f"Account status is '{account_status}'. Manual intervention required.",
        )

    # Paper account throttle — wait 60s and retry probe
    logger.info("Account blocked — waiting 60s for paper account throttle to clear")
    await asyncio.sleep(60)
    reachable2, account_status2 = _probe_alpaca(api_key, api_secret)

    if reachable2 and (account_status2 or "").upper() in ("ACTIVE", ""):
        return HealResult(
            resolved=True,
            error_class=ExecutionErrorClass.ACCOUNT_BLOCKED,
            action_taken="Waited 60s for paper account throttle. Account now reachable.",
            resume_approved=True,
            escalate_to_sovereign=True,
            escalate_to_ahmed=False,
            summary="Paper account throttle cleared after 60s wait. Execution resumed.",
        )

    return HealResult(
        resolved=False,
        error_class=ExecutionErrorClass.ACCOUNT_BLOCKED,
        action_taken="Waited 60s — account still blocked after retry.",
        resume_approved=False,
        escalate_to_sovereign=True,
        escalate_to_ahmed=True,
        summary="Account still blocked after 60s. Manual intervention required.",
    )


async def _resolve_rate_limited(exc: Exception) -> HealResult:
    """429 — respect Retry-After header (default 60s) → resume."""
    retry_after = 60
    raw_header = getattr(exc, "retry_after", None)
    if raw_header:
        try:
            retry_after = int(raw_header)
        except (ValueError, TypeError):
            pass

    logger.info("Rate limited — waiting %ds (Retry-After)", retry_after)
    await asyncio.sleep(retry_after)

    return HealResult(
        resolved=True,
        error_class=ExecutionErrorClass.RATE_LIMITED,
        action_taken=f"Waited {retry_after}s per Retry-After header.",
        resume_approved=True,
        escalate_to_sovereign=True,
        escalate_to_ahmed=False,
        summary=f"Rate limit cleared after {retry_after}s. Execution resumed.",
    )


async def _resolve_invalid_payload(exc: Exception, ticker: str) -> HealResult:
    """422 — skip ticker for rest of session, do NOT pause execution."""
    return HealResult(
        resolved=True,
        error_class=ExecutionErrorClass.INVALID_PAYLOAD,
        action_taken=f"Skipped ticker {ticker} for remainder of session (invalid payload — 422).",
        resume_approved=True,
        escalate_to_sovereign=True,
        escalate_to_ahmed=False,
        summary=f"Invalid payload for {ticker}. Ticker skipped; execution continues for other tickers.",
    )


async def _resolve_broker_unavailable(exc: Exception, api_key: str, api_secret: str) -> HealResult:
    """5xx — retry 3x with 30s backoff → resume or escalate."""
    for attempt in range(1, 4):
        logger.info("Broker unavailable — retry %d/3 in 30s", attempt)
        await asyncio.sleep(30)
        reachable, _ = _probe_alpaca(api_key, api_secret)
        if reachable:
            return HealResult(
                resolved=True,
                error_class=ExecutionErrorClass.BROKER_UNAVAILABLE,
                action_taken=f"Retried {attempt}x with 30s backoff — Alpaca recovered.",
                resume_approved=True,
                escalate_to_sovereign=True,
                escalate_to_ahmed=False,
                summary=f"Alpaca recovered on retry {attempt}. Execution resumed.",
            )

    return HealResult(
        resolved=False,
        error_class=ExecutionErrorClass.BROKER_UNAVAILABLE,
        action_taken="Retried 3x with 30s backoff — Alpaca still unavailable.",
        resume_approved=False,
        escalate_to_sovereign=True,
        escalate_to_ahmed=True,
        summary="Alpaca still unavailable after 3 retries (90s). Manual intervention required.",
    )


async def _resolve_transient_network(exc: Exception, api_key: str, api_secret: str) -> HealResult:
    """Timeout/ConnectionError — retry 2x with 15s backoff → resume or escalate."""
    for attempt in range(1, 3):
        logger.info("Transient network error — retry %d/2 in 15s", attempt)
        await asyncio.sleep(15)
        reachable, _ = _probe_alpaca(api_key, api_secret)
        if reachable:
            return HealResult(
                resolved=True,
                error_class=ExecutionErrorClass.TRANSIENT_NETWORK,
                action_taken=f"Retried {attempt}x with 15s backoff — network recovered.",
                resume_approved=True,
                escalate_to_sovereign=True,
                escalate_to_ahmed=False,
                summary=f"Network recovered on retry {attempt}. Execution resumed.",
            )

    return HealResult(
        resolved=False,
        error_class=ExecutionErrorClass.TRANSIENT_NETWORK,
        action_taken="Retried 2x with 15s backoff — still unreachable.",
        resume_approved=False,
        escalate_to_sovereign=True,
        escalate_to_ahmed=True,
        summary="Network still unreachable after 2 retries (30s). Manual intervention required.",
    )


async def _resolve_unknown(exc: Exception) -> HealResult:
    """Unknown error — escalate immediately, no retry."""
    return HealResult(
        resolved=False,
        error_class=ExecutionErrorClass.UNKNOWN,
        action_taken="No retry attempted — unknown error class requires human diagnosis.",
        resume_approved=False,
        escalate_to_sovereign=True,
        escalate_to_ahmed=True,
        summary=f"Unknown error: {type(exc).__name__}: {str(exc)[:200]}. Immediate manual review required.",
    )


# ── Public API ────────────────────────────────────────────────────────────────

async def auto_heal_execution(
    exc: Exception,
    ticker: str,
    app_state: dict,
    state_lock,
    api_key: str,
    api_secret: str,
    skipped_tickers: set,
) -> HealResult:
    """
    Auto-diagnose and attempt autonomous resolution of a fatal execution error.

    Called immediately after execution_paused=True is set.
    On success: clears execution_paused and first_exec_failed via state_lock.
    On INVALID_PAYLOAD: does NOT set paused at all — just adds ticker to skipped set.
    On failure: leaves execution_paused=True and pages Ahmed.
    SOVEREIGN is notified of every outcome (success and failure).

    Args:
        exc:            The exception that triggered the pause.
        ticker:         Ticker symbol that caused the error.
        app_state:      The shared app_state dict from main.py.
        state_lock:     The threading.Lock protecting app_state.
        api_key:        Alpaca API key (for probing).
        api_secret:     Alpaca API secret (for probing).
        skipped_tickers: Mutable set — INVALID_PAYLOAD adds ticker here.

    Returns:
        HealResult with full outcome details.
    """
    error_msg = str(exc)
    error_class = classify_error(exc)

    logger.info(
        "AUTO-HEAL initiated — ticker=%s error_class=%s error=%s",
        ticker, error_class.value, error_msg[:100],
    )

    # ── Dispatch to resolution handler ──
    if error_class == ExecutionErrorClass.AUTH_FAILURE:
        result = await _resolve_auth_failure(exc, api_key, api_secret)

    elif error_class == ExecutionErrorClass.ACCOUNT_BLOCKED:
        result = await _resolve_account_blocked(exc, api_key, api_secret)

    elif error_class == ExecutionErrorClass.RATE_LIMITED:
        result = await _resolve_rate_limited(exc)

    elif error_class == ExecutionErrorClass.INVALID_PAYLOAD:
        result = await _resolve_invalid_payload(exc, ticker)

    elif error_class == ExecutionErrorClass.BROKER_UNAVAILABLE:
        result = await _resolve_broker_unavailable(exc, api_key, api_secret)

    elif error_class == ExecutionErrorClass.TRANSIENT_NETWORK:
        result = await _resolve_transient_network(exc, api_key, api_secret)

    else:
        result = await _resolve_unknown(exc)

    # ── Apply state changes ──
    if error_class == ExecutionErrorClass.INVALID_PAYLOAD:
        # Skip ticker only — do NOT touch execution_paused (it was never set for 422)
        # BUG-FIX (RE-001): main.py passes _skipped_tickers as a dict {ticker: datetime},
        # not a set. Calling .add() raised AttributeError on every 422, crashing the
        # healer thread silently and leaving the ticker unskipped.
        _ts = __import__("datetime").datetime.now(__import__("pytz").timezone("America/New_York"))
        if isinstance(skipped_tickers, dict):
            skipped_tickers[ticker.upper()] = _ts
        else:
            # Fallback for set-type callers (defensive)
            skipped_tickers.add(ticker.upper())  # type: ignore[attr-defined]
        logger.info("Ticker %s added to session skip list (healer: INVALID_PAYLOAD)", ticker)

    elif result.resume_approved:
        with state_lock:
            app_state["execution_paused"]  = False
            app_state["first_exec_failed"] = False
        logger.info(
            "AUTO-HEAL resolved — execution_paused cleared. Error class: %s",
            error_class.value,
        )
    else:
        logger.critical(
            "AUTO-HEAL failed — execution remains paused. Error class: %s. "
            "Escalating to %s.",
            error_class.value,
            "Ahmed + SOVEREIGN" if result.escalate_to_ahmed else "SOVEREIGN",
        )

    # ── Notify SOVEREIGN (always) ──
    _notify_sovereign(result, ticker, error_msg)

    # ── Notify Ahmed (only on escalation) ──
    if result.escalate_to_ahmed:
        _notify_ahmed(result, ticker, error_msg)

    return result
