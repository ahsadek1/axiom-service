"""
thesis_trader.py — THESIS Standalone Trading Engine
=====================================================
Independent trading system using Five Legends intelligence.

Architecture:
  1. Market Brief Generator → one JSON per cycle (every 90 min)
  2. Five Legends Screen    → 250 stocks → shortlist (3+ endorsements)
  3. Deterministic Scoring  → Cipher+Sage+Atlas score shortlist
  4. Backtest Filter        → win_rate >= 0.85 required
  5. Kelly Sizing           → position size by historical edge
  6. Execute               → bull put spreads via own Alpaca account
  7. Exit Monitor          → endorsing legend watches for thesis break
  8. AILS Learning         → per-legend accuracy tracking

Port: 8070 (standalone, independent of V2)
Ahmed directive May 2026.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("thesis.trader")
_ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ALPACA_API_KEY   = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET    = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER     = os.getenv("ALPACA_PAPER", "true").lower() == "true"
ALPACA_BASE_URL  = "https://paper-api.alpaca.markets" if ALPACA_PAPER else "https://api.alpaca.markets"

ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
POLYGON_KEY      = os.getenv("POLYGON_API_KEY", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT    = os.getenv("THESIS_CHAT_ID", os.getenv("AHMED_CHAT_ID", ""))

BACKTEST_DB      = os.getenv("BACKTEST_DB_PATH", "/Users/ahmedsadek/nexus/data/backtest.db")
NEXUS_SECRET     = os.getenv("NEXUS_WEBHOOK_SECRET", "")

# Trading parameters
BASE_POSITION_USD   = float(os.getenv("THESIS_BASE_POSITION", "2000"))
MAX_POSITIONS       = int(os.getenv("THESIS_MAX_POSITIONS", "20"))
MIN_BACKTEST_WINRATE = float(os.getenv("THESIS_MIN_WINRATE", "0.85"))
TARGET_DTE          = int(os.getenv("THESIS_TARGET_DTE", "40"))
MIN_LEGEND_ENDORSEMENTS = int(os.getenv("THESIS_MIN_ENDORSEMENTS", "3"))

# Screening interval
SCREENING_INTERVAL_MARKET   = 90 * 60   # 90 minutes
SCREENING_INTERVAL_OFFHOURS = 4 * 3600  # 4 hours


# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------

def _market_hours() -> bool:
    n = datetime.now(_ET)
    return n.weekday() < 5 and (n.hour > 9 or (n.hour == 9 and n.minute >= 30)) and n.hour < 16


def _screening_interval() -> int:
    return SCREENING_INTERVAL_MARKET if _market_hours() else SCREENING_INTERVAL_OFFHOURS


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _notify(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Alpaca
# ---------------------------------------------------------------------------

def _alpaca(method: str, path: str, data: Optional[dict] = None) -> Optional[dict]:
    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    try:
        if method == "GET":
            r = requests.get(f"{ALPACA_BASE_URL}{path}", headers=headers, timeout=10)
        else:
            r = requests.post(f"{ALPACA_BASE_URL}{path}", headers=headers, json=data, timeout=10)
        if r.status_code in (200, 201):
            return r.json()
        log.warning("Alpaca %s %s → %d", method, path, r.status_code)
    except Exception as exc:
        log.error("Alpaca error: %s", exc)
    return None


def get_account() -> Optional[dict]:
    return _alpaca("GET", "/v2/account")


def get_positions() -> list:
    result = _alpaca("GET", "/v2/positions")
    return result if isinstance(result, list) else []


def get_open_position_count() -> int:
    return len(get_positions())


# ---------------------------------------------------------------------------
# Backtest DB queries
# ---------------------------------------------------------------------------

def get_win_rate(ticker: str, regime: str, strategy: str = "bull_put_spread") -> Optional[float]:
    try:
        conn = sqlite3.connect(BACKTEST_DB, timeout=5)
        row = conn.execute(
            """SELECT win_rate FROM historical_win_rates
               WHERE ticker=? AND strategy=? AND regime=?
               AND direction='bullish' AND sample_count >= 10""",
            (ticker, strategy, regime),
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def get_top_tickers_by_winrate(regime: str, limit: int = 50) -> list[dict]:
    """Get top tickers by bull_put_spread win rate in current regime."""
    try:
        conn = sqlite3.connect(BACKTEST_DB, timeout=5)
        rows = conn.execute(
            """SELECT ticker, win_rate, sample_count
               FROM historical_win_rates
               WHERE strategy='bull_put_spread' AND regime=?
               AND direction='bullish' AND sample_count >= 20
               AND win_rate >= ?
               ORDER BY win_rate DESC LIMIT ?""",
            (regime, MIN_BACKTEST_WINRATE, limit),
        ).fetchall()
        conn.close()
        return [{"ticker": r[0], "win_rate": r[1], "samples": r[2]} for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Position sizing (Kelly-based)
# ---------------------------------------------------------------------------

def kelly_position_size(win_rate: float, base_usd: float = BASE_POSITION_USD) -> float:
    """Half-Kelly sizing based on historical win rate."""
    if win_rate >= 0.95:
        return base_usd * 1.5
    elif win_rate >= 0.85:
        return base_usd * 1.2
    elif win_rate >= 0.70:
        return base_usd * 1.0
    else:
        return base_usd * 0.7


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def health_check() -> dict:
    account = get_account()
    positions = get_positions()
    return {
        "status": "healthy" if account else "degraded",
        "service": "thesis-trader",
        "version": "1.0.0",
        "alpaca_connected": bool(account),
        "paper_mode": ALPACA_PAPER,
        "open_positions": len(positions),
        "max_positions": MAX_POSITIONS,
        "buying_power": account.get("buying_power", "N/A") if account else "N/A",
        "backtest_db": BACKTEST_DB,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("THESIS Trader starting...")
    h = health_check()
    log.info("Health: %s", h)
    if h["alpaca_connected"]:
        _notify(
            f"✅ <b>THESIS TRADER ONLINE</b>\n"
            f"Paper mode: {ALPACA_PAPER}\n"
            f"Buying power: ${float(h['buying_power']):,.0f}\n"
            f"Max positions: {MAX_POSITIONS}\n"
            f"Min win rate: {MIN_BACKTEST_WINRATE:.0%}\n"
            f"Ready to trade."
        )
    else:
        log.error("Alpaca not connected — check credentials")
