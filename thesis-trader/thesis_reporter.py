"""
thesis_reporter.py — THESIS Proactive Reporting System
=======================================================
Sends periodic trade updates and end-of-day summary to Telegram.

Schedule:
  Mid-session updates: 10:00, 12:00, 14:00 ET (trades from last 2 hours)
  End-of-day summary: 16:15 ET (all trades today)

All data comes directly from Alpaca — real fills, real premiums, real strikes.
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger("thesis.reporter")
_ET = ZoneInfo("America/New_York")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_URL    = "https://paper-api.alpaca.markets"
TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT       = os.getenv("THESIS_CHAT_ID", os.getenv("AHMED_CHAT_ID", ""))


# ---------------------------------------------------------------------------
# Alpaca helpers
# ---------------------------------------------------------------------------

def _alpaca_get(path: str) -> Optional[dict | list]:
    try:
        r = requests.get(
            f"{ALPACA_URL}{path}",
            headers={
                "APCA-API-KEY-ID": ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        log.warning("Alpaca GET %s failed: %s", path, exc)
    return None


def get_filled_orders(since_minutes: int = 120) -> list[dict]:
    """Get filled orders from the last N minutes."""
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    result = _alpaca_get(
        f"/v2/orders?status=filled&after={since.isoformat()}&limit=50&direction=desc"
    )
    if not isinstance(result, list):
        return []
    return result


def get_all_orders_today() -> list[dict]:
    """Get all filled orders since market open today."""
    now_et = datetime.now(_ET)
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    since = market_open.astimezone(timezone.utc)
    result = _alpaca_get(
        f"/v2/orders?status=filled&after={since.isoformat()}&limit=100&direction=asc"
    )
    if not isinstance(result, list):
        return []
    return result


def get_open_positions() -> list[dict]:
    result = _alpaca_get("/v2/positions")
    return result if isinstance(result, list) else []


def get_account() -> dict:
    result = _alpaca_get("/v2/account")
    return result if isinstance(result, dict) else {}


def format_order(order: dict) -> str:
    """Format a single filled order for Telegram."""
    symbol   = order.get("symbol", "???")
    side     = order.get("side", "").upper()
    qty      = order.get("filled_qty", order.get("qty", "?"))
    price    = order.get("filled_avg_price") or order.get("limit_price") or "?"
    notional = order.get("notional") or (
        float(qty) * float(price) if qty != "?" and price != "?" else None
    )
    filled_at = order.get("filled_at", "")
    if filled_at:
        try:
            dt = datetime.fromisoformat(filled_at.replace("Z", "+00:00"))
            filled_at = dt.astimezone(_ET).strftime("%H:%M ET")
        except Exception:
            filled_at = ""

    line = f"  {symbol} — {side} {qty} @ ${float(price):.2f}"
    if notional:
        line += f" | ${float(notional):,.0f}"
    if filled_at:
        line += f" | {filled_at}"
    return line


# ---------------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------------

def build_mid_session_report(screening_state: dict) -> str:
    """Build 2-hour update message."""
    now_et = datetime.now(_ET)
    orders = get_filled_orders(since_minutes=120)
    positions = get_open_positions()
    account = get_account()
    buying_power = float(account.get("buying_power", 0))

    regime = screening_state.get("regime", "NORMAL")
    vix    = screening_state.get("vix", 0)
    screenings = screening_state.get("screenings_today", 0)
    shortlist_size = screening_state.get("shortlist_size", 0)
    qualified = screening_state.get("last_screening", {}).get("qualified", 0)

    lines = [
        f"📊 <b>THESIS UPDATE — {now_et.strftime('%H:%M ET')}</b>",
        f"",
        f"Regime: {regime} | VIX: {vix:.1f}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if orders:
        lines.append(f"🎯 <b>TRADES LAST 2 HOURS ({len(orders)})</b>")
        lines.append("")
        for order in orders:
            lines.append(format_order(order))
        lines.append("")
    else:
        lines.append("🎯 <b>TRADES LAST 2 HOURS</b>")
        lines.append("  No fills in this period")
        lines.append("")

    lines += [
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📈 <b>SESSION STATUS</b>",
        f"Screenings: {screenings} | Last shortlist: {shortlist_size} tickers",
        f"Qualified (≥85%): {qualified}",
        f"Open positions: {len(positions)}",
        f"Buying power: ${buying_power:,.0f}",
    ]

    return "\n".join(lines)


def build_eod_report(screening_state: dict) -> str:
    """Build end-of-day summary message."""
    now_et = datetime.now(_ET)
    orders = get_all_orders_today()
    positions = get_open_positions()
    account = get_account()
    buying_power = float(account.get("buying_power", 0))

    screenings = screening_state.get("screenings_today", 0)

    # Calculate total notional
    total_notional = 0.0
    for order in orders:
        qty   = order.get("filled_qty", 0)
        price = order.get("filled_avg_price") or order.get("limit_price") or 0
        try:
            total_notional += float(qty) * float(price)
        except Exception:
            pass

    lines = [
        f"🏁 <b>THESIS END-OF-DAY — {now_et.strftime('%b %d, %Y')}</b>",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if orders:
        lines.append(f"🎯 <b>ALL TRADES TODAY ({len(orders)})</b>")
        lines.append("")
        for order in orders:
            lines.append(format_order(order))
        lines.append("")
        lines.append(f"Total notional: ${total_notional:,.0f}")
    else:
        lines.append("🎯 <b>ALL TRADES TODAY</b>")
        lines.append("  No fills today")

    lines += [
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>DAY SUMMARY</b>",
        f"Screenings completed: {screenings}/6",
        f"Open positions: {len(positions)}",
        f"Buying power: ${buying_power:,.0f}",
        f"",
        f"<i>Next screening: 08:45 ET tomorrow</i>",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Send helpers
# ---------------------------------------------------------------------------

def send_report(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        log.warning("Telegram not configured")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        log.info("Report sent to Telegram")
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)


def send_mid_session_report(screening_state: dict) -> None:
    msg = build_mid_session_report(screening_state)
    send_report(msg)


def send_eod_report(screening_state: dict) -> None:
    msg = build_eod_report(screening_state)
    send_report(msg)
