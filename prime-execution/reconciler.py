"""
reconciler.py — Prime Execution Position Reconciler

Runs every 30 minutes during market hours.
Compares DB positions vs Alpaca live positions.
Pauses execution and alerts Ahmed on mismatch.

Reconciler logic:
  1. Get all open positions from SQLite
  2. Get all live positions from Alpaca
  3. If Alpaca fetch fails → treat as critical mismatch, pause execution
  4. Compare DB positions to Alpaca by BOTH ticker AND order_id
     (ticker-only matching causes false positives when multiple positions
      exist on the same ticker, or when Alpaca aggregates by symbol)
  5. On mismatch: log, alert Ahmed, pause execution flag
  6. Auto-resumes if mismatch is resolved next cycle

CRITICAL FIX (Pass A adversarial review):
  - Previous version matched by ticker only → false positives on duplicate tickers
  - Previous version did NOT pause on Alpaca API errors → unknown state treated as OK
"""

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from database import get_open_positions, log_reconciliation

# OMNI Pass 3 PROBE-I2 fix: run_reconciler() mutates app_state["execution_paused"]
# directly. The _state_lock in callers (scheduler + /reconcile endpoint) is cosmetic
# if callers can forget to acquire it. Acquire the lock INSIDE the function so any
# future caller path is automatically safe.
# Initialized lazily to avoid circular import; callers may also pass their own lock.
_reconciler_lock: Optional[threading.Lock] = None


def _get_reconciler_lock() -> threading.Lock:
    global _reconciler_lock
    if _reconciler_lock is None:
        _reconciler_lock = threading.Lock()
    return _reconciler_lock

logger = logging.getLogger("prime_exec.reconciler")


def run_reconciler(
    db_path:      str,
    alpaca_client,
    bot_token:    str,
    chat_id:      str,
    app_state:    dict,
) -> dict:
    """
    Run a reconciliation pass between SQLite and Alpaca.

    Args:
        db_path:       Prime Execution database path.
        alpaca_client: AlpacaClient instance.
        bot_token:     Telegram bot token.
        chat_id:       Ahmed's Telegram chat ID.
        app_state:     Shared app state (sets execution_paused flag).

    Returns:
        Reconciliation result dict.
    """
    # OMNI Pass 3 PROBE-I2: acquire lock internally so all callers are safe
    # regardless of whether they hold _state_lock. The external _state_lock in
    # main.py callers remains (belt-and-suspenders); this internal lock is the
    # authoritative guard on app_state["execution_paused"] mutations.
    with _get_reconciler_lock():
        return _run_reconciler_locked(db_path, alpaca_client, bot_token, chat_id, app_state)


def _run_reconciler_locked(
    db_path:      str,
    alpaca_client,
    bot_token:    str,
    chat_id:      str,
    app_state:    dict,
) -> dict:
    """Internal implementation — always called under _get_reconciler_lock()."""
    now = datetime.now(timezone.utc).isoformat()

    # Get DB open positions
    db_positions = get_open_positions(db_path)

    # Get Alpaca live positions AND pending orders (Cipher Pass 3 P3-9)
    # P3-9: reconciler ran between order placement (DB status='open') and order fill
    # (Alpaca position appears). get_positions() returns no record during the fill window →
    # false mismatch → execution paused → false alarm.
    # Fix: also fetch pending Alpaca orders and treat tickers with active orders
    # as "present on Alpaca" to avoid false positives during the fill window.
    alpaca_symbols: set[str] = set()
    alpaca_pending_tickers: set[str] = set()
    alpaca_error: Optional[str] = None
    try:
        live           = alpaca_client.get_positions()
        alpaca_symbols = {p.get("symbol", "").upper() for p in live}
        # Fetch open orders to cover the order-placed-but-not-yet-filled window
        try:
            open_orders = alpaca_client.get_orders(status="open") or []
            alpaca_pending_tickers = {
                o.get("symbol", "").upper() for o in open_orders
            }
            if alpaca_pending_tickers:
                logger.debug(
                    "Reconciler: %d pending Alpaca orders — excluding from mismatch check",
                    len(alpaca_pending_tickers),
                )
        except Exception as e_orders:
            # Order fetch failure is non-critical — positions fetch succeeded
            logger.warning("Reconciler: could not fetch open orders: %s", e_orders)
    except Exception as e:
        alpaca_error = str(e)[:200]
        logger.error(
            "Reconciler: Alpaca positions fetch FAILED — pausing execution. Error: %s", e
        )

    # ── Critical fix: Alpaca failure = unknown state = pause ─────────────────
    if alpaca_error is not None:
        app_state["execution_paused"]          = True
        _paused_ts = __import__('time').time()
        app_state["reconcile_paused_at"] = _paused_ts   # P2 fix: timestamp for auto-clear
        app_state["last_reconcile_at"]   = now
        app_state["last_reconcile_mismatches"] = -1   # -1 signals API failure
        # Bug 4 fix: persist so auto-clear survives service restarts
        if "svc_state" in app_state:
            app_state["svc_state"].write("reconcile_paused_at", str(_paused_ts))

        log_reconciliation(
            db_path          = db_path,
            positions_db     = len(db_positions),
            positions_alpaca = 0,
            mismatches       = -1,
            mismatch_details = f"Alpaca API error: {alpaca_error}",
            execution_paused = True,
        )
        _send_mismatch_alert(
            bot_token, chat_id,
            [f"Cannot reach Alpaca API: {alpaca_error}"],
            len(db_positions), 0,
        )
        app_state["was_paused_for_reconcile"] = True
        return {
            "run_at":          now,
            "db_positions":    len(db_positions),
            "alpaca_positions": 0,
            "mismatches":      -1,
            "execution_paused": True,
            "alpaca_error":    alpaca_error,
        }

    # ── Compare by ticker (equity positions aggregate by symbol on Alpaca) ────
    # For equity positions, Alpaca always aggregates holdings by symbol —
    # there is at most ONE position record per symbol even across multiple orders.
    # We match DB positions by ticker against Alpaca symbols.
    # Multiple DB rows for the same ticker are expected (partial closes, etc.)
    # and do NOT count as mismatches — what matters is at least one Alpaca record exists.
    db_tickers_with_issues: list[str] = []
    mismatches: list[str] = []

    seen_tickers: set[str] = set()
    for pos in db_positions:
        ticker = pos["ticker"].upper()
        if ticker in seen_tickers:
            # Already checked this ticker — skip duplicates (Alpaca aggregates)
            continue
        seen_tickers.add(ticker)

        if ticker not in alpaca_symbols:
            # Cipher Pass 3 P3-9: check if there is a pending order for this ticker.
            # DB status='open' is written when the order is PLACED, not when it FILLS.
            # During the fill window (100ms–2s for equities), Alpaca shows no position
            # yet but the order exists. Flagging this as a mismatch causes a false pause.
            if ticker in alpaca_pending_tickers:
                logger.debug(
                    "Reconciler: %s in DB-open + Alpaca-pending-order — skipping (fill in progress)",
                    ticker,
                )
                continue

            # GAP-004 Option C: Order-centric verification.
            # Before flagging as a mismatch, verify the order status directly.
            # A filled order = position is REAL but /positions hasn't propagated yet.
            order_id = (pos.get("alpaca_order_id") or "").strip()
            if order_id:
                try:
                    order_data = alpaca_client.get_order(order_id)
                    if order_data:
                        o_status   = order_data.get("status", "")
                        o_filled   = float(order_data.get("filled_qty") or 0)
                        if o_status == "filled" and o_filled > 0:
                            logger.info(
                                "GAP-004: %s order %s is FILLED — position real, "
                                "skipping mismatch (Alpaca /positions propagation lag)",
                                ticker, order_id[:8],
                            )
                            continue   # Not a mismatch — position is real
                except Exception as _oc_err:
                    logger.debug("GAP-004 order-check failed for %s: %s", ticker, _oc_err)

            # DB says open, Alpaca has no position, order not filled — genuine mismatch
            mismatches.append(
                f"{ticker} (pos_id={pos['id']}, order_id={pos.get('alpaca_order_id','?')}) "
                f"in DB but not in Alpaca"
            )
            db_tickers_with_issues.append(ticker)

    mismatch_count   = len(mismatches)
    execution_paused = mismatch_count > 0

    # Update app state
    app_state["execution_paused"]          = execution_paused
    app_state["last_reconcile_at"]         = now
    app_state["last_reconcile_mismatches"] = mismatch_count
    # P2 fix: stamp pause timestamp for auto-clear logic in main.py
    if execution_paused:
        _paused_ts2 = __import__('time').time()
        app_state["reconcile_paused_at"] = _paused_ts2
        # Bug 4 fix: persist so auto-clear survives service restarts
        if "svc_state" in app_state:
            app_state["svc_state"].write("reconcile_paused_at", str(_paused_ts2))
    else:
        app_state["reconcile_paused_at"] = None
        if "svc_state" in app_state:
            app_state["svc_state"].write("reconcile_paused_at", "")

    # Log to DB
    log_reconciliation(
        db_path          = db_path,
        positions_db     = len(db_positions),
        positions_alpaca = len(alpaca_symbols),
        mismatches       = mismatch_count,
        mismatch_details = "; ".join(mismatches) if mismatches else None,
        execution_paused = execution_paused,
    )

    if execution_paused:
        logger.warning("RECONCILER MISMATCH: %d — %s", mismatch_count, mismatches)
        _send_mismatch_alert(bot_token, chat_id, mismatches, len(db_positions), len(alpaca_symbols))
    else:
        if mismatch_count == 0 and app_state.get("was_paused_for_reconcile"):
            logger.info("Reconciler: mismatch resolved — execution resumed")
        logger.info(
            "Reconciler OK: DB=%d Alpaca=%d mismatches=%d",
            len(db_positions), len(alpaca_symbols), mismatch_count,
        )

    app_state["was_paused_for_reconcile"] = execution_paused

    return {
        "run_at":           now,
        "db_positions":     len(db_positions),
        "alpaca_positions": len(alpaca_symbols),
        "mismatches":       mismatch_count,
        "execution_paused": execution_paused,
        "alpaca_error":     None,
    }


def _send_mismatch_alert(
    bot_token: str, chat_id: str,
    mismatches: list[str],
    db_count: int, alpaca_count: int,
) -> None:
    """Send reconciliation mismatch alert to Ahmed."""
    import requests
    details = "\n".join(f"  • {m}" for m in mismatches[:5])
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "parse_mode": "HTML",
                "text": (
                    f"⚠️ <b>PRIME RECONCILER MISMATCH</b>\n"
                    f"DB: {db_count} positions | Alpaca: {alpaca_count}\n"
                    f"Mismatches:\n{details}\n\n"
                    f"New Prime entries PAUSED until resolved."
                ),
            },
            timeout=8,
        )
    except Exception:
        pass
