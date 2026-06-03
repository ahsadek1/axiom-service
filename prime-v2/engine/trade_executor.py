"""
trade_executor.py — Prime V2 Trade Executor
=============================================
Places equity trades via Alpaca Guardian.
Handles entry, position registration, and DB tracking.

Entry logic:
  PRE_EARNINGS:   Buy at market open on pre_entry_date
  POST_EARNINGS:  Buy at 10:00 AM (let gap settle, confirm continuation)
  MOMENTUM:       Buy at market open or VWAP pullback
"""
from __future__ import annotations
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("prime_v2.executor")
_ET = ZoneInfo("America/New_York")

PRIME_V2_DB = os.getenv("PRIME_V2_DB",
              "/Users/ahmedsadek/nexus/data/prime_v2.db")
TG_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN","")
TG_CHAT     = os.getenv("AHMED_CHAT_ID","")
POLYGON_KEY = os.getenv("POLYGON_API_KEY","ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl")

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


def init_positions_db() -> None:
    """Initialize Prime V2 positions table."""
    conn = sqlite3.connect(PRIME_V2_DB, timeout=10)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS prime_v2_positions (
            position_id     TEXT PRIMARY KEY,
            ticker          TEXT NOT NULL,
            direction       TEXT NOT NULL,
            strategy        TEXT NOT NULL,
            tier            TEXT NOT NULL,
            conviction      REAL,
            shares          REAL NOT NULL,
            entry_price     REAL NOT NULL,
            position_size_usd REAL NOT NULL,
            alpaca_order_id TEXT,
            report_date     TEXT,
            pre_entry_date  TEXT,
            post_entry_date TEXT,
            trailing_high   REAL,
            status          TEXT DEFAULT 'OPEN',
            entry_time      TEXT NOT NULL,
            exit_time       TEXT,
            exit_reason     TEXT,
            exit_price      REAL,
            pnl_pct         REAL,
            pnl_usd         REAL,
            agent_count     INTEGER DEFAULT 2,
            agent_scores    TEXT,
            double_signal   INTEGER DEFAULT 0,
            ts              REAL
        );
        CREATE INDEX IF NOT EXISTS idx_pv2_status
            ON prime_v2_positions(status, ticker);
        CREATE TABLE IF NOT EXISTS prime_v2_trades_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id TEXT,
            ticker      TEXT,
            event       TEXT,
            detail      TEXT,
            ts          REAL
        );
    """)
    conn.commit()
    conn.close()


def is_already_positioned(ticker: str) -> bool:
    """Check if we already have an open position in this ticker."""
    try:
        conn = sqlite3.connect(PRIME_V2_DB, timeout=5)
        row  = conn.execute(
            "SELECT COUNT(*) FROM prime_v2_positions WHERE ticker=? AND status='OPEN'",
            (ticker,)
        ).fetchone()
        conn.close()
        return (row[0] if row else 0) > 0
    except Exception:
        return False


def get_current_price(ticker: str) -> Optional[float]:
    """Get current stock price."""
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{ticker}/trades/latest",
            headers={
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            params={"feed":"iex"},
            timeout=8,
        )
        if r.status_code == 200:
            price = float(r.json().get("trade",{}).get("p",0))
            if price > 0:
                return price
    except Exception:
        pass

    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev",
            params={"apiKey": POLYGON_KEY},
            timeout=8,
        )
        if r.status_code == 200:
            results = r.json().get("results",[])
            if results:
                return float(results[0]["c"])
    except Exception:
        pass
    return None


def get_vwap(ticker: str) -> Optional[float]:
    """Get current VWAP from Alpaca snapshot."""
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/snapshots",
            headers={
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            params={"symbols": ticker, "feed":"iex"},
            timeout=8,
        )
        if r.status_code == 200:
            snap = r.json().get(ticker,{})
            return float(snap.get("dailyBar",{}).get("vw",0)) or None
    except Exception:
        pass
    return None


def check_post_earnings_confirmation(
    ticker: str,
    entry_price: float,
) -> tuple[bool, str]:
    """
    For post-earnings entries: confirm continuation at 10 AM.
    Rules:
    - Price must be holding above gap open (no gap fill)
    - Volume must be running above average
    - Price should be near or above VWAP
    """
    now    = datetime.now(_ET)
    current = get_current_price(ticker)
    vwap    = get_vwap(ticker)

    if not current:
        return False, "Cannot get current price"

    # Price still above gap (entry_price is approximate gap level)
    if current < entry_price * 0.97:
        return False, f"Gap filling — price {current:.2f} below gap {entry_price:.2f}"

    # Price near or above VWAP
    if vwap and current < vwap * 0.995:
        return False, f"Price {current:.2f} below VWAP {vwap:.2f} — weak"

    return True, f"Continuation confirmed: price={current:.2f} vwap={vwap or 0:.2f}"


def place_equity_order(
    ticker:    str,
    side:      str,
    shares:    float,
    client_id: str,
) -> Optional[dict]:
    """Place equity order via Alpaca Guardian."""
    try:
        import sys as _sys
        _sys.path.insert(0, "/Users/ahmedsadek/nexus")
        from shared.alpaca.registry import get_guardian
        guardian = get_guardian("PRIME")
        if guardian:
            order = guardian.place_order(
                symbol          = ticker,
                qty             = int(shares),
                side            = side,
                order_type      = "market",
                client_order_id = client_id,
            )
            return order
    except Exception as exc:
        log.error("Guardian order failed for %s: %s", ticker, exc)

    # Fallback: direct Alpaca
    try:
        r = requests.post(
            f"{ALPACA_URL}/v2/orders",
            headers={
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            json={
                "symbol":          ticker,
                "qty":             str(int(shares)),
                "side":            side,
                "type":            "market",
                "time_in_force":   "day",
                "client_order_id": client_id,
            },
            timeout=10,
        )
        if r.status_code in (200,201):
            return r.json()
        log.error("Direct order failed: %d %s", r.status_code, r.text[:100])
    except Exception as exc:
        log.error("Direct order exception: %s", exc)
    return None


def execute_trade(opportunity: dict, size_result: dict) -> Optional[dict]:
    """
    Execute a trade for a qualified opportunity.

    Args:
        opportunity:  Dict from opportunity_ranker with all trade parameters
        size_result:  Dict from position_sizer with approved size

    Returns:
        Position dict if successful, None if failed.
    """
    init_positions_db()

    ticker    = opportunity["ticker"]
    direction = opportunity.get("direction","bullish")
    strategy  = opportunity.get("strategy","MOMENTUM_BREAKOUT")
    tier      = opportunity.get("tier","TIER_B")
    conviction = opportunity.get("conviction",70)
    size_usd  = size_result["position_size_usd"]

    # Duplicate check
    if is_already_positioned(ticker):
        log.info("Already positioned in %s — skip", ticker)
        return None

    # Get current price
    price = get_current_price(ticker)
    if not price:
        log.error("Cannot get price for %s — skip", ticker)
        return None

    # Post-earnings confirmation check (wait for 10 AM)
    if strategy == "POST_EARNINGS":
        now = datetime.now(_ET)
        if now.hour < 10:
            log.info("%s: POST_EARNINGS — waiting for 10 AM confirmation", ticker)
            return None
        confirmed, reason = check_post_earnings_confirmation(ticker, price)
        if not confirmed:
            log.info("%s: POST_EARNINGS confirmation failed: %s", ticker, reason)
            return None
        log.info("%s: POST_EARNINGS confirmed: %s", ticker, reason)

    # Calculate shares
    shares = max(1, int(size_usd / price))
    actual_size = shares * price

    # Place order
    position_id = str(uuid.uuid4())[:12]
    client_id   = f"pv2-{position_id}"
    side        = "buy" if direction == "bullish" else "sell"

    order = place_equity_order(ticker, side, shares, client_id)
    if not order:
        log.error("Order failed for %s", ticker)
        return None

    order_id = order.get("id","unknown")
    log.info("Order placed: %s %s %d shares @ ~$%.2f order_id=%s",
             side.upper(), ticker, shares, price, order_id[:8])

    # Wait briefly for fill
    time.sleep(3)

    # Save position to DB
    now    = datetime.now(timezone.utc).isoformat()
    import json as _json

    conn = sqlite3.connect(PRIME_V2_DB, timeout=10)
    conn.execute("""
        INSERT INTO prime_v2_positions
        (position_id, ticker, direction, strategy, tier, conviction,
         shares, entry_price, position_size_usd, alpaca_order_id,
         report_date, pre_entry_date, post_entry_date,
         trailing_high, status, entry_time, agent_count,
         double_signal, ts)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        position_id, ticker, direction, strategy, tier, conviction,
        shares, price, actual_size, order_id,
        opportunity.get("report_date"),
        opportunity.get("pre_entry_date"),
        opportunity.get("post_entry_date"),
        price,  # trailing_high starts at entry
        "OPEN", now,
        opportunity.get("agent_count",2),
        int(bool(opportunity.get("double_signal",False))),
        time.time(),
    ))
    conn.commit()
    conn.close()

    position = {
        "position_id":     position_id,
        "ticker":          ticker,
        "direction":       direction,
        "strategy":        strategy,
        "tier":            tier,
        "conviction":      conviction,
        "shares":          shares,
        "entry_price":     price,
        "position_size_usd": actual_size,
        "alpaca_order_id": order_id,
    }

    _notify(
        f"<b>PRIME V2 ENTRY: {ticker}</b>\n"
        f"Strategy: {strategy} | Tier: {tier}\n"
        f"Direction: {direction.upper()}\n"
        f"Shares: {shares} @ ${price:.2f}\n"
        f"Size: ${actual_size:,.0f} | Conviction: {conviction:.0f}\n"
        f"Double signal: {'YES ★' if opportunity.get('double_signal') else 'No'}\n"
        f"Order: {order_id[:12]}"
    )

    return position
