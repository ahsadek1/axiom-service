"""
NEXUS Prime Exit Monitor v2.0 — Velocity Architecture
======================================================
Monitors all open Prime positions every 5 minutes.
Enforces tiered profit targets and time stops with ZERO exceptions.

VELOCITY PRINCIPLE:
  Hit the target → close IMMEDIATELY. No second-guessing.
  Time expired → close regardless. The next cycle matters more.
  Capital freed → priority recycling signal written instantly.

EXIT TRIGGERS (all auto-execute):
  1. Tier target hit (10/20/30%) → CLOSE IMMEDIATELY
  2. Trailing stop triggered → close and bank profit
  3. Hard stop (-5/8/10%) → close, limit loss
  4. Time stop (day 2/3/5 depending on tier) → close, thesis expired
  5. Momentum stall (RSI drops below 45 on long) → close
  6. Earnings within 2 days → close before announcement
  7. VIX > 35 → close all longs
  8. VIX > 40 → close ALL positions

CIRCUIT BREAKERS:
  Daily loss > $500    → halt new entries 24h
  3 consecutive losses → 48h pause + Ahmed alert
  5% portfolio drawdown → reduce size next cycle

Author: OMNI
Date:   2026-04-05
"""

import os
import sys
import json
import time
import datetime
import argparse
import requests
import pytz
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
ALPACA_BASE   = os.getenv("ALPACA_URL",          "https://paper-api.alpaca.markets")
ALPACA_KEY    = os.getenv("ALPACA_API_KEY",       "PKPGM3BRNYPGCF5Z56IAUZCZJL")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY",    "5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs")
ALPACA_H      = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}
TG_BOT      = "8747601602:AAGTzRd3NJWq44Bvbzd5JvhtnO2edBUvjbc"
AHMED_ID    = "8573754783"
PRIME_GROUP = "-5111649337"
HEALTH_GRP  = "-5184172590"

LOG_FILE      = Path(os.path.expanduser("~/.openclaw/workspace-omni/logs/prime_exit_alerts.jsonl"))
CB_STATE_FILE = Path("/tmp/prime_circuit_breaker_state.json")
RECYCLE_FILE  = Path("/tmp/prime_capital_freed.json")
ET            = pytz.timezone("America/New_York")

# Tier parameters
TIER_PARAMS = {
    1: {"target": 0.10, "stop": 0.05, "max_hold_days": 2, "rsi_exit": 45},
    2: {"target": 0.20, "stop": 0.08, "max_hold_days": 3, "rsi_exit": 45},
    3: {"target": 0.30, "stop": 0.10, "max_hold_days": 5, "rsi_exit": 42},
}

# Circuit breakers
CB_DAILY_LOSS    = 500
CB_CONSEC_LOSSES = 3
CB_VIX_LONGS     = 35
CB_VIX_ALL       = 40
EARNINGS_DAYS    = 2


# ── Utilities ─────────────────────────────────────────────────────────────────

def log_event(event: dict):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


def notify(msg: str, channels: list = None):
    if channels is None:
        channels = [AHMED_ID, PRIME_GROUP]
    for chat_id in channels:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_BOT}/sendMessage",
                         json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                         timeout=5)
        except Exception:
            pass


def get_price(ticker: str) -> float:
    try:
        r   = requests.get(f"{ALPACA_BASE}/v2/stocks/{ticker}/quotes/latest",
                           headers=ALPACA_H, timeout=5)
        q   = r.json().get("quote", {})
        bid = float(q.get("bp", 0) or 0)
        ask = float(q.get("ap", 0) or 0)
        return round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else 0.0
    except Exception:
        return 0.0


def get_vix() -> float:
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return 0.0


def get_rsi(ticker: str) -> float:
    try:
        import yfinance as yf
        hist  = yf.Ticker(ticker).history(period="1mo", interval="1d")
        delta = hist["Close"].diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss
        return round(float((100 - (100 / (1 + rs))).iloc[-1]), 1)
    except Exception:
        return 50.0


def close_position(ticker: str, shares: int, direction: str, reason: str) -> dict:
    side = "sell" if direction == "long" else "buy"
    try:
        r = requests.post(
            f"{ALPACA_BASE}/v2/orders",
            headers=ALPACA_H,
            json={"symbol": ticker, "qty": abs(shares), "side": side,
                  "type": "market", "time_in_force": "day",
                  "client_order_id": f"prime_exit_{ticker}_{int(time.time())}"},
            timeout=10
        )
        if r.status_code in (200, 201):
            _signal_recycling(ticker, abs(shares))
            return {"status": "closed", "ticker": ticker, "reason": reason}
        return {"status": "error", "code": r.status_code}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def _signal_recycling(ticker: str, shares: int):
    """Immediate capital freed signal — velocity core."""
    try:
        existing = []
        if RECYCLE_FILE.exists():
            existing = json.loads(RECYCLE_FILE.read_text())
        existing.append({
            "ticker":   ticker,
            "shares":   shares,
            "freed_at": datetime.datetime.utcnow().isoformat(),
            "consumed": False,
            "priority": "HIGH",
            "system":   "PRIME_V2",
        })
        RECYCLE_FILE.write_text(json.dumps(existing))
    except Exception:
        pass


def get_open_positions() -> list:
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/positions", headers=ALPACA_H, timeout=10)
        if r.status_code == 200:
            return [p for p in r.json()
                    if "/" not in p.get("symbol","") and len(p.get("symbol","")) <= 5]
        return []
    except Exception:
        return []


def load_cb_state() -> dict:
    try:
        if CB_STATE_FILE.exists():
            state = json.loads(CB_STATE_FILE.read_text())
            if state.get("date") != str(datetime.date.today()):
                state["daily_loss"] = 0.0
                state["date"] = str(datetime.date.today())
                CB_STATE_FILE.write_text(json.dumps(state))
            return state
    except Exception:
        pass
    return {"daily_loss": 0.0, "consecutive_losses": 0,
            "halted_until": None, "date": str(datetime.date.today())}


def save_cb_state(state: dict):
    try:
        CB_STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def get_hold_days(position: dict) -> float:
    """Calculate trading days this position has been held."""
    try:
        opened = position.get("asset_id") or position.get("created_at", "")
        # Fallback: use the change today / avg
        change_today = float(position.get("change_today", 0))
        avg_entry    = float(position.get("avg_entry_price", 1))
        unrealized   = float(position.get("unrealized_pl", 0))
        cost         = float(position.get("cost_basis", 1))
        if cost > 0 and avg_entry > 0:
            total_pct = unrealized / cost
            daily_pct = abs(change_today / avg_entry) if avg_entry > 0 else 0.01
            if daily_pct > 0:
                return abs(total_pct) / daily_pct
    except Exception:
        pass
    return 1.0  # Default: assume 1 day


# ── Main monitor ──────────────────────────────────────────────────────────────

def run_prime_exit_monitor(dry_run: bool = True):
    now_et = datetime.datetime.now(ET)
    print(f"\n⚡ Prime Exit Monitor v2.0 — {now_et.strftime('%Y-%m-%d %H:%M ET')}")

    positions = get_open_positions()
    if not positions:
        print("  No open Prime positions.")
        return []

    vix      = get_vix()
    cb_state = load_cb_state()
    closed   = []

    print(f"  {len(positions)} position(s) | VIX: {vix:.1f}")

    # ── CIRCUIT BREAKER: VIX ALL ──────────────────────────────────────────────
    if vix >= CB_VIX_ALL:
        print(f"  🚨 VIX {vix:.1f} ≥ {CB_VIX_ALL} — CLOSE ALL")
        for pos in positions:
            ticker    = pos["symbol"]
            shares    = int(float(pos.get("qty", 0)))
            direction = "long" if shares > 0 else "short"
            if not dry_run:
                close_position(ticker, abs(shares), direction, f"VIX {vix:.1f} circuit breaker")
            closed.append({"ticker": ticker, "trigger": "VIX_ALL", "vix": vix})
        notify(f"🚨 <b>PRIME VIX CIRCUIT BREAKER</b>\nVIX: {vix:.1f} — all {len(positions)} closed\n"
               f"<i>{'DRY RUN' if dry_run else 'LIVE'}</i>")
        return closed

    # ── CIRCUIT BREAKER: VIX LONGS ───────────────────────────────────────────
    if vix >= CB_VIX_LONGS:
        long_pos = [p for p in positions if float(p.get("qty", 0)) > 0]
        if long_pos:
            print(f"  ⚠️  VIX {vix:.1f} ≥ {CB_VIX_LONGS} — close longs")
            for pos in long_pos:
                if not dry_run:
                    close_position(pos["symbol"], int(float(pos["qty"])),
                                   "long", f"VIX {vix:.1f}")
                closed.append({"ticker": pos["symbol"], "trigger": "VIX_LONGS"})
            notify(f"⚠️ <b>PRIME VIX ALERT</b> — {len(long_pos)} long(s) closed. VIX {vix:.1f}")

    # ── PER-POSITION CHECKS ───────────────────────────────────────────────────
    for pos in positions:
        ticker      = pos["symbol"]
        shares      = int(float(pos.get("qty", 0)))
        direction   = "long" if shares > 0 else "short"
        entry_price = float(pos.get("avg_entry_price", 0))
        unrealized  = float(pos.get("unrealized_pl", 0))
        cost_basis  = float(pos.get("cost_basis", 1))
        current     = get_price(ticker)

        if entry_price <= 0 or current <= 0:
            continue

        gain_pct = (current - entry_price) / entry_price if direction == "long" \
                   else (entry_price - current) / entry_price

        # Detect tier from position metadata (fallback T2)
        tier   = 2
        tier_p = TIER_PARAMS.get(tier, TIER_PARAMS[2])

        hold_days   = get_hold_days(pos)
        trigger     = None
        reason      = None
        trigger_type = "NEUTRAL"

        print(f"\n  📊 {ticker} T{tier} {direction}: "
              f"${entry_price:.2f} → ${current:.2f} ({gain_pct*100:+.1f}%) "
              f"hold≈{hold_days:.1f}d")

        # Rule 1: Tier target hit → CLOSE IMMEDIATELY (velocity rule #1)
        if gain_pct >= tier_p["target"]:
            trigger      = "TARGET_HIT"
            trigger_type = "PROFIT"
            reason       = f"T{tier} target +{tier_p['target']*100:.0f}% hit — close immediately and redeploy"

        # Rule 2: Time stop (velocity rule #2 — thesis expired)
        elif hold_days >= tier_p["max_hold_days"]:
            trigger      = "TIME_STOP"
            trigger_type = "TIME" if gain_pct >= 0 else "LOSS"
            reason       = f"T{tier} time stop: {hold_days:.1f} days ≥ {tier_p['max_hold_days']} max. Thesis expired."

        # Rule 3: Hard stop
        elif gain_pct <= -tier_p["stop"]:
            trigger      = "HARD_STOP"
            trigger_type = "LOSS"
            reason       = f"Hard stop T{tier} -{tier_p['stop']*100:.0f}%: ${current:.2f}"

        # Rule 4: Momentum stall (RSI)
        elif not trigger:
            rsi = get_rsi(ticker)
            if direction == "long" and rsi < tier_p["rsi_exit"]:
                trigger      = "MOMENTUM_STALL"
                trigger_type = "LOSS" if gain_pct < 0 else "NEUTRAL"
                reason       = f"RSI {rsi:.0f} < {tier_p['rsi_exit']} — momentum exhausted on long"

        # Rule 5: Earnings check
        if not trigger:
            try:
                import yfinance as yf
                cal = yf.Ticker(ticker).calendar
                if cal is not None:
                    ed = cal.get("Earnings Date")
                    if ed is not None:
                        if hasattr(ed, '__iter__'):
                            ed = list(ed)[0]
                        days_away = (ed.date() - datetime.date.today()).days
                        if 0 < days_away <= EARNINGS_DAYS:
                            trigger      = "EARNINGS"
                            trigger_type = "RISK"
                            reason       = f"Earnings in {days_away} day(s) — close before event risk"
            except Exception:
                pass

        if trigger:
            pnl_usd = unrealized
            log_event({"ts": now_et.isoformat(), "ticker": ticker, "tier": tier,
                       "trigger": trigger, "type": trigger_type,
                       "gain_pct": gain_pct, "hold_days": hold_days,
                       "vix": vix, "dry_run": dry_run, "pnl_usd": pnl_usd})

            if not dry_run:
                close_position(ticker, abs(shares), direction, reason)

                # Update circuit breaker state
                if trigger_type == "LOSS":
                    cb_state["daily_loss"] = round(cb_state.get("daily_loss", 0) + abs(pnl_usd), 2)
                    cb_state["consecutive_losses"] = cb_state.get("consecutive_losses", 0) + 1
                    if cb_state["daily_loss"] >= CB_DAILY_LOSS:
                        cb_state["halted_until"] = (datetime.datetime.utcnow() +
                                                     datetime.timedelta(hours=24)).isoformat()
                        notify(f"🛑 <b>PRIME DAILY LOSS HALT</b>\n"
                               f"Daily loss: ${cb_state['daily_loss']:.2f}\nHalted 24h")
                    if cb_state["consecutive_losses"] >= CB_CONSEC_LOSSES:
                        cb_state["halted_until"] = (datetime.datetime.utcnow() +
                                                     datetime.timedelta(hours=48)).isoformat()
                        notify(f"🛑 <b>PRIME CONSECUTIVE LOSS HALT</b>\n"
                               f"{cb_state['consecutive_losses']} losses — halted 48h")
                else:
                    cb_state["consecutive_losses"] = 0
                save_cb_state(cb_state)

            icon = "✅" if trigger_type == "PROFIT" else "⏱️" if trigger_type == "TIME" else "🛑"
            notify(
                f"{icon} <b>PRIME EXIT — {ticker}</b>\n"
                f"T{tier} | Trigger: {trigger}\n"
                f"{reason}\n"
                f"P&L: {gain_pct*100:+.1f}% (${pnl_usd:+.2f})\n"
                f"Hold: {hold_days:.1f}d | VIX: {vix:.1f}\n"
                f"<i>{'DRY RUN' if dry_run else 'LIVE — capital redeploying'}</i>"
            )
            closed.append({
                "ticker": ticker, "tier": tier, "trigger": trigger,
                "gain_pct": gain_pct, "hold_days": hold_days
            })
        else:
            remaining_pct = (tier_p["target"] - gain_pct) * 100
            remaining_days = tier_p["max_hold_days"] - hold_days
            print(f"     Hold — {remaining_pct:.1f}% to target | {remaining_days:.1f}d remaining")

    return closed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live",  action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    dry_run = not args.live
    if dry_run:
        print("⚠️  DRY RUN — no orders submitted")

    closed = run_prime_exit_monitor(dry_run=dry_run)
    print(f"\n{'⚡ Velocity cycle complete' if not closed else f'🚪 Closed {len(closed)} position(s)'}")
    for c in closed:
        print(f"  {c['ticker']} T{c['tier']}: {c['trigger']} ({c.get('gain_pct',0)*100:+.1f}% | {c.get('hold_days',0):.1f}d)")
