"""
exit_monitor.py — Alpha Execution Exit Monitor

Runs every 1 minute during market hours (changed from 15-min, approved Ahmed Sadek 2026-04-12).
Evaluates all open positions against exit rules.
Executes exits via Alpaca. Reports outcomes to Alpha Buffer.

Exit rules (approved Apr 10, 2026):
  +35%  → sell 50%, activate 15% trailing stop
  +100% → close immediately (full profit take)
  Trailing stop → close when P&L drops 15% from trailing high
  DTE ≤ 7  → close entire position
  DTE ≤ 5  → emergency close
  DTE ≤ 21 → flag for roll (log only in MVP — manual roll)
"""

import logging
from datetime import date, datetime, timezone
from typing import Optional

from ails_reporter import post_outcome as _post_ails_outcome

from config import (
    DTE_CLOSE_THRESHOLD,
    DTE_EMERGENCY_CLOSE,
    DTE_ROLL_THRESHOLD,
    EXIT_FULL_CLOSE_PCT,
    EXIT_PARTIAL_SELL_FRACTION,
    EXIT_PARTIAL_TRIGGER_PCT,
    EXIT_TRAILING_STOP_PCT,
)
from contract_resolver import calculate_dte
from database import (
    close_position,
    get_open_positions,
    reduce_contracts,
    update_trailing_stop,
)

logger = logging.getLogger("alpha_exec.exit_monitor")


def evaluate_exits(
    db_path:       str,
    alpaca_client,
    buffer_reporter,
    bot_token:     str,
    chat_id:       str,
) -> list[dict]:
    """
    Evaluate all open positions against exit rules.

    Runs on every scheduled tick (every 1 min during market hours).

    Args:
        db_path:         Alpha Execution database path.
        alpaca_client:   AlpacaClient instance.
        buffer_reporter: BufferReporter for outcome reporting.
        bot_token:       Telegram bot token.
        chat_id:         Ahmed's chat ID.

    Returns:
        List of exit events that occurred this evaluation.
    """
    positions = get_open_positions(db_path)
    if not positions:
        return []

    exits: list[dict] = []

    for pos in positions:
        try:
            exit_event = _evaluate_position(
                pos, db_path, alpaca_client, buffer_reporter, bot_token, chat_id
            )
            if exit_event:
                exits.append(exit_event)
        except Exception as e:
            logger.error("Exit evaluation failed for position %d (%s): %s", pos["id"], pos["ticker"], e)

    return exits


def _evaluate_position(
    pos:             dict,
    db_path:         str,
    alpaca_client,
    buffer_reporter,
    bot_token:       str,
    chat_id:         str,
) -> Optional[dict]:
    """
    Evaluate a single position's exit conditions.

    Args:
        pos: Position dict from database.

    Returns:
        Exit event dict if position was closed/partially closed, else None.
    """
    ticker     = pos["ticker"]
    pos_id     = pos["id"]
    expiry     = pos["expiration_date"]
    dte        = calculate_dte(expiry)
    partial    = bool(pos["partial_closed"])
    trail_pct  = pos["trailing_stop_pct"]
    trail_high = pos["trailing_stop_high"]
    entry_px   = pos["entry_price"] or 0

    # ── Get current position value from Alpaca ────────────────────────────────
    current_pnl_pct = _get_current_pnl(pos, alpaca_client)
    if current_pnl_pct is None:
        logger.debug("Could not get P&L for position %d (%s) — skipping", pos_id, ticker)
        return None

    # ── DTE Emergency Close (≤ 5) ─────────────────────────────────────────────
    if dte <= DTE_EMERGENCY_CLOSE:
        logger.warning("EMERGENCY CLOSE: position %d (%s) DTE=%d", pos_id, ticker, dte)
        return _execute_close(
            pos, db_path, alpaca_client, buffer_reporter,
            bot_token, chat_id,
            reason     = "DTE_EMERGENCY",
            pnl_pct    = current_pnl_pct,
        )

    # ── DTE Close (≤ 7) ──────────────────────────────────────────────────────
    if dte <= DTE_CLOSE_THRESHOLD:
        logger.info("DTE CLOSE: position %d (%s) DTE=%d", pos_id, ticker, dte)
        return _execute_close(
            pos, db_path, alpaca_client, buffer_reporter,
            bot_token, chat_id,
            reason  = "DTE_CLOSE",
            pnl_pct = current_pnl_pct,
        )

    # ── DTE Roll Flag (≤ 21) ─────────────────────────────────────────────────
    if dte <= DTE_ROLL_THRESHOLD:
        logger.info("ROLL FLAG: position %d (%s) DTE=%d — manual roll needed", pos_id, ticker, dte)
        _send_roll_alert(bot_token, chat_id, ticker, pos_id, dte, current_pnl_pct)

    # ── +100% Full Close ──────────────────────────────────────────────────────
    if current_pnl_pct >= EXIT_FULL_CLOSE_PCT:
        logger.info("FULL PROFIT TAKE: position %d (%s) pnl=%.1f%%", pos_id, ticker, current_pnl_pct * 100)
        return _execute_close(
            pos, db_path, alpaca_client, buffer_reporter,
            bot_token, chat_id,
            reason  = "PROFIT_TARGET_100",
            pnl_pct = current_pnl_pct,
        )

    # ── Trailing Stop Check ───────────────────────────────────────────────────
    if partial and trail_pct and trail_high is not None:
        # Update trailing high
        new_high = max(trail_high, current_pnl_pct)
        if new_high > trail_high:
            update_trailing_stop(db_path, pos_id, trail_pct, new_high, partial_closed=True)
            trail_high = new_high

        # Check if trailing stop breached
        if current_pnl_pct <= trail_high - trail_pct:
            logger.info(
                "TRAILING STOP: position %d (%s) pnl=%.1f%% trail_high=%.1f%% stop=%.1f%%",
                pos_id, ticker,
                current_pnl_pct * 100, trail_high * 100, trail_pct * 100,
            )
            return _execute_close(
                pos, db_path, alpaca_client, buffer_reporter,
                bot_token, chat_id,
                reason  = "TRAILING_STOP",
                pnl_pct = current_pnl_pct,
            )

    # ── +35% Partial Close ────────────────────────────────────────────────────
    if not partial and current_pnl_pct >= EXIT_PARTIAL_TRIGGER_PCT:
        logger.info(
            "PARTIAL EXIT: position %d (%s) pnl=%.1f%% — selling 50%%",
            pos_id, ticker, current_pnl_pct * 100,
        )
        _execute_partial_close(
            pos, db_path, alpaca_client,
            fraction   = EXIT_PARTIAL_SELL_FRACTION,
            trail_pct  = EXIT_TRAILING_STOP_PCT,
            current_high = current_pnl_pct,
        )
        return {
            "event":     "PARTIAL_CLOSE",
            "position_id": pos_id,
            "ticker":    ticker,
            "pnl_pct":   current_pnl_pct,
            "reason":    "PROFIT_TARGET_35",
        }

    return None


def _get_current_pnl(pos: dict, alpaca_client) -> Optional[float]:
    """
    Calculate current P&L percentage from Alpaca position data.

    Adversarial fix #4: now queries by individual leg contract symbols (short + long)
    rather than underlying ticker. For a credit spread:
      - Short leg gain = short_entry_credit - current_short_cost (positive if option decayed)
      - Long leg gain  = current_long_value - long_entry_debit   (negative, insurance cost)
      - Net P&L        = short_gain + long_gain

    If contract symbols are not stored (legacy records), falls back to ticker-based proxy
    with a clear warning.

    Args:
        pos:           Position dict from database.
        alpaca_client: AlpacaClient instance.

    Returns:
        Current P&L as decimal (e.g. 0.35 = +35%), or None if unavailable.
    """
    short_symbol = pos.get("short_contract_symbol")
    long_symbol  = pos.get("long_contract_symbol")
    size_usd     = pos.get("position_size_usd") or 0

    if short_symbol and long_symbol and size_usd > 0:
        # ── Preferred path: per-leg P&L aggregation ──────────────────────────
        try:
            short_pos = alpaca_client.get_position(short_symbol)
            long_pos  = alpaca_client.get_position(long_symbol)

            short_pnl = float(short_pos.get("unrealized_pl", 0)) if short_pos else 0.0
            long_pnl  = float(long_pos.get("unrealized_pl",  0)) if long_pos  else 0.0
            net_pnl   = short_pnl + long_pnl

            return net_pnl / size_usd
        except Exception as e:
            logger.warning(
                "Per-leg P&L fetch failed for position %d (%s/%s): %s — falling back to ticker",
                pos.get("id"), short_symbol, long_symbol, e,
            )

    # ── Fallback: underlying ticker proxy (legacy or on per-leg failure) ──────
    # Known limitation: returns aggregated P&L across all positions on ticker.
    # Only used when contract symbols are unavailable.
    try:
        ticker     = pos["ticker"]
        alpaca_pos = alpaca_client.get_position(ticker)
        if alpaca_pos is None:
            return None
        logger.warning(
            "Position %d (%s): using ticker-based P&L proxy (contract symbols not stored)",
            pos.get("id"), ticker,
        )
        return float(alpaca_pos.get("unrealized_plpc", 0))
    except Exception as e:
        logger.debug("P&L fetch failed for %s: %s", pos.get("ticker"), e)
        return None


def _execute_close(
    pos:             dict,
    db_path:         str,
    alpaca_client,
    buffer_reporter,
    bot_token:       str,
    chat_id:         str,
    reason:          str,
    pnl_pct:         float,
) -> dict:
    """Execute a full position close via Alpaca and report outcome."""
    pos_id     = pos["id"]
    ticker     = pos["ticker"]
    size_usd   = pos["position_size_usd"] or 0
    pnl_usd    = size_usd * pnl_pct

    # ── Close on Alpaca FIRST — only mark DB closed on confirmed success ────────
    # Adversarial fix #1: close each leg by contract symbol, not by underlying ticker.
    # Closing by ticker would aggregate and close ALL positions on that symbol.
    short_symbol = pos.get("short_contract_symbol")
    long_symbol  = pos.get("long_contract_symbol")

    close_error = None
    for attempt in range(3):
        if short_symbol and long_symbol:
            # ── Two-leg spread close — track each leg independently ────────────
            # OMNI Pass 3 Finding 5: prior code caught each leg exception independently
            # and always set close_error=None + break — so dual-leg failure still
            # resulted in DB being marked closed. Fix: gate close_error=None on
            # at least one leg succeeding.
            short_ok = False
            long_ok  = False
            try:
                alpaca_client.close_position(short_symbol)
                short_ok = True
            except Exception as e_short:
                logger.warning(
                    "Short leg close failed for %s (attempt %d): %s",
                    short_symbol, attempt + 1, e_short,
                )
            try:
                alpaca_client.close_position(long_symbol)
                long_ok = True
            except Exception as e_long:
                logger.warning(
                    "Long leg close failed for %s (attempt %d): %s",
                    long_symbol, attempt + 1, e_long,
                )

            if short_ok or long_ok:
                # At least one leg closed — proceed to DB write
                if not short_ok or not long_ok:
                    logger.warning(
                        "Position %d (%s): partial leg close — short_ok=%s long_ok=%s. "
                        "Marking DB closed; orphaned leg will surface in reconciler.",
                        pos_id, ticker, short_ok, long_ok,
                    )
                close_error = None
                break
            else:
                # Both legs failed — do not mark closed; retry
                close_error = Exception(f"Both legs failed on attempt {attempt + 1}")
                if attempt < 2:
                    import time
                    time.sleep(2 ** attempt)
                else:
                    logger.error(
                        "ALPACA CLOSE FAILED (both legs) after 3 attempts for position %d (%s) — "
                        "NOT marking DB closed. Will retry on next exit monitor tick.",
                        pos_id, ticker,
                    )
                    return {
                        "event":       "CLOSE_FAILED",
                        "position_id": pos_id,
                        "ticker":      ticker,
                        "reason":      reason,
                        "error":       "Both spread legs failed to close after 3 attempts",
                    }
        else:
            # ── Legacy single-ticker fallback ─────────────────────────────────
            try:
                logger.warning(
                    "Position %d (%s): no contract symbols stored — closing by ticker (may affect wrong legs)",
                    pos_id, ticker,
                )
                alpaca_client.close_position(ticker)
                close_error = None
                break
            except Exception as e:
                close_error = e
                if attempt < 2:
                    import time
                    time.sleep(2 ** attempt)
                else:
                    logger.error(
                        "ALPACA CLOSE FAILED after 3 attempts for position %d (%s) — "
                        "NOT marking DB closed. Will retry on next exit monitor tick. Error: %s",
                        pos_id, ticker, e,
                    )
                    return {
                        "event":       "CLOSE_FAILED",
                        "position_id": pos_id,
                        "ticker":      ticker,
                        "reason":      reason,
                        "error":       str(e)[:200],
                    }

    logger.info("Position %d (%s) closed on Alpaca: reason=%s pnl=%.1f%%", pos_id, ticker, reason, pnl_pct * 100)

    # Alpaca confirmed — now safe to update DB
    close_position(db_path, pos_id, reason, pnl_pct, pnl_usd)

    # Report back to Alpha Buffer for circuit breaker
    try:
        buffer_reporter.report_outcome(
            ticker  = ticker,
            won     = pnl_pct > 0,
            pnl_pct = pnl_pct,
            pathway = pos.get("pathway", "P1"),
        )
    except Exception as e:
        logger.warning("Buffer outcome report failed for %s: %s", ticker, e)

    # Telegram notification
    _send_close_notification(bot_token, chat_id, ticker, pos_id, reason, pnl_pct, pnl_usd)

    # Post outcome to AILS for Bayesian learning (fire-and-forget)
    _post_ails_outcome(
        ticker=ticker,
        strategy=pos.get("option_type", "bull_put_spread"),
        regime=pos.get("regime", "UNKNOWN"),
        direction=pos.get("direction", "bullish"),
        pnl_usd=pnl_usd,
        win=pnl_pct > 0,
        system="alpha",
        concordance_path=pos.get("pathway"),
    )

    return {
        "event":       "FULL_CLOSE",
        "position_id": pos_id,
        "ticker":      ticker,
        "reason":      reason,
        "pnl_pct":     pnl_pct,
        "pnl_usd":     pnl_usd,
    }


def _execute_partial_close(
    pos:          dict,
    db_path:      str,
    alpaca_client,
    fraction:     float,
    trail_pct:    float,
    current_high: float,
) -> None:
    """
    Execute partial close and activate trailing stop.

    Adversarial fix #3: reduces the DB contracts count by qty_closed after partial close.
    Adversarial fix #10: trailing stop parameters are ALWAYS set regardless of whether
    the Alpaca partial close succeeds, ensuring the stop is active even on partial failure.
    Adversarial fix #1: closes by short contract symbol, not by ticker.
    """
    pos_id       = pos["id"]
    ticker       = pos["ticker"]
    contracts    = pos.get("contracts", 1)
    close_qty    = max(1, int(contracts * fraction))
    short_symbol = pos.get("short_contract_symbol")
    long_symbol  = pos.get("long_contract_symbol")

    # ── Always set trailing stop FIRST (fix #10) ─────────────────────────────
    # Even if the Alpaca call below fails, the exit monitor will see partial_closed=True
    # on the next tick and enforce the trailing stop. This is the safe default.
    update_trailing_stop(
        db_path, pos_id,
        trailing_stop_pct  = trail_pct,
        trailing_stop_high = current_high,
        partial_closed     = True,
    )

    # ── Reduce contract count in DB (fix #3) ─────────────────────────────────
    remaining = reduce_contracts(db_path, pos_id, close_qty)
    logger.info(
        "Partial close: position %d (%s) — closing %d contracts, %d remaining",
        pos_id, ticker, close_qty, remaining,
    )

    # ── Execute partial close on Alpaca — BOTH legs (OMNI Pass 3 Finding 4) ──
    # Prior code only closed the short leg, leaving the long leg at full size.
    # After partial close: short leg has fewer contracts than long leg → unhedged
    # directional long exposure + wrong P&L calculation on next monitor tick.
    # Fix: reduce BOTH legs by close_qty simultaneously.
    try:
        if short_symbol and long_symbol:
            # Close both legs of the spread
            try:
                alpaca_client.close_position(short_symbol, qty=close_qty)
            except Exception as e_short:
                logger.error(
                    "Partial close short leg failed for position %d (%s): %s",
                    pos_id, short_symbol, e_short,
                )
            try:
                alpaca_client.close_position(long_symbol, qty=close_qty)
            except Exception as e_long:
                logger.error(
                    "Partial close long leg failed for position %d (%s): %s",
                    pos_id, long_symbol, e_long,
                )
        elif short_symbol:
            # Single-leg (unusual) or legacy position without long_contract_symbol
            alpaca_client.close_position(short_symbol, qty=close_qty)
        else:
            # Fallback: close by ticker (legacy positions without contract symbols)
            logger.warning(
                "Position %d (%s): no contract symbols — partial close by ticker",
                pos_id, ticker,
            )
            alpaca_client.close_position(ticker, qty=close_qty)
    except Exception as e:
        logger.error(
            "Partial close Alpaca call failed for position %d (%s): %s — "
            "trailing stop is active; full close will fire on next tick if stop breaches",
            pos_id, ticker, e,
        )


def _send_close_notification(
    bot_token: str, chat_id: str,
    ticker: str, pos_id: int,
    reason: str, pnl_pct: float, pnl_usd: float,
) -> None:
    """Send trade closed Telegram notification."""
    import requests
    emoji  = "✅" if pnl_pct > 0 else "❌"
    result = "WIN" if pnl_pct > 0 else "LOSS"
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       (
                    f"{emoji} <b>ALPHA TRADE CLOSED — {ticker}</b>\n"
                    f"Result: {result} | P&L: {pnl_pct*100:+.1f}% (${pnl_usd:+,.0f})\n"
                    f"Reason: {reason} | Position #{pos_id}"
                ),
                "parse_mode": "HTML",
            },
            timeout=8,
        )
    except Exception:
        pass


def _send_roll_alert(
    bot_token: str, chat_id: str,
    ticker: str, pos_id: int,
    dte: int, pnl_pct: float,
) -> None:
    """Send roll reminder for positions approaching DTE threshold."""
    import requests
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       (
                    f"⏰ <b>ALPHA ROLL NEEDED — {ticker}</b>\n"
                    f"DTE: {dte} | P&L: {pnl_pct*100:+.1f}%\n"
                    f"Position #{pos_id} — consider rolling forward"
                ),
                "parse_mode": "HTML",
            },
            timeout=8,
        )
    except Exception:
        pass
