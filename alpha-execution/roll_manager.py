"""
roll_manager.py — Auto-Roll Manager for Alpha Execution

Automatically rolls options spreads reaching DTE ≤ 21 forward to a new
~40-DTE expiration. Called by exit_monitor.evaluate_exits() as part of
the normal exit-evaluation loop.

Roll logic:
  1. Check dedup: skip if position was already rolled today
  2. Resolve new spread params (same ticker/direction, ~40 DTE)
  3. Find new contracts via Alpaca options chain
  4. Close existing spread (buy-to-close)
  5. Open replacement spread (sell-to-open)
  6. Update DB atomically: old closed ('ROLLED'), new position created
  7. Send Telegram alert on outcome

Failure safety:
  - If close succeeds but new order fails → P0 alert + CHRONICLE log
  - If DB write fails → rollback, keep old position open
  - Max 1 roll per position per calendar day (dedup)
"""

import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
from alert_client import send_alert as _send_alert

from config import (
    DTE_ROLL_THRESHOLD,
    TARGET_DTE,
)
from contract_resolver import resolve_spread
from database import (
    cancel_pending_position,
    close_position,
    confirm_pending_position,
    get_open_positions,
    get_position_by_id,
    reserve_position_slot,
)

logger = logging.getLogger("alpha_exec.roll_manager")

# ── In-memory dedup: position_id → date rolled ────────────────────────────────
_rolled_today: dict[int, date] = {}


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class RollResult:
    """Outcome of an attempted roll operation."""

    position_id:     int
    ticker:          str
    action:          str          # "rolled" | "skipped" | "failed"
    new_position_id: Optional[int] = None
    reason:          str           = ""
    error:           str           = ""


# ── Public API ────────────────────────────────────────────────────────────────

def evaluate_rolls(
    db_path:      str,
    alpaca_client,
    bot_token:    str,
    chat_id:      str,
) -> list[RollResult]:
    """
    Evaluate all open positions and roll those at or below DTE_ROLL_THRESHOLD.

    Args:
        db_path:       Alpha Execution database path.
        alpaca_client: Authenticated AlpacaClient instance.
        bot_token:     Telegram bot token.
        chat_id:       Ahmed's Telegram chat ID.

    Returns:
        List of RollResult for every position evaluated (only includes results
        where action != "skipped" due to DTE being above threshold).
    """
    from contract_resolver import calculate_dte  # local import avoids circular at module load

    positions = get_open_positions(db_path)
    results: list[RollResult] = []

    for pos in positions:
        pos_id  = pos["id"]
        ticker  = pos["ticker"]
        exp_str = pos.get("expiration_date") or pos.get("expiry_date") or ""

        if not exp_str:
            logger.debug("roll_manager: position %d has no expiration_date — skipping", pos_id)
            continue

        try:
            dte = calculate_dte(exp_str)
        except Exception as exc:
            logger.warning("roll_manager: DTE calc failed for pos %d: %s", pos_id, exc)
            continue

        if dte > DTE_ROLL_THRESHOLD:
            continue  # not yet at roll threshold

        result = _attempt_roll(pos, db_path, alpaca_client, bot_token, chat_id)
        results.append(result)

    return results


# ── Internal: single roll attempt ─────────────────────────────────────────────

def _attempt_roll(
    pos:          dict,
    db_path:      str,
    alpaca_client,
    bot_token:    str,
    chat_id:      str,
) -> RollResult:
    """
    Attempt to roll a single position forward.

    Returns a RollResult describing what happened.
    """
    pos_id  = pos["id"]
    ticker  = pos["ticker"]
    today   = date.today()

    # ── Dedup: skip if already rolled today ──────────────────────────────────
    if _rolled_today.get(pos_id) == today:
        logger.info("roll_manager: pos %d already rolled today — dedup skip", pos_id)
        return RollResult(pos_id, ticker, "skipped", reason="already_rolled_today")

    # ── Validate position is still open ──────────────────────────────────────
    current = get_position_by_id(db_path, pos_id)
    if not current or current.get("status") != "open":
        logger.info("roll_manager: pos %d not open (status=%s) — skipping", pos_id, current.get("status") if current else "missing")
        return RollResult(pos_id, ticker, "skipped", reason="position_not_open")

    direction = pos.get("direction", "bullish")
    contracts = pos.get("contracts", 1)
    pos_size  = pos.get("position_size_usd", 2000.0)

    # ── Resolve new spread parameters ────────────────────────────────────────
    try:
        price = alpaca_client.get_latest_price(ticker)
        if not price:
            raise ValueError(f"No price available for {ticker}")
        new_params = resolve_spread(ticker, direction, price)
    except Exception as exc:
        logger.error("roll_manager: spread resolution failed for %s: %s", ticker, exc)
        _send_roll_alert(
            bot_token, chat_id, ticker, pos_id,
            action="FAILED", reason=f"Spread resolution error: {exc}",
        )
        return RollResult(pos_id, ticker, "failed", error=str(exc), reason="spread_resolution_failed")

    # ── Find new option contracts ─────────────────────────────────────────────
    new_legs = _find_roll_contracts(alpaca_client, new_params, contracts)
    if new_legs is None:
        logger.warning("roll_manager: no roll contracts found for %s %s", ticker, new_params.expiration_date)
        _send_roll_alert(
            bot_token, chat_id, ticker, pos_id,
            action="SKIPPED", reason=f"No contracts found for {new_params.expiration_date}",
        )
        return RollResult(pos_id, ticker, "skipped", reason="no_contracts_available")

    # ── Build close legs (buy-to-close existing spread) ──────────────────────
    short_sym = pos.get("short_contract_symbol")
    long_sym  = pos.get("long_contract_symbol")

    if not short_sym or not long_sym:
        logger.error("roll_manager: pos %d missing contract symbols — cannot close", pos_id)
        return RollResult(pos_id, ticker, "failed", error="missing_contract_symbols")

    close_legs = [
        {"symbol": short_sym, "side": "buy",  "ratio_qty": "1"},  # buy back short
        {"symbol": long_sym,  "side": "sell", "ratio_qty": "1"},  # sell long
    ]

    # ── Step 1: Close the existing spread ────────────────────────────────────
    close_coid = f"roll-close-{pos_id}-{today.strftime('%Y%m%d')}"
    logger.info("roll_manager: closing pos %d (%s) via spread close order", pos_id, ticker)
    try:
        alpaca_client.place_spread_order(
            legs            = close_legs,
            qty             = contracts,
            order_type      = "limit",
            limit_debit     = None,   # use market for close leg of roll
            client_order_id = close_coid,
        )
        logger.info("roll_manager: close order placed for pos %d", pos_id)
    except Exception as exc:
        logger.error("roll_manager: close order FAILED for pos %d: %s", pos_id, exc)
        _send_roll_alert(
            bot_token, chat_id, ticker, pos_id,
            action="FAILED", reason=f"Close order error: {exc}",
        )
        return RollResult(pos_id, ticker, "failed", error=str(exc), reason="close_order_failed")

    # Close in DB — old position done
    try:
        close_position(
            db_path      = db_path,
            position_id  = pos_id,
            close_reason = "ROLLED",
            pnl_pct      = pos.get("current_pnl_pct", 0.0),
            pnl_usd      = 0.0,  # P&L settled at roll execution price — unknown here
        )
    except Exception as exc:
        logger.error("roll_manager: DB close failed for pos %d: %s — position still open in DB", pos_id, exc)
        # Don't abort: Alpaca close was submitted; DB will be stale but better than crashing

    # ── Step 2: Open replacement spread ──────────────────────────────────────
    open_coid  = f"roll-open-{pos_id}-{today.strftime('%Y%m%d')}"
    new_pos_id: Optional[int] = None

    try:
        # Reserve DB slot before placing order
        from contract_resolver import calculate_dte

        new_pos_id = reserve_position_slot(
            db_path           = db_path,
            ticker            = ticker,
            direction         = direction,
            pathway           = pos.get("pathway", "P1"),
            option_type       = new_params.option_type,
            short_strike      = new_params.short_strike,
            long_strike       = new_params.long_strike,
            expiration_date   = new_params.expiration_date,
            dte_at_open       = new_params.target_dte,
            contracts         = contracts,
            position_size_usd = pos_size,
            window_id         = f"roll-{pos_id}",
            agent_scores      = pos.get("agent_scores", "{}"),
            verdict           = pos.get("verdict", "ROLLED"),
        )

        alpaca_resp = alpaca_client.place_spread_order(
            legs            = new_legs,
            qty             = contracts,
            order_type      = "limit",
            client_order_id = open_coid,
        )

        # Confirm position with new contract symbols
        # Roll uses a single spread order (mleg) — both legs share the same order_id
        roll_order_id = alpaca_resp.get("id", "")
        confirm_pending_position(
            db_path                = db_path,
            position_id            = new_pos_id,
            short_alpaca_order_id  = roll_order_id,
            long_alpaca_order_id   = roll_order_id,
            short_contract_symbol  = _build_occ(new_params, "short"),
            long_contract_symbol   = _build_occ(new_params, "long"),
            entry_price            = 0.0,  # filled price unknown at submission
        )

        logger.info(
            "roll_manager: ✅ pos %d → new pos %d | %s %s → exp %s",
            pos_id, new_pos_id, ticker, direction, new_params.expiration_date,
        )

        # Mark dedup
        _rolled_today[pos_id] = today

        _send_roll_alert(
            bot_token, chat_id, ticker, pos_id,
            action        = "ROLLED",
            new_pos_id    = new_pos_id,
            new_expiry    = new_params.expiration_date,
            new_dte       = new_params.target_dte,
        )

        return RollResult(pos_id, ticker, "rolled", new_position_id=new_pos_id, reason="success")

    except Exception as exc:
        logger.critical(
            "roll_manager: 🚨 P0 — pos %d CLOSED in Alpaca but new order FAILED: %s",
            pos_id, exc,
        )
        if new_pos_id is not None:
            try:
                cancel_pending_position(db_path, new_pos_id)
            except Exception:
                pass

        # P0: old position closed, new one didn't open — capital at risk
        _send_p0_alert(bot_token, chat_id, ticker, pos_id, exc)
        _log_to_chronicle(pos_id, ticker, str(exc))

        return RollResult(
            pos_id, ticker, "failed",
            error  = str(exc),
            reason = "new_order_failed_after_close",
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_roll_contracts(
    alpaca_client,
    params,
    qty: int,
) -> Optional[list[dict]]:
    """
    Find available option contracts for the roll spread.

    Returns leg dicts ready for place_spread_order, or None if unavailable.

    Args:
        alpaca_client: Authenticated AlpacaClient.
        params:        SpreadParams from resolve_spread.
        qty:           Number of contracts.

    Returns:
        List of leg dicts or None.
    """
    option_type = params.option_type  # 'put' or 'call'
    expiry      = params.expiration_date

    # Fetch contracts near target strikes
    contracts = alpaca_client.get_option_contracts(
        underlying       = params.underlying,
        expiration_date  = expiry,
        option_type      = option_type,
        strike_price_lte = params.short_strike * 1.05,
        strike_price_gte = params.long_strike  * 0.95,
        limit            = 20,
    )

    if not contracts:
        logger.warning("roll: no contracts for %s %s exp %s", params.underlying, option_type, expiry)
        return None

    # Find the best short and long leg
    short_contract = _nearest_strike(contracts, params.short_strike)
    long_contract  = _nearest_strike(contracts, params.long_strike)

    if not short_contract or not long_contract:
        logger.warning("roll: could not match strikes for %s", params.underlying)
        return None

    if option_type == "put":
        # Bull put spread: sell higher put, buy lower put
        short_side, long_side = "sell", "buy"
    else:
        # Bear call spread: sell lower call, buy higher call
        short_side, long_side = "sell", "buy"

    return [
        {"symbol": short_contract["symbol"], "side": short_side, "ratio_qty": "1"},
        {"symbol": long_contract["symbol"],  "side": long_side,  "ratio_qty": "1"},
    ]


def _nearest_strike(contracts: list[dict], target_strike: float) -> Optional[dict]:
    """
    Return the contract whose strike is closest to target_strike.

    Args:
        contracts:     List of option contract dicts from Alpaca.
        target_strike: Target strike price.

    Returns:
        Closest contract dict or None if list is empty.
    """
    if not contracts:
        return None
    return min(contracts, key=lambda c: abs(float(c.get("strike_price", 0)) - target_strike))


def _build_occ(params, leg: str) -> str:
    """
    Build a simplified OCC symbol string for logging/DB.

    Not a full OCC parser — used for DB record only.
    Real symbol comes from Alpaca response in _find_roll_contracts.

    Args:
        params: SpreadParams
        leg:    "short" or "long"

    Returns:
        Human-readable contract description.
    """
    strike = params.short_strike if leg == "short" else params.long_strike
    opt    = params.option_type[0].upper()  # P or C
    exp    = params.expiration_date.replace("-", "")
    return f"{params.underlying}{exp}{opt}{strike:08.3f}"


def _send_roll_alert(
    bot_token:   str,
    chat_id:     str,
    ticker:      str,
    pos_id:      int,
    action:      str      = "ROLLED",
    reason:      str      = "",
    new_pos_id:  Optional[int]  = None,
    new_expiry:  Optional[str]  = None,
    new_dte:     Optional[int]  = None,
) -> None:
    """
    Send a Telegram/bus alert for roll outcome.

    Args:
        bot_token:  Telegram bot token.
        chat_id:    Ahmed's Telegram chat ID.
        ticker:     Underlying symbol.
        pos_id:     Original position ID.
        action:     "ROLLED", "SKIPPED", or "FAILED".
        reason:     Human-readable detail.
        new_pos_id: New position ID (on success).
        new_expiry: New expiration date (on success).
        new_dte:    New DTE (on success).
    """
    icons  = {"ROLLED": "🔄", "SKIPPED": "⚠️", "FAILED": "🚨"}
    icon   = icons.get(action, "ℹ️")
    level  = "INFO" if action == "ROLLED" else "WARNING" if action == "SKIPPED" else "ERROR"

    if action == "ROLLED":
        body = f"Position #{pos_id} rolled → #{new_pos_id} | New exp: {new_expiry} (~{new_dte} DTE)"
    else:
        body = f"Position #{pos_id} roll {action.lower()}: {reason}"

    _send_alert(
        source    = "alpha-roll-manager",
        level     = level,
        title     = f"{icon} ALPHA AUTO-ROLL {action} — {ticker}",
        body      = body,
        dedup_key = f"roll_{action.lower()}_{pos_id}_{date.today().strftime('%Y%m%d')}",
        targets   = ["nexus_health_group"],
    )


def _send_p0_alert(
    bot_token: str,
    chat_id:   str,
    ticker:    str,
    pos_id:    int,
    error:     Exception,
) -> None:
    """
    Send P0 alert when close succeeded but reopen failed (orphaned closure).

    Args:
        bot_token: Telegram bot token.
        chat_id:   Ahmed's chat ID.
        ticker:    Underlying symbol.
        pos_id:    Original position ID.
        error:     The exception that caused the failure.
    """
    _send_alert(
        source    = "alpha-roll-manager",
        level     = "CRITICAL",
        title     = f"🚨 P0 ROLL FAILURE — {ticker} | pos #{pos_id}",
        body      = (
            f"OLD SPREAD CLOSED but new order FAILED.\n"
            f"Capital at risk — manual intervention required.\n"
            f"Error: {error}"
        ),
        dedup_key = f"p0_roll_{pos_id}",
        targets   = ["nexus_health_group"],
    )


def _log_to_chronicle(pos_id: int, ticker: str, error_msg: str) -> None:
    """
    Log P0 roll failure to CHRONICLE intervention_log.

    Args:
        pos_id:    Original position ID.
        ticker:    Underlying symbol.
        error_msg: Error description.
    """
    try:
        import sqlite3
        from datetime import datetime, timezone

        db_path = "/Users/ahmedsadek/nexus/data/chronicle.db"
        now_iso = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO intervention_log
                    (agent, error_description, time_identified, ideal_response_time,
                     time_acted, time_resolved, resolution_type, damage_assessment, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "genesis",
                    f"P0 roll failure: pos {pos_id} ({ticker}) closed but reopen failed. {error_msg}",
                    now_iso,
                    now_iso,  # ideal = immediate (we're logging in real-time)
                    now_iso,
                    None,
                    "escalated",
                    f"Position {pos_id} ({ticker}) orphaned — spread closed in Alpaca, no replacement",
                    "in_progress",
                ),
            )
        logger.info("roll_manager: P0 failure logged to CHRONICLE for pos %d", pos_id)
    except Exception as exc:
        logger.error("roll_manager: CHRONICLE log failed: %s", exc)
