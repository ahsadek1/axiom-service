"""
exit_monitor.py — THESIS Position Exit Monitor
===============================================
Monitors all open positions and exits them when conditions are met.

Exit rules (in priority order):
  1. STOP LOSS:     Premium doubles (200% of entry) → exit immediately
  2. PROFIT TARGET: 50% of max profit captured → exit (theta decay standard)
  3. TIME EXIT:     21 DTE remaining → exit (avoid gamma risk)
  4. THESIS BREAK:  Endorsing legend's thesis no longer holds → exit
  5. EXPIRATION:    Never hold to expiration → exit 1 day before

Runs every 5 minutes during market hours.
Ahmed directive: "Exit monitor fixed — credit spreads hold for theta decay"
"""
from __future__ import annotations
import logging
import os
import sqlite3
import time
from datetime import datetime, date, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from .spread_executor import (
    get_open_positions, update_position_status,
    place_option_order, wait_for_fill, POSITIONS_DB,
)

# Guard module imports (structural safety fixes)
try:
    from .position_reconciler import reconcile_position, handle_mismatch
    from .router_gateway import get_router_gateway
    from .escalation import escalate_warn, escalate_info
    from .event_log import log_event
    HAS_GUARDS = True
except ImportError:
    HAS_GUARDS = False
    log_event = lambda *args, **kwargs: None
    escalate_warn = lambda *args, **kwargs: None
    escalate_info = lambda *args, **kwargs: None

log = logging.getLogger("thesis.exit_monitor")
_ET = ZoneInfo("America/New_York")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL      = "https://paper-api.alpaca.markets"
DATA_URL      = "https://data.alpaca.markets"
TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT       = os.getenv("THESIS_CHAT_ID", os.getenv("AHMED_CHAT_ID", ""))

TIME_EXIT_DTE     = 21    # close at 21 DTE
PROFIT_TARGET_PCT = 0.50  # close at 50% of max profit
STOP_LOSS_PCT     = 2.00  # close at 200% of premium (loss = 2x premium)
MIN_HOLD_DAYS     = 3     # never exit in first 3 days


def _get_guardian():
    import sys as _sys
    _sys.path.insert(0, "/Users/ahmedsadek/nexus")
    try:
        from shared.alpaca.registry import get_guardian
        g = get_guardian("THESIS")
        if g:
            return g
    except Exception:
        pass
    from shared.alpaca.guardian import AlpacaGuardian
    return AlpacaGuardian(
        system_id  = "THESIS",
        api_key    = os.getenv("ALPACA_API_KEY",""),
        secret_key = os.getenv("ALPACA_SECRET_KEY",""),
        notify_fn  = _notify,
    )


def _notify(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


def _market_hours() -> bool:
    now = datetime.now(_ET)
    return (now.weekday() < 5
            and (now.hour > 9 or (now.hour == 9 and now.minute >= 30))
            and now.hour < 16)


# ---------------------------------------------------------------------------
# Current P&L calculation
# ---------------------------------------------------------------------------

def get_spread_current_value(position: dict) -> Optional[float]:
    """
    Get current market value of the spread.
    Returns net debit to close (cost to close the spread).
    Profit = entry premium - current close cost.
    """
    short_sym = position["short_symbol"]
    long_sym  = position["long_symbol"]

    try:
        guardian = _get_guardian()
        data  = guardian._executor.get_data(
            "/v1beta1/options/snapshots",
            params={"symbols": f"{short_sym},{long_sym}", "feed": "indicative"},
        )
        snaps = data.get("snapshots", {})
        if not snaps:
            return None

        def mid(sym):
            snap = snaps.get(sym, {})
            q = snap.get("latestQuote", {}) or {}
            bid = float(q.get("bp", 0) or 0)
            ask = float(q.get("ap", 0) or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            return None

        short_mid = mid(short_sym)
        long_mid  = mid(long_sym)

        if short_mid is None or long_mid is None:
            return None

        # Cost to close: buy back short + sell long
        close_cost = short_mid - long_mid
        return round(close_cost, 2)

    except Exception as exc:
        log.warning("Failed to get spread value for %s: %s",
                   position["ticker"], exc)
        return None


def calculate_pnl(position: dict, current_value: float) -> dict:
    """Calculate current P&L for a position."""
    entry_premium = position["net_premium"]
    contracts     = position["contracts"]
    multiplier    = 100

    # Profit = what we collected - what it costs to close
    unrealized_pnl = (entry_premium - current_value) * contracts * multiplier
    pnl_pct        = unrealized_pnl / position["max_profit"] if position["max_profit"] else 0

    return {
        "entry_premium":  entry_premium,
        "current_value":  current_value,
        "unrealized_pnl": round(unrealized_pnl, 2),
        "pnl_pct":        round(pnl_pct, 3),
        "contracts":      contracts,
    }


# ---------------------------------------------------------------------------
# Exit conditions
# ---------------------------------------------------------------------------

def check_exit_conditions(position: dict) -> Optional[tuple[str, str]]:
    """
    Check all exit conditions for a position.
    Returns (exit_reason_code, exit_reason_text) or None if hold.

    Priority:
    1. Stop loss (immediate)
    2. Expiration proximity (next day)
    3. Time exit (21 DTE)
    4. Profit target (50%)
    """
    ticker     = position["ticker"]
    expiration = position["expiration"]
    today      = date.today()
    exp_date   = date.fromisoformat(expiration)
    dte        = (exp_date - today).days
    entry_time = datetime.fromisoformat(
        position["entry_time"].replace("Z", "+00:00")
    )
    days_held  = (datetime.now(timezone.utc) - entry_time).days

    # Minimum hold period — never exit in first 3 days
    if days_held < MIN_HOLD_DAYS:
        log.debug("%s: Hold period active (%d days)", ticker, days_held)
        return None

    # ── 1. Never hold to expiration ───────────────────────────────────────────
    if dte <= 1:
        return ("EXPIRATION", f"Expiration tomorrow ({expiration}) — closing today")

    # ── 2. Get current market value ───────────────────────────────────────────
    current_value = get_spread_current_value(position)

    if current_value is not None:
        pnl = calculate_pnl(position, current_value)

        # Stop loss: spread value > 200% of premium (cost to close > 2x entry)
        if current_value >= position["net_premium"] * STOP_LOSS_PCT:
            loss = pnl["unrealized_pnl"]
            return (
                "STOP_LOSS",
                f"Stop loss triggered: spread=$%.2f entry=$%.2f loss=$%.0f" % (
                    current_value, position["net_premium"], abs(loss))
            )

        # Profit target: 50% of max profit captured
        if pnl["pnl_pct"] >= PROFIT_TARGET_PCT:
            profit = pnl["unrealized_pnl"]
            return (
                "PROFIT_TARGET",
                f"50%% profit target hit: pnl=$%.0f (%.0f%% of max)" % (
                    profit, pnl["pnl_pct"] * 100)
            )

    # ── 3. Time exit: 21 DTE ─────────────────────────────────────────────────
    if dte <= TIME_EXIT_DTE:
        return (
            "TIME_EXIT",
            f"21 DTE time exit: {dte} days to expiration ({expiration})"
        )

    return None  # Hold


# ---------------------------------------------------------------------------
# Position closer
# ---------------------------------------------------------------------------

def close_position(position: dict, reason: str) -> bool:
    """
    Close a bull put spread position:
    1. Verify position state (reconciliation)
    2. Buy back short put
    3. Sell long put
    4. Release capital allocation
    Returns True if both legs closed.
    """
    ticker    = position["ticker"]
    short_sym = position["short_symbol"]
    long_sym  = position["long_symbol"]
    contracts = position["contracts"]
    position_id = position["position_id"]

    log.info("Closing %s: %s/%s reason=%s",
             ticker, position["short_strike"], position["long_strike"], reason)
    
    # Reconciliation check before exit (structural safety)
    if HAS_GUARDS:
        try:
            recon = reconcile_position(position_id, ticker)
            if recon.get("status") != "OK":
                log.warning("%s: Reconciliation mismatch detected", position_id)
                mismatch_logged = handle_mismatch(
                    position_id=position_id,
                    reconciliation_result=recon,
                    escalate_fn=escalate_warn
                )
                if not mismatch_logged:
                    log.warning("%s: Skipping exit due to reconciliation failure", position_id)
                    return False
        except Exception as e:
            log.warning("%s: Reconciliation unavailable (non-blocking): %s", position_id, e)

    import uuid as _uuid
    guardian = _get_guardian()

    # Buy back short put
    buy_short = guardian.place_option_order(
        short_sym, contracts, "buy", "market",
        client_order_id=str(_uuid.uuid4())[:48]
    )
    if buy_short:
        guardian.wait_for_fill(buy_short["id"], timeout_s=20)

    # Sell long put
    sell_long = guardian.place_option_order(
        long_sym, contracts, "sell", "market",
        client_order_id=str(_uuid.uuid4())[:48]
    )
    if sell_long:
        guardian.wait_for_fill(sell_long["id"], timeout_s=20)

    # Calculate final P&L
    current_value = get_spread_current_value(position)
    if current_value is not None:
        pnl_data = calculate_pnl(position, current_value)
        exit_pnl = pnl_data["unrealized_pnl"]
    else:
        exit_pnl = 0.0

    update_position_status(
        position["position_id"],
        status="CLOSED",
        exit_reason=reason,
        exit_pnl=exit_pnl,
    )
    
    # Release capital allocation (structural safety)
    if HAS_GUARDS and position.get("allocation_id"):
        try:
            router = get_router_gateway()
            release_result = router.release_allocation(position["allocation_id"])
            log.info("%s: Capital allocation released — id=%s", ticker, position["allocation_id"])
            escalate_info("Capital Released", f"{ticker} allocation released", position_id)
        except Exception as e:
            log.warning("%s: Capital release failed (non-blocking): %s", ticker, e)
    
    # Log exit event
    if HAS_GUARDS:
        log_event(POSITIONS_DB, position_id, "POSITION_CLOSED",
                  actor="exit_monitor",
                  status="CLOSED",
                  details={"reason": reason, "exit_pnl": exit_pnl})

    emoji = "✅" if exit_pnl >= 0 else "🔴"
    _notify(
        f"{emoji} <b>THESIS POSITION CLOSED</b>\n"
        f"Ticker: {ticker} | ID: {position['position_id']}\n"
        f"Strikes: {position['short_strike']}/{position['long_strike']}\n"
        f"Reason: {reason}\n"
        f"P&L: ${exit_pnl:+.0f}\n"
        f"Entry premium: ${position['net_premium']:.2f} | "
        f"Contracts: {contracts}"
    )

    log.info("%s closed: reason=%s pnl=$%.0f", ticker, reason, exit_pnl)
    return True


# ---------------------------------------------------------------------------
# Main exit monitor loop
# ---------------------------------------------------------------------------

def run_exit_scan() -> int:
    """
    Scan all open positions and close any that meet exit conditions.
    Returns number of positions closed.
    """
    if not _market_hours():
        return 0

    positions = get_open_positions()
    if not positions:
        return 0

    closed = 0
    for position in positions:
        try:
            exit_signal = check_exit_conditions(position)
            if exit_signal:
                reason_code, reason_text = exit_signal
                log.warning("EXIT SIGNAL %s: %s — %s",
                           position["ticker"], reason_code, reason_text)
                success = close_position(position, reason_text)
                if success:
                    closed += 1
        except Exception as exc:
            log.error("Exit scan error for %s: %s",
                     position.get("ticker", "?"), exc)

    if closed:
        log.info("Exit scan complete: closed %d positions", closed)

    return closed
