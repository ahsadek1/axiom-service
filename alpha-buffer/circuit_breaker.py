"""
circuit_breaker.py — Alpha Buffer Circuit Breaker

Evaluates current trading state against approved thresholds.
State persisted to SQLite — survives restarts (not /tmp).
Three levels: AMBER → RED → STOP.

Thresholds approved by Ahmed Sadek — April 10, 2026.
See NEXUS_MASTER_ARCHITECTURE.md for full circuit breaker spec.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

# Module-level lock — serializes all circuit breaker reads and writes.
# Prevents concurrent trade outcomes from racing on the same state.
_CB_LOCK = threading.Lock()

from config import (
    CB_AMBER_CONSECUTIVE_LOSSES,
    CB_AMBER_DAILY_LOSS_PCT,
    CB_AMBER_ENTRIES_PER_DAY,
    CB_AMBER_VIX_THRESHOLD,
    CB_AMBER_WIN_RATE_FLOOR,
    CB_RED_CONSECUTIVE_LOSSES,
    CB_RED_DAILY_LOSS_PCT,
    CB_RED_WEEKLY_LOSS_PCT,
    CB_STOP_CONSECUTIVE_LOSSES,
    CB_STOP_DAILY_LOSS_PCT,
    CB_STOP_PORTFOLIO_LOSS_PCT,
    CB_STOP_VIX_THRESHOLD,
    CB_STOP_WEEKLY_LOSS_PCT,
    CB_STOP_WIN_RATE_FLOOR,
    MAX_NEW_POSITIONS_PER_DAY,
)
from database import get_circuit_breaker_state, update_circuit_breaker_state

logger = logging.getLogger("alpha_buffer.circuit_breaker")


@dataclass
class CircuitBreakerStatus:
    """Current circuit breaker evaluation result."""

    status:         str              # NORMAL, AMBER, RED, STOP
    can_trade:      bool
    entries_today:  int
    max_entries:    int
    triggers:       list[str]        # what tripped this level
    auto_resume:    bool             # True = auto-resumes; False = manual override required
    notes:          str

    def to_dict(self) -> dict:
        return {
            "status":        self.status,
            "can_trade":     self.can_trade,
            "entries_today": self.entries_today,
            "max_entries":   self.max_entries,
            "triggers":      self.triggers,
            "auto_resume":   self.auto_resume,
            "notes":         self.notes,
        }


def evaluate_circuit_breaker(
    db_path:     str,
    current_vix: Optional[float] = None,
) -> CircuitBreakerStatus:
    """
    Evaluate the current circuit breaker state and return trading permissions.

    Reads persisted state from SQLite. Does NOT modify state — call record_trade_result
    or update_circuit_breaker_state to make changes.
    Thread-safe: holds _CB_LOCK to ensure a consistent read snapshot.

    Args:
        db_path:     Path to Alpha Buffer database.
        current_vix: Current VIX level (optional, used for AMBER + STOP triggers).

    Returns:
        CircuitBreakerStatus with can_trade flag and full trigger list.
    """
    with _CB_LOCK:
        return _evaluate_circuit_breaker_locked(db_path, current_vix)


def _evaluate_circuit_breaker_locked(
    db_path:     str,
    current_vix: Optional[float] = None,
) -> CircuitBreakerStatus:
    """Inner implementation — must only be called while holding _CB_LOCK."""
    state   = get_circuit_breaker_state(db_path)
    today   = date.today().isoformat()
    triggers: list[str] = []

    # Manual STOP override — Ahmed must clear manually
    if state.get("manual_override"):
        return CircuitBreakerStatus(
            status        = "STOP",
            can_trade     = False,
            entries_today = state.get("daily_trades", 0),
            max_entries   = 0,
            triggers      = [f"MANUAL STOP: {state.get('manual_override_note', 'No note')}"],
            auto_resume   = False,
            notes         = "Manual override active. Ahmed must clear via /circuit-breaker/reset.",
        )

    # Reset daily counters if new trading day
    last_date = state.get("last_trade_date")
    if last_date and last_date != today:
        _reset_daily_counters(db_path, today)
        state = get_circuit_breaker_state(db_path)
        logger.info("Circuit breaker daily counters reset for %s", today)

    consec    = state.get("consecutive_losses", 0)
    daily_pnl = state.get("daily_pnl_pct", 0.0)
    weekly_pnl= state.get("weekly_pnl_pct", 0.0)
    portfolio = state.get("portfolio_pnl_pct", 0.0)
    daily_trades = state.get("daily_trades", 0)
    daily_wins   = state.get("daily_wins", 0)
    daily_total  = state.get("daily_trades", 0)
    win_rate     = (daily_wins / daily_total) if daily_total >= 5 else None

    # ── STOP — Full halt (manual resume) ─────────────────────────────────────
    stop_triggers: list[str] = []

    if consec >= CB_STOP_CONSECUTIVE_LOSSES:
        stop_triggers.append(f"{consec} consecutive losses (limit: {CB_STOP_CONSECUTIVE_LOSSES})")

    if daily_pnl <= -CB_STOP_DAILY_LOSS_PCT:
        stop_triggers.append(
            f"Daily loss {daily_pnl*100:.1f}% (limit: -{CB_STOP_DAILY_LOSS_PCT*100:.0f}%)"
        )

    if weekly_pnl <= -CB_STOP_WEEKLY_LOSS_PCT:
        stop_triggers.append(
            f"Weekly loss {weekly_pnl*100:.1f}% (limit: -{CB_STOP_WEEKLY_LOSS_PCT*100:.0f}%)"
        )

    if portfolio <= -CB_STOP_PORTFOLIO_LOSS_PCT:
        stop_triggers.append(
            f"Portfolio loss {portfolio*100:.1f}% (limit: -{CB_STOP_PORTFOLIO_LOSS_PCT*100:.0f}%)"
        )

    if current_vix and current_vix >= CB_STOP_VIX_THRESHOLD:
        stop_triggers.append(f"VIX {current_vix:.1f} ≥ {CB_STOP_VIX_THRESHOLD} (CRISIS)")

    if win_rate is not None and win_rate < CB_STOP_WIN_RATE_FLOOR:
        stop_triggers.append(
            f"Win rate {win_rate*100:.0f}% < {CB_STOP_WIN_RATE_FLOOR*100:.0f}% floor"
        )

    if stop_triggers:
        _update_status_if_upgraded(db_path, state, "STOP")
        # Cipher Pass 3 P3-13: STOP claimed auto_resume=False but never wrote
        # manual_override=1 to DB. _reset_daily_counters() clears consecutive_losses
        # and daily_pnl the next morning → STOP conditions vanish → status silently
        # downgraded to NORMAL. System auto-resumed despite claiming it wouldn't.
        # Fix: write manual_override=1 so the next evaluation hits the manual
        # override check first and returns STOP regardless of cleared triggers.
        # Portfolio STOP already persisted correctly (portfolio_pnl never resets daily).
        update_circuit_breaker_state(db_path, {
            "manual_override":      1,
            "manual_override_note": "; ".join(stop_triggers),
        })
        return CircuitBreakerStatus(
            status        = "STOP",
            can_trade     = False,
            entries_today = daily_trades,
            max_entries   = 0,
            triggers      = stop_triggers,
            auto_resume   = False,
            notes         = "FULL STOP — all new entries halted. Ahmed manual override required via /circuit-breaker/reset.",
        )

    # ── RED — Pause new entries (auto-resume next day) ───────────────────────
    red_triggers: list[str] = []

    if consec >= CB_RED_CONSECUTIVE_LOSSES:
        red_triggers.append(f"{consec} consecutive losses (limit: {CB_RED_CONSECUTIVE_LOSSES})")

    if daily_pnl <= -CB_RED_DAILY_LOSS_PCT:
        red_triggers.append(
            f"Daily loss {daily_pnl*100:.1f}% (limit: -{CB_RED_DAILY_LOSS_PCT*100:.0f}%)"
        )

    if weekly_pnl <= -CB_RED_WEEKLY_LOSS_PCT:
        red_triggers.append(
            f"Weekly loss {weekly_pnl*100:.1f}% (limit: -{CB_RED_WEEKLY_LOSS_PCT*100:.0f}%)"
        )

    if red_triggers:
        _update_status_if_upgraded(db_path, state, "RED")
        return CircuitBreakerStatus(
            status        = "RED",
            can_trade     = False,
            entries_today = daily_trades,
            max_entries   = 0,
            triggers      = red_triggers,
            auto_resume   = True,
            notes         = "RED — no new entries. Auto-resumes next trading day.",
        )

    # ── AMBER — Reduced entries, alert only ───────────────────────────────────
    amber_triggers: list[str] = []

    if consec >= CB_AMBER_CONSECUTIVE_LOSSES:
        amber_triggers.append(f"{consec} consecutive losses (limit: {CB_AMBER_CONSECUTIVE_LOSSES})")

    if daily_pnl <= -CB_AMBER_DAILY_LOSS_PCT:
        amber_triggers.append(
            f"Daily loss {daily_pnl*100:.1f}% (limit: -{CB_AMBER_DAILY_LOSS_PCT*100:.0f}%)"
        )

    if current_vix and current_vix >= CB_AMBER_VIX_THRESHOLD:
        amber_triggers.append(f"VIX {current_vix:.1f} ≥ {CB_AMBER_VIX_THRESHOLD}")

    if win_rate is not None and win_rate < CB_AMBER_WIN_RATE_FLOOR:
        amber_triggers.append(
            f"Win rate {win_rate*100:.0f}% < {CB_AMBER_WIN_RATE_FLOOR*100:.0f}%"
        )

    if amber_triggers:
        _update_status_if_upgraded(db_path, state, "AMBER")
        return CircuitBreakerStatus(
            status        = "AMBER",
            can_trade     = daily_trades < CB_AMBER_ENTRIES_PER_DAY,
            entries_today = daily_trades,
            max_entries   = CB_AMBER_ENTRIES_PER_DAY,
            triggers      = amber_triggers,
            auto_resume   = True,
            notes         = f"AMBER — max {CB_AMBER_ENTRIES_PER_DAY} entries/day. Auto-resumes.",
        )

    # ── NORMAL ────────────────────────────────────────────────────────────────
    _update_status_if_upgraded(db_path, state, "NORMAL")
    return CircuitBreakerStatus(
        status        = "NORMAL",
        can_trade     = daily_trades < MAX_NEW_POSITIONS_PER_DAY,
        entries_today = daily_trades,
        max_entries   = MAX_NEW_POSITIONS_PER_DAY,
        triggers      = [],
        auto_resume   = True,
        notes         = "NORMAL — all systems operational.",
    )


def record_trade_outcome(
    db_path:  str,
    won:      bool,
    pnl_pct:  float,
) -> None:
    """
    Update circuit breaker state after a trade closes.

    Updates consecutive loss counter, daily/weekly P&L.
    Resets consecutive loss counter on any win.
    Thread-safe: holds _CB_LOCK for the full read-modify-write cycle.

    Args:
        db_path:  Path to database.
        won:      True if the trade was a win.
        pnl_pct:  P&L as decimal (0.35 = +35%, -0.15 = -15%).
    """
    with _CB_LOCK:
      return _record_trade_outcome_locked(db_path, won, pnl_pct)


def _record_trade_outcome_locked(
    db_path:  str,
    won:      bool,
    pnl_pct:  float,
) -> None:
    """Inner implementation — must only be called while holding _CB_LOCK."""
    state   = get_circuit_breaker_state(db_path)
    today   = date.today().isoformat()
    updates: dict = {}

    # Reset daily counters if new day
    if state.get("last_trade_date") != today:
        updates["daily_trades"]  = 0
        updates["daily_wins"]    = 0
        updates["daily_losses"]  = 0
        updates["daily_pnl_pct"] = 0.0
        updates["last_trade_date"] = today

    # Apply updates to in-memory state for this call
    current_consec  = state.get("consecutive_losses", 0)
    current_daily   = updates.get("daily_pnl_pct", state.get("daily_pnl_pct", 0.0))
    current_weekly  = state.get("weekly_pnl_pct", 0.0)
    current_trades  = updates.get("daily_trades", state.get("daily_trades", 0))
    current_wins    = updates.get("daily_wins", state.get("daily_wins", 0))
    current_losses  = updates.get("daily_losses", state.get("daily_losses", 0))

    if won:
        updates["consecutive_losses"] = 0
        updates["daily_wins"]         = current_wins + 1
    else:
        updates["consecutive_losses"] = current_consec + 1
        updates["daily_losses"]       = current_losses + 1

    updates["daily_trades"]   = current_trades + 1
    updates["daily_pnl_pct"]  = current_daily + pnl_pct
    updates["weekly_pnl_pct"] = current_weekly + pnl_pct
    updates["last_trade_date"] = today

    update_circuit_breaker_state(db_path, updates)
    logger.info(
        "Trade outcome recorded: %s pnl=%.1f%% | consec_losses=%d",
        "WIN" if won else "LOSS",
        pnl_pct * 100,
        updates.get("consecutive_losses", current_consec),
    )


def reset_circuit_breaker(db_path: str, note: str = "Manual reset by Ahmed") -> None:
    """
    Manually reset the circuit breaker to NORMAL.

    Clears all counters and manual override flag.

    Args:
        db_path: Path to database.
        note:    Reason for manual reset (for audit).
    """
    update_circuit_breaker_state(db_path, {
        "status":               "NORMAL",
        "consecutive_losses":   0,
        "daily_trades":         0,
        "daily_wins":           0,
        "daily_losses":         0,
        "daily_pnl_pct":        0.0,
        "manual_override":      0,
        "manual_override_note": None,
    })
    logger.info("Circuit breaker RESET: %s", note)


def _reset_daily_counters(db_path: str, today: str) -> None:
    """Reset daily trade counters for a new trading day.

    OMNI C1 fix: also resets weekly P&L on Monday (or on first run of a new ISO week).
    Without this, weekly_pnl_pct accumulated indefinitely — one bad week permanently
    trips CB_STOP_WEEKLY_LOSS_PCT and halts the system until manual intervention.
    """
    from datetime import date as _date
    updates: dict = {
        "daily_trades":    0,
        "daily_wins":      0,
        "daily_losses":    0,
        "daily_pnl_pct":   0.0,
        "last_trade_date": today,
        # consecutive_losses and portfolio_pnl_pct intentionally preserved
    }

    # Weekly reset: check ISO (year, week) against last recorded reset
    iso = _date.today().isocalendar()
    current_week = f"{iso[0]}-{iso[1]}"
    try:
        state = get_circuit_breaker_state(db_path)
    except Exception:
        state = {}
    last_weekly = state.get("last_weekly_reset")

    if last_weekly != current_week:
        updates["weekly_pnl_pct"]     = 0.0
        updates["weekly_losses"]       = 0
        updates["last_weekly_reset"]   = current_week
        # Also downgrade STOP/RED status that was driven purely by weekly threshold
        if state.get("status") in ("STOP", "RED"):
            updates["status"] = "NORMAL"
        logger.info(
            "Weekly circuit breaker reset — new ISO week %s (was %s)",
            current_week, last_weekly or "never",
        )

    update_circuit_breaker_state(db_path, updates)


def _update_status_if_upgraded(db_path: str, state: dict, new_status: str) -> None:
    """
    Persist status change if it's a new/different status.

    Only upgrades — never writes NORMAL if current is AMBER/RED/STOP
    (downgrade happens naturally via daily counter reset).

    Args:
        db_path:    Path to database.
        state:      Current state dict.
        new_status: New status to write.
    """
    level_order = {"NORMAL": 0, "AMBER": 1, "RED": 2, "STOP": 3}
    current = state.get("status", "NORMAL")

    if level_order.get(new_status, 0) != level_order.get(current, 0):
        update_circuit_breaker_state(db_path, {"status": new_status})
        logger.info("Circuit breaker status: %s → %s", current, new_status)
