"""
thesis_conversational.py — THESIS Conversational Interface
===========================================================
Allows Ahmed to ask THESIS questions via Telegram and get intelligent responses.

How it works:
  1. Telegram webhook/polling receives Ahmed's message
  2. Claude analyzes the question with full THESIS context
  3. Response sent back to Telegram within seconds

Example questions Ahmed can ask:
  "What did you trade today?"
  "Why did you pick NVDA?"
  "What's your current exposure?"
  "How many positions are open?"
  "What's the best performing pick?"
  "What does your shortlist look like?"
  "Are you going to trade this afternoon?"
  "What's the regime telling you?"
"""
from __future__ import annotations
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import anthropic
import requests
from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger("thesis.conversational")
_ET = ZoneInfo("America/New_York")

ALPACA_KEY      = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET   = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_URL      = "https://paper-api.alpaca.markets"
TG_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT         = os.getenv("THESIS_CHAT_ID", os.getenv("AHMED_CHAT_ID", ""))
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
BACKTEST_DB     = os.getenv("BACKTEST_DB_PATH", "/Users/ahmedsadek/nexus/data/backtest.db")
AHMED_CHAT_ID   = os.getenv("AHMED_CHAT_ID", "8573754783")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    return _client


# ---------------------------------------------------------------------------
# Context gathering — what THESIS knows right now
# ---------------------------------------------------------------------------

def _get_alpaca(path: str):
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
    except Exception:
        pass
    return None


def _gather_context(screening_state: dict) -> str:
    """Gather all current THESIS context for the LLM."""
    now_et = datetime.now(_ET)

    # Account
    account = _get_alpaca("/v2/account") or {}
    buying_power = float(account.get("buying_power", 0))
    portfolio_value = float(account.get("portfolio_value", 0))

    # Positions
    positions = _get_alpaca("/v2/positions") or []

    # Today's orders
    since = now_et.replace(hour=9, minute=30, second=0).astimezone(timezone.utc)
    orders = _get_alpaca(
        f"/v2/orders?status=filled&after={since.isoformat()}&limit=50"
    ) or []

    # Last screening
    last = screening_state.get("last_screening", {})
    regime = last.get("regime", screening_state.get("regime", "NORMAL"))
    vix = last.get("vix", 0)
    shortlist_size = last.get("shortlist_size", 0)
    qualified = last.get("qualified", 0)
    screenings_today = screening_state.get("screenings_today", 0)

    # Backtest top picks
    top_picks = []
    try:
        conn = sqlite3.connect(BACKTEST_DB, timeout=5)
        rows = conn.execute("""
            SELECT ticker, win_rate, sample_count
            FROM historical_win_rates
            WHERE strategy='bull_put_spread' AND regime=? AND direction='bullish'
            AND sample_count>=10 AND win_rate>=0.85
            ORDER BY win_rate DESC LIMIT 10
        """, (regime,)).fetchall()
        conn.close()
        top_picks = [{"ticker": r[0], "win_rate": r[1], "samples": r[2]} for r in rows]
    except Exception:
        pass

    # Format positions
    pos_text = "None" if not positions else "\n".join([
        f"  {p.get('symbol')} | qty={p.get('qty')} | "
        f"P&L=${float(p.get('unrealized_pl', 0)):+.2f} | "
        f"avg_entry=${float(p.get('avg_entry_price', 0)):.2f}"
        for p in positions
    ])

    # Format orders
    orders_text = "None today" if not orders else "\n".join([
        f"  {o.get('symbol')} | {o.get('side')} {o.get('filled_qty')} @ "
        f"${float(o.get('filled_avg_price') or 0):.2f} | "
        f"filled: {o.get('filled_at', '')[:16]}"
        for o in orders
    ])

    # Format top picks
    picks_text = "\n".join([
        f"  {p['ticker']}: {p['win_rate']:.1%} win rate ({p['samples']} samples)"
        for p in top_picks
    ]) or "No data"

    return f"""THESIS TRADING SYSTEM — LIVE CONTEXT
Time: {now_et.strftime('%Y-%m-%d %H:%M ET')}

ACCOUNT:
  Buying power: ${buying_power:,.0f}
  Portfolio value: ${portfolio_value:,.0f}
  Paper trading: Yes

MARKET REGIME:
  Classification: {regime}
  VIX: {vix:.1f}

TODAY'S ACTIVITY:
  Screenings completed: {screenings_today}/6
  Last shortlist size: {shortlist_size} tickers
  Qualified (≥85% win rate): {qualified}
  Trades executed today: {len(orders)}

OPEN POSITIONS:
{pos_text}

TODAY'S FILLED ORDERS:
{orders_text}

TOP BACKTEST PICKS FOR {regime} REGIME:
{picks_text}

SYSTEM STATUS:
  Five Legends screening: Active
  Strategy: Bull put spreads
  Min win rate required: 85%
  Min legend endorsements: 3/5
  Position size: $2,000 base (Kelly adjusted)
"""


# ---------------------------------------------------------------------------
# Main conversation handler
# ---------------------------------------------------------------------------

def handle_message(
    user_message: str,
    screening_state: dict,
    conversation_history: list,
) -> str:
    """
    Process a message from Ahmed and return THESIS's response.
    Maintains conversation history for context continuity.
    """
    context = _gather_context(screening_state)

    system_prompt = f"""You are THESIS, an autonomous trading system using Five Legends intelligence (Druckenmiller, Jones, Soros, Cohen, Buffett).

You are speaking directly with Ahmed Sadek, your operator. Be direct, precise, and confident.
Use data to answer every question. Never speculate — only report what you actually know.
Keep responses concise but complete. Use numbers. Be a trading system, not a chatbot.

YOUR CURRENT STATE:
{context}

When answering questions:
- "What did you trade?" → report filled orders with ticker, side, qty, price
- "Why did you pick X?" → explain which legends endorsed it and the backtest win rate
- "What's your exposure?" → report open positions with P&L
- "Are you going to trade?" → explain current regime, screening schedule, qualified tickers
- "What's the shortlist?" → report last shortlist results
- If you don't have data to answer, say so directly

Respond in plain text (no markdown). Keep it under 300 words unless detail is specifically requested."""

    messages = conversation_history + [
        {"role": "user", "content": user_message}
    ]

    try:
        response = _get_client().messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=system_prompt,
            messages=messages,
        )
        return response.content[0].text
    except Exception as exc:
        log.error("LLM call failed: %s", exc)
        return f"THESIS system error: {exc}. Check logs."


def send_response(msg: str, chat_id: str = None) -> None:
    target = chat_id or TG_CHAT
    if not TG_TOKEN or not target:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": target, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)


# ---------------------------------------------------------------------------
# Telegram polling loop
# ---------------------------------------------------------------------------

class ThesisConversation:
    """Polls Telegram for messages from Ahmed and responds as THESIS."""

    def __init__(self, screening_state_getter):
        self._get_state = screening_state_getter
        self._offset = 0
        self._history: list = []
        self._max_history = 10  # keep last 10 turns

    def poll_and_respond(self) -> None:
        """Poll Telegram for new messages. Non-blocking instant poll."""
        if not TG_TOKEN:
            return
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params={"offset": self._offset, "timeout": 0, "limit": 10},
                timeout=5,
            )
            if r.status_code != 200:
                return

            updates = r.json().get("result", [])
            for update in updates:
                self._offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                from_id = str(msg.get("from", {}).get("id", ""))

                # Only respond to Ahmed
                if from_id != AHMED_CHAT_ID and chat_id != AHMED_CHAT_ID:
                    continue

                if not text or text.startswith("/"):
                    continue

                log.info("Message from Ahmed: %s", text[:80])

                # Get response
                response = handle_message(
                    user_message=text,
                    screening_state=self._get_state(),
                    conversation_history=self._history,
                )

                # Update history
                self._history.append({"role": "user", "content": text})
                self._history.append({"role": "assistant", "content": response})

                # Trim history
                if len(self._history) > self._max_history * 2:
                    self._history = self._history[-(self._max_history * 2):]

                # Send response
                send_response(response, chat_id=chat_id)
                log.info("Response sent to Ahmed")

        except Exception as exc:
            log.warning("Poll error: %s", exc)
