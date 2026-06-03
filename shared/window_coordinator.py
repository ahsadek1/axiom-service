"""
window_coordinator.py — Window mismatch detection and policy enforcement.

SW3 / G7: Window Mismatch Coordinator (GENESIS, April 19 2026)

Used by: alpha-buffer, prime-buffer (at pick submission time)
OMNI reads sizing_adjustment from the concordance result at synthesis time.

Staleness levels (Axiom pushes every 15 min):
  FRESH    (0 cycles): execute normally, sizing_adjustment=1.0
  STALE_1  (1 cycle / 15 min): sizing_adjustment=0.75, EXECUTE_REDUCED
  STALE_2  (2 cycles / 30 min): sizing_adjustment=0.50, EXECUTE_REDUCED
  STALE_3  (3 cycles / 45 min): HOLD, alert Ahmed
  EXPIRED  (4+ cycles / 60+ min): REJECT, alert Ahmed

Cipher fix (2026-04-22): widened window tolerance for earnings season.
Previously STALE_2 (30 min) triggered HOLD_FOR_REVIEW, deadlocking picks
before OMNI could cycle on high-activity days. Now STALE_2 executes at 0.50
sizing and HOLD threshold moved to 45 min. EXPIRED threshold moved to 60 min.

Window ID format: YYYY-MM-DD-HHMM  e.g. 2026-04-17-0915
Fail-open: if current_window_id is unreachable, treat pick as FRESH.
"""
import os
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

WINDOW_INTERVAL_MINUTES = 15  # Axiom push cadence


def _parse_window_ts(window_id: str) -> Optional[datetime]:
    """Parse window_id YYYY-MM-DD-HHMM to UTC datetime. Returns None on failure."""
    try:
        return datetime.strptime(window_id, "%Y-%m-%d-%H%M").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def cycles_stale(pick_window_id: str, current_window_id: str) -> int:
    """
    Return how many 15-min cycles old pick_window_id is vs current.

    Returns:
        0       if FRESH (same window or parse failure → fail-open)
        999     if pick_window_id is missing/empty (treat as EXPIRED)
        int > 0 if stale
    """
    if not pick_window_id:
        return 999  # missing = EXPIRED

    if pick_window_id == current_window_id:
        return 0

    pick_ts    = _parse_window_ts(pick_window_id)
    current_ts = _parse_window_ts(current_window_id)

    if not pick_ts or not current_ts:
        return 0  # parse failure = fail-open (FRESH)

    diff_minutes = (current_ts - pick_ts).total_seconds() / 60
    if diff_minutes <= 0:
        return 0  # pick is same or newer (race condition) → FRESH

    return int(diff_minutes // WINDOW_INTERVAL_MINUTES)


def evaluate_window(
    pick_window_id: str,
    current_window_id: str,
    ticker: str,
    agent: str,
) -> dict:
    """
    Evaluate window staleness and return the coordinator result dict.

    Args:
        pick_window_id:     window_id the agent used when analyzing the pick
        current_window_id:  current live Axiom window_id
        ticker:             underlying symbol
        agent:              agent name (for logging/alerting)

    Returns:
        dict with: status, pick_window_id, current_window_id, cycles_stale,
                   sizing_adjustment, action, alert_sent, note
    """
    n = cycles_stale(pick_window_id, current_window_id)

    if n == 0:
        return {
            "status":            "FRESH",
            "pick_window_id":    pick_window_id,
            "current_window_id": current_window_id,
            "cycles_stale":      0,
            "sizing_adjustment": 1.0,
            "action":            "EXECUTE",
            "alert_sent":        False,
            "note":              None,
        }

    if n == 1:
        logger.warning(
            "WINDOW STALE_1: %s/%s pick_window=%s current=%s — sizing 75%%",
            ticker, agent, pick_window_id, current_window_id,
        )
        return {
            "status":            "STALE_1",
            "pick_window_id":    pick_window_id,
            "current_window_id": current_window_id,
            "cycles_stale":      1,
            "sizing_adjustment": 0.75,
            "action":            "EXECUTE_REDUCED",
            "alert_sent":        False,
            "note":              "Pick analyzed against 15-min old pool. Reduced sizing applied.",
        }

    if n == 2:
        # Cipher fix: was HOLD_FOR_REVIEW — deadlocked earnings-season picks.
        # Now executes at 0.50 sizing. HOLD threshold moved to 3 cycles (45 min).
        logger.warning(
            "WINDOW STALE_2: %s/%s — 30 min stale. Executing at 50%% sizing.",
            ticker, agent,
        )
        return {
            "status":            "STALE_2",
            "pick_window_id":    pick_window_id,
            "current_window_id": current_window_id,
            "cycles_stale":      2,
            "sizing_adjustment": 0.50,
            "action":            "EXECUTE_REDUCED",
            "alert_sent":        False,
            "note":              "30 min stale. Executing at 50% sizing (earnings tolerance).",
        }

    if n == 3:
        # New: STALE_3 = 45 min — hold and alert (was previously EXPIRED threshold)
        logger.warning(
            "WINDOW STALE_3: %s/%s — 45 min stale. HOLDING for review.",
            ticker, agent,
        )
        _alert_ahmed(ticker, agent, pick_window_id, current_window_id, n, "STALE_3")
        return {
            "status":            "STALE_3",
            "pick_window_id":    pick_window_id,
            "current_window_id": current_window_id,
            "cycles_stale":      3,
            "sizing_adjustment": 0.50,
            "action":            "HOLD_FOR_REVIEW",
            "alert_sent":        True,
            "note":              "45 min stale. Held for manual review.",
        }

    # n >= 4: EXPIRED (Cipher fix: was 3+ cycles / 45 min, now 4+ cycles / 60 min)
    logger.error(
        "WINDOW EXPIRED: %s/%s — %d cycles stale. REJECTING.",
        ticker, agent, n,
    )
    _alert_ahmed(ticker, agent, pick_window_id, current_window_id, n, "EXPIRED")
    return {
        "status":            "EXPIRED",
        "pick_window_id":    pick_window_id,
        "current_window_id": current_window_id,
        "cycles_stale":      n,
        "sizing_adjustment": 0.0,
        "action":            "REJECTED",
        "alert_sent":        True,
        "note":              f"Pick is {n * 15} min stale. Rejected.",
    }


def _alert_ahmed(
    ticker: str,
    agent: str,
    pick_window_id: str,
    current_window_id: str,
    n: int,
    status: str,
) -> None:
    """Fire Telegram alert to Ahmed for STALE_2 or EXPIRED picks."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("AHMED_CHAT_ID", "8573754783")
    if not token:
        return
    emoji = "\u26a0\ufe0f" if status == "STALE_2" else "\U0001f6a8"
    action_note = "Held for manual review." if status == "STALE_2" else "REJECTED — not executed."
    text = (
        f"{emoji} WINDOW MISMATCH \u2014 {status}\n"
        f"Ticker: {ticker} | Agent: {agent}\n"
        f"Pick window: {pick_window_id} | Current: {current_window_id}\n"
        f"Pick is {n * 15} minutes stale.\n"
        f"{action_note}"
    )
    try:
        import requests as _req
        _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
    except Exception:
        pass


def apply_sizing_adjustment(sizing_mult: float, window_result: dict) -> float:
    """
    Apply window coordinator sizing adjustment multiplicatively to Axiom sizing_mult.

    Returns 0.0 if action is REJECTED. Never compounds with itself.

    Args:
        sizing_mult:    Base sizing multiplier from Axiom (e.g. 1.0)
        window_result:  Result dict from evaluate_window()

    Returns:
        Adjusted sizing multiplier (float, 0.0-1.0 range)
    """
    if window_result.get("action") == "REJECTED":
        return 0.0
    adjustment = window_result.get("sizing_adjustment", 1.0)
    return round(sizing_mult * adjustment, 4)
