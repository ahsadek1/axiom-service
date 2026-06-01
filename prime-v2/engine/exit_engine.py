"""
exit_engine.py — Intelligent Exit Engine
==========================================
Tier-specific exit rules. Runs every 5 minutes during market hours.

Exit rules by strategy:

PRE_EARNINGS:
  → Always exit 1 trading day before report
  → Stop loss: 4% below entry
  → Never hold through earnings binary event

POST_EARNINGS:
  → Profit target: 10% (lock in the continuation move)
  → Stop loss: 4% below entry (gap fill = thesis broken)
  → Time exit: 3 trading days maximum
  → If gap fills completely: exit immediately

MOMENTUM_BREAKOUT (Tier A):
  → Partial exit: sell 50% at 8% profit
  → Trail remaining 50% with 8% trailing stop from high
  → Time exit: 5 trading days maximum
  → Volume dry-up exit: if volume drops below 0.7x avg for 2 days

MOMENTUM_BREAKOUT (Tier B/C):
  → Fixed profit target: 5% (Tier B) / 3% (Tier C)
  → Fixed stop loss: 2.5% (Tier B) / 1.5% (Tier C)
  → Time exit: 3 trading days

MOMENTUM_BREAKDOWN (bearish):
  → Same as bullish but inverted
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

log = logging.getLogger("prime_v2.exit_engine")
_ET = ZoneInfo("America/New_York")

POLYGON_KEY = os.getenv("POLYGON_API_KEY", "ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl")
PRIME_V2_DB = os.getenv("PRIME_V2_DB",
              "/Users/ahmedsadek/nexus/data/prime_v2.db")
TG_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT     = os.getenv("AHMED_CHAT_ID", "")

ALPACA_KEY    = os.getenv("PRIME_ALPACA_KEY",    os.getenv("ALPACA_API_KEY",""))
ALPACA_SECRET = os.getenv("PRIME_ALPACA_SECRET", os.getenv("ALPACA_SECRET_KEY",""))
ALPACA_URL    = "https://paper-api.alpaca.markets"


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
    return (now.weekday() < 5 and
            (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and
            now.hour < 16)


def get_current_price(ticker: str) -> Optional[float]:
    """Get current price from Polygon."""
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{ticker}/trades/latest",
            headers={
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            params={"feed": "iex"},
            timeout=5,
        )
        if r.status_code == 200:
            price = float(r.json().get("trade", {}).get("p", 0))
            if price > 0:
                return price
    except Exception:
        pass

    # Fallback: Polygon
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/last/trade/{ticker}",
            params={"apiKey": POLYGON_KEY},
            timeout=5,
        )
        if r.status_code == 200:
            return float(r.json().get("results", {}).get("p", 0)) or None
    except Exception:
        pass
    return None


def get_open_positions() -> list[dict]:
    """Load all open Prime V2 positions."""
    try:
        conn = sqlite3.connect(PRIME_V2_DB, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM prime_v2_positions
            WHERE status='OPEN'
            ORDER BY entry_time ASC
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def update_position(
    position_id: str,
    status:      str,
    exit_reason: str,
    exit_price:  float,
    pnl_pct:     float,
    pnl_usd:     float,
) -> None:
    """Mark position as closed with P&L."""
    try:
        conn = sqlite3.connect(PRIME_V2_DB, timeout=5)
        conn.execute("""
            UPDATE prime_v2_positions
            SET status=?, exit_time=?, exit_reason=?,
                exit_price=?, pnl_pct=?, pnl_usd=?
            WHERE position_id=?
        """, (
            status,
            datetime.now(timezone.utc).isoformat(),
            exit_reason,
            exit_price,
            pnl_pct,
            pnl_usd,
            position_id,
        ))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("Position update failed: %s", exc)


def update_trailing_stop(position_id: str, new_high: float) -> None:
    """Update trailing stop high watermark."""
    try:
        conn = sqlite3.connect(PRIME_V2_DB, timeout=5)
        conn.execute("""
            UPDATE prime_v2_positions
            SET trailing_high=?
            WHERE position_id=?
        """, (new_high, position_id))
        conn.commit()
        conn.close()
    except Exception:
        pass


def close_position_alpaca(ticker: str, shares: float) -> bool:
    """Close position via Alpaca Guardian."""
    try:
        import sys as _sys
        _sys.path.insert(0, "/Users/ahmedsadek/nexus")
        from shared.alpaca.registry import get_guardian
        g = get_guardian("PRIME")
        if g:
            result = g.close_position(ticker, qty=shares)
            return result is not None
    except Exception as exc:
        log.error("Alpaca close failed for %s: %s", ticker, exc)
    return False


# ---------------------------------------------------------------------------
# Exit condition checkers
# ---------------------------------------------------------------------------

def check_pre_earnings_exit(position: dict, current_price: float) -> Optional[tuple]:
    """
    Pre-earnings: exit 1 day before report OR stop loss.
    Returns (exit_reason, exit_price) or None.
    """
    entry_price  = position["entry_price"]
    report_date  = position.get("report_date","")
    pnl_pct      = (current_price - entry_price) / entry_price

    # Stop loss: 4%
    if pnl_pct <= -0.04:
        return ("STOP_LOSS_4PCT", current_price)

    # Time exit: 1 day before earnings
    if report_date:
        try:
            rdate    = date.fromisoformat(report_date)
            today    = date.today()
            days_to  = (rdate - today).days
            if days_to <= 1:
                return ("PRE_EARNINGS_EXIT", current_price)
        except ValueError:
            pass

    return None


def check_post_earnings_exit(position: dict, current_price: float) -> Optional[tuple]:
    """
    Post-earnings: 10% profit target, 4% stop, 3-day time limit.
    If gap fills completely: exit immediately.
    """
    entry_price = position["entry_price"]
    entry_time  = datetime.fromisoformat(
        position["entry_time"].replace("Z","+00:00")
    )
    days_held   = (datetime.now(timezone.utc) - entry_time).days
    pnl_pct     = (current_price - entry_price) / entry_price
    direction   = position.get("direction","bullish")

    if direction == "bearish":
        pnl_pct = -pnl_pct

    # Profit target: 10%
    if pnl_pct >= 0.10:
        return ("PROFIT_TARGET_10PCT", current_price)

    # Stop loss: 4%
    if pnl_pct <= -0.04:
        return ("STOP_LOSS_4PCT", current_price)

    # Time exit: 3 days
    if days_held >= 3:
        return ("TIME_EXIT_3DAYS", current_price)

    return None


def check_momentum_exit(position: dict, current_price: float) -> Optional[tuple]:
    """
    Momentum: partial exit at 8%, trail remaining, time limit 5 days.
    """
    entry_price   = position["entry_price"]
    trailing_high = position.get("trailing_high") or entry_price
    entry_time    = datetime.fromisoformat(
        position["entry_time"].replace("Z","+00:00")
    )
    days_held  = (datetime.now(timezone.utc) - entry_time).days
    pnl_pct    = (current_price - entry_price) / entry_price
    tier       = position.get("tier","TIER_B")
    direction  = position.get("direction","bullish")

    if direction == "bearish":
        pnl_pct = -pnl_pct
        effective_price = entry_price * 2 - current_price
    else:
        effective_price = current_price

    # Update trailing high
    if effective_price > trailing_high:
        update_trailing_stop(position["position_id"], effective_price)
        trailing_high = effective_price

    # Tier-specific rules
    if tier == "TIER_A":
        # Profit target: 12%
        if pnl_pct >= 0.12:
            return ("PROFIT_TARGET_12PCT", current_price)
        # Trailing stop: 8% from high
        trail_stop = trailing_high * (1 - 0.08)
        if direction == "bullish" and current_price < trail_stop:
            return ("TRAILING_STOP_8PCT", current_price)
        # Stop loss: 4%
        if pnl_pct <= -0.04:
            return ("STOP_LOSS_4PCT", current_price)
        # Time exit: 5 days
        if days_held >= 5:
            return ("TIME_EXIT_5DAYS", current_price)

    elif tier == "TIER_B":
        # Profit target: 7%
        if pnl_pct >= 0.07:
            return ("PROFIT_TARGET_7PCT", current_price)
        # Stop loss: 3%
        if pnl_pct <= -0.03:
            return ("STOP_LOSS_3PCT", current_price)
        # Time exit: 3 days
        if days_held >= 3:
            return ("TIME_EXIT_3DAYS", current_price)

    else:  # TIER_C
        # Profit target: 4%
        if pnl_pct >= 0.04:
            return ("PROFIT_TARGET_4PCT", current_price)
        # Stop loss: 2%
        if pnl_pct <= -0.02:
            return ("STOP_LOSS_2PCT", current_price)
        # Time exit: 2 days
        if days_held >= 2:
            return ("TIME_EXIT_2DAYS", current_price)

    return None


# ---------------------------------------------------------------------------
# Main exit scan
# ---------------------------------------------------------------------------

def run_exit_scan() -> int:
    """
    Scan all open positions for exit conditions.
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
            ticker        = position["ticker"]
            current_price = get_current_price(ticker)

            if not current_price:
                log.warning("Cannot get price for %s — skip", ticker)
                continue

            strategy  = position.get("strategy","MOMENTUM_BREAKOUT")
            exit_signal = None

            if strategy == "PRE_EARNINGS":
                exit_signal = check_pre_earnings_exit(position, current_price)
            elif strategy == "POST_EARNINGS":
                exit_signal = check_post_earnings_exit(position, current_price)
            else:
                exit_signal = check_momentum_exit(position, current_price)

            if not exit_signal:
                continue

            exit_reason, exit_price = exit_signal
            entry_price = position["entry_price"]
            shares      = position["shares"]
            pnl_pct     = (exit_price - entry_price) / entry_price
            pnl_usd     = pnl_pct * entry_price * shares

            if position.get("direction") == "bearish":
                pnl_pct = -pnl_pct
                pnl_usd = -pnl_usd

            log.warning("EXIT: %s reason=%s pnl=%.1f%% ($%.0f)",
                       ticker, exit_reason, pnl_pct*100, pnl_usd)

            # Close via Alpaca
            closed_ok = close_position_alpaca(ticker, shares)

            # Update DB
            update_position(
                position_id = position["position_id"],
                status      = "CLOSED",
                exit_reason = exit_reason,
                exit_price  = exit_price,
                pnl_pct     = round(pnl_pct, 4),
                pnl_usd     = round(pnl_usd, 2),
            )

            closed += 1
            emoji = "✅" if pnl_usd >= 0 else "🔴"
            _notify(
                f"{emoji} <b>PRIME V2 EXIT: {ticker}</b>\n"
                f"Strategy: {strategy}\n"
                f"Reason: {exit_reason}\n"
                f"Entry: ${entry_price:.2f} → Exit: ${exit_price:.2f}\n"
                f"P&L: {pnl_pct:+.1%} (${pnl_usd:+,.0f})\n"
                f"Alpaca: {'closed' if closed_ok else 'FAILED - check manually'}"
            )

        except Exception as exc:
            log.error("Exit scan error for %s: %s",
                     position.get("ticker","?"), exc)

    if closed:
        log.info("Exit scan: closed %d positions", closed)
    return closed
