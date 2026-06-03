"""
spread_executor.py — Bull Put Spread Order Executor
====================================================
Places bull put spreads as two-legged orders via Alpaca.
Confirms fills. Registers positions to THESIS position tracker.

Order flow:
  1. Sell short put (collect premium)
  2. Buy long put (define max loss)
  3. Confirm both legs filled
  4. Register position with entry data
  5. Notify Ahmed via Telegram
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

# Guard module imports (structural safety fixes)
try:
    from .order_guard import place_option_order_safe
    from .event_log import init_event_log_schema, log_event
    from .router_gateway import get_router_gateway
    from .cascade_breaker import can_execute_component, record_component_success, record_component_failure
    from .escalation import escalate_critical, escalate_warn
    HAS_GUARDS = True
except ImportError:
    HAS_GUARDS = False
    log_event = lambda *args, **kwargs: None
    escalate_critical = lambda *args, **kwargs: None
    escalate_warn = lambda *args, **kwargs: None

log = logging.getLogger("thesis.executor")
_ET = ZoneInfo("America/New_York")

TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT       = os.getenv("THESIS_CHAT_ID", os.getenv("AHMED_CHAT_ID", ""))
POSITIONS_DB  = os.getenv("THESIS_POSITIONS_DB",
                          "/Users/ahmedsadek/nexus/data/thesis_positions.db")
FILL_TIMEOUT  = 30


def _get_guardian():
    """Get THESIS guardian — lazy import to avoid circular deps."""
    import sys as _sys
    _sys.path.insert(0, "/Users/ahmedsadek/nexus")
    try:
        from shared.alpaca.registry import get_guardian
        g = get_guardian("THESIS")
        if g:
            return g
    except Exception:
        pass
    # Fallback: build a fresh guardian if registry not running
    from shared.alpaca.guardian import AlpacaGuardian
    return AlpacaGuardian(
        system_id  = "THESIS",
        api_key    = os.getenv("ALPACA_API_KEY",""),
        secret_key = os.getenv("ALPACA_SECRET_KEY",""),
        db_path    = POSITIONS_DB,
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


# ---------------------------------------------------------------------------
# Position database
# ---------------------------------------------------------------------------

def init_positions_db() -> None:
    conn = sqlite3.connect(POSITIONS_DB, timeout=10)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            position_id     TEXT PRIMARY KEY,
            ticker          TEXT NOT NULL,
            strategy        TEXT NOT NULL,
            expiration      TEXT NOT NULL,
            dte_at_entry    INTEGER,
            short_strike    REAL,
            long_strike     REAL,
            short_symbol    TEXT,
            long_symbol     TEXT,
            short_order_id  TEXT,
            long_order_id   TEXT,
            contracts       INTEGER DEFAULT 1,
            net_premium     REAL,
            max_profit      REAL,
            max_loss        REAL,
            breakeven       REAL,
            underlying_price REAL,
            endorsements    INTEGER,
            win_rate        REAL,
            legends         TEXT,
            status          TEXT DEFAULT 'OPEN',
            entry_time      TEXT,
            exit_time       TEXT,
            exit_reason     TEXT,
            exit_pnl        REAL,
            allocation_id   TEXT,
            ts              REAL
        );

        CREATE TABLE IF NOT EXISTS exit_targets (
            position_id     TEXT PRIMARY KEY,
            profit_target   REAL,
            stop_loss       REAL,
            time_exit_dte   INTEGER DEFAULT 21,
            thesis_active   INTEGER DEFAULT 1,
            FOREIGN KEY(position_id) REFERENCES positions(position_id)
        );

        CREATE INDEX IF NOT EXISTS idx_positions_status
            ON positions(status, ticker);
        CREATE INDEX IF NOT EXISTS idx_positions_expiration
            ON positions(expiration, status);
    """)
    conn.commit()
    conn.close()
    
    # Initialize event log schema (guard module)
    if HAS_GUARDS:
        try:
            init_event_log_schema(POSITIONS_DB)
            log.info("Event log schema initialized")
        except Exception as e:
            log.warning("Event log initialization failed: %s", e)


def save_position(position: dict) -> None:
    conn = sqlite3.connect(POSITIONS_DB, timeout=10)
    conn.execute("""
        INSERT OR REPLACE INTO positions
        (position_id, ticker, strategy, expiration, dte_at_entry,
         short_strike, long_strike, short_symbol, long_symbol,
         short_order_id, long_order_id, contracts, net_premium,
         max_profit, max_loss, breakeven, underlying_price,
         endorsements, win_rate, legends, status, entry_time, ts)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        position["position_id"],
        position["ticker"],
        position.get("strategy", "bull_put_spread"),
        position["expiration"],
        position.get("dte_at_entry", 0),
        position["short_strike"],
        position["long_strike"],
        position["short_symbol"],
        position["long_symbol"],
        position.get("short_order_id", ""),
        position.get("long_order_id", ""),
        position.get("contracts", 1),
        position["net_premium"],
        position["max_profit"],
        position["max_loss"],
        position["breakeven"],
        position.get("underlying_price", 0),
        position.get("endorsements", 0),
        position.get("win_rate", 0),
        position.get("legends", ""),
        position.get("status", "OPEN"),
        position.get("entry_time", datetime.now(timezone.utc).isoformat()),
        time.time(),
    ))

    # Save exit targets
    conn.execute("""
        INSERT OR REPLACE INTO exit_targets
        (position_id, profit_target, stop_loss, time_exit_dte, thesis_active)
        VALUES (?,?,?,?,?)
    """, (
        position["position_id"],
        position["net_premium"] * 0.50,   # 50% profit target
        position["net_premium"] * 2.00,   # 200% stop loss
        21,                                # 21 DTE time exit
        1,                                 # thesis active
    ))

    conn.commit()
    conn.close()


def get_open_positions() -> list[dict]:
    try:
        conn = sqlite3.connect(POSITIONS_DB, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='OPEN' ORDER BY entry_time DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def update_position_status(
    position_id: str, status: str,
    exit_reason: str = "", exit_pnl: float = 0.0
) -> None:
    conn = sqlite3.connect(POSITIONS_DB, timeout=5)
    conn.execute("""
        UPDATE positions
        SET status=?, exit_time=?, exit_reason=?, exit_pnl=?
        WHERE position_id=?
    """, (
        status,
        datetime.now(timezone.utc).isoformat(),
        exit_reason,
        exit_pnl,
        position_id,
    ))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def place_option_order(
    symbol: str,
    side: str,
    contracts: int = 1,
    order_type: str = "limit",
    limit_price: Optional[float] = None,
    client_order_id: Optional[str] = None,
) -> Optional[dict]:
    """Place option order via Guardian — idempotent, circuit-broken, auto-healing."""
    import uuid as _uuid
    coid = client_order_id or str(_uuid.uuid4())[:48]
    try:
        guardian = _get_guardian()
        order = guardian.place_option_order(
            contract_symbol  = symbol,
            qty              = contracts,
            side             = side,
            order_type       = order_type,
            limit_price      = limit_price,
            client_order_id  = coid,
        )
        log.info("Order placed: %s %s %s → id=%s",
                side, contracts, symbol, order.get("id","?"))
        return order
    except Exception as exc:
        log.error("Order failed for %s: %s", symbol, exc)
        return None


def wait_for_fill(order_id: str, timeout: int = FILL_TIMEOUT) -> Optional[dict]:
    """Poll via Guardian until filled or timeout."""
    try:
        return _get_guardian().wait_for_fill(order_id, timeout_s=timeout)
    except Exception as exc:
        log.error("wait_for_fill error: %s", exc)
        return None


def cancel_order(order_id: str) -> bool:
    """Cancel via Guardian — safe cancel only."""
    try:
        return _get_guardian().cancel_order_safe(order_id)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Bull put spread executor
# ---------------------------------------------------------------------------

def execute_bull_put_spread(
    spread: dict,
    contracts: int = 1,
    endorsements: int = 0,
    win_rate: float = 0.0,
    legends: list = None,
) -> Optional[dict]:
    """
    Execute a bull put spread:
    1. Sell short put (collect premium)
    2. Buy long put (hedge)
    3. Confirm both fills
    4. Save position to DB
    5. Notify Ahmed

    Returns position dict if successful, None if failed.
    """
    init_positions_db()
    position_id = str(uuid.uuid4())[:8]
    ticker      = spread["ticker"]
    short_sym   = spread["short_symbol"]
    long_sym    = spread["long_symbol"]
    premium     = spread["net_premium"]
    position_value = premium * contracts * 100  # per contract = 100 shares

    log.info("Executing bull put spread: %s %s/%s exp=%s premium=$%.2f contracts=%d",
             ticker, spread["short_strike"], spread["long_strike"],
             spread["expiration"], premium, contracts)
    
    # Capital Router pre-flight check (structural safety)
    allocation_id = None
    if HAS_GUARDS:
        try:
            router = get_router_gateway()
            # Check if ticker is already allocated elsewhere
            ticker_state = router.verify_position_state(ticker)
            if ticker_state.get("allocated") and ticker_state.get("allocated_to") != "THESIS":
                log.error("%s: Capital conflict! Allocated to %s. Cannot execute.",
                         ticker, ticker_state["allocated_to"])
                escalate_critical(
                    "Capital Conflict",
                    f"Ticker {ticker} already allocated to {ticker_state['allocated_to']}",
                    position_id
                )
                return None
            # Allocate capital for this spread
            allocation = router.allocate_capital(
                ticker=ticker,
                amount_usd=position_value,
                position_type="bull_put_spread",
                dte=spread.get("dte", 0),
                contracts=contracts,
            )
            if allocation.get("status") != "ALLOCATED":
                log.error("%s: Capital allocation failed — %s", ticker, allocation.get("message"))
                return None
            allocation_id = allocation.get("allocation_id")
            log.info("%s: Capital allocated — id=%s", ticker, allocation_id)
        except Exception as e:
            log.warning("%s: Capital Router unavailable (non-blocking): %s", ticker, e)

    # ── Leg 1: Sell short put ─────────────────────────────────────────────────
    short_order = place_option_order(short_sym, "sell", contracts)
    if not short_order:
        log.error("%s: Failed to place short put order", ticker)
        return None

    short_fill = wait_for_fill(short_order["id"])
    if not short_fill:
        cancel_order(short_order["id"])
        log.error("%s: Short put did not fill — cancelled", ticker)
        return None

    actual_short_price = float(short_fill.get("filled_avg_price", premium * 0.6))

    # ── Leg 2: Buy long put ───────────────────────────────────────────────────
    long_order = place_option_order(long_sym, "buy", contracts)
    if not long_order:
        # Critical: short leg filled but long leg failed — must close short
        log.error("%s: Long put order failed — closing short leg", ticker)
        place_option_order(short_sym, "buy", contracts)  # close short
        return None

    long_fill = wait_for_fill(long_order["id"])
    if not long_fill:
        log.error("%s: Long put did not fill — closing short leg", ticker)
        place_option_order(short_sym, "buy", contracts)
        return None

    actual_long_price = float(long_fill.get("filled_avg_price", premium * 0.2))
    actual_premium    = actual_short_price - actual_long_price
    actual_max_profit = actual_premium * contracts * 100
    actual_max_loss   = (spread["spread_width"] - actual_premium) * contracts * 100

    # ── Save position ─────────────────────────────────────────────────────────
    # Log position entry event
    if HAS_GUARDS:
        log_event(POSITIONS_DB, position_id, "SPREAD_EXECUTION_START",
                  actor="spread_executor",
                  details={"ticker": ticker, "contracts": contracts})
    
    position = {
        "position_id":     position_id,
        "ticker":          ticker,
        "strategy":        "bull_put_spread",
        "expiration":      spread["expiration"],
        "dte_at_entry":    spread["dte"],
        "allocation_id":   allocation_id,
        "short_strike":    spread["short_strike"],
        "long_strike":     spread["long_strike"],
        "short_symbol":    short_sym,
        "long_symbol":     long_sym,
        "short_order_id":  short_order["id"],
        "long_order_id":   long_order["id"],
        "contracts":       contracts,
        "net_premium":     round(actual_premium, 2),
        "max_profit":      round(actual_max_profit, 2),
        "max_loss":        round(actual_max_loss, 2),
        "breakeven":       round(spread["short_strike"] - actual_premium, 2),
        "underlying_price": spread["underlying_price"],
        "endorsements":    endorsements,
        "win_rate":        win_rate,
        "legends":         ",".join(legends or []),
        "status":          "OPEN",
        "entry_time":      datetime.now(timezone.utc).isoformat(),
    }

    save_position(position)

    log.info("%s EXECUTED: premium=$%.2f max_profit=$%.2f max_loss=$%.2f",
             ticker, actual_premium, actual_max_profit, actual_max_loss)

    # ── Notify Ahmed ──────────────────────────────────────────────────────────
    _notify(
        f"<b>THESIS TRADE EXECUTED</b>\n"
        f"Ticker: {ticker} | ID: {position_id}\n"
        f"Strategy: Bull Put Spread\n"
        f"Strikes: {spread['short_strike']}/{spread['long_strike']}\n"
        f"Expiration: {spread['expiration']} ({spread['dte']} DTE)\n"
        f"Premium: ${actual_premium:.2f} | Contracts: {contracts}\n"
        f"Max Profit: ${actual_max_profit:.0f} | Max Loss: ${actual_max_loss:.0f}\n"
        f"Win Rate: {win_rate:.1%} | Endorsements: {endorsements}/5\n"
        f"Legends: {', '.join(legends or [])}"
    )

    return position
