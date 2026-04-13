"""
exit_monitor.py — Prime Execution Exit Monitor

Runs every 5 minutes during market hours (changed from 15-min, approved Ahmed Sadek 2026-04-12).
Evaluates all open swing positions against Prime exit rules.

Exit rules (approved Apr 10, 2026):
  +35%  → sell 50%, activate 12% trailing stop
  +50%  → tighten trailing stop to 8%
  Trailing stop → close when P&L drops below (high - trail_pct)
  Technical invalidation → primary stop (flagged externally)
  -18%  → hard backstop close
"""

import logging

from ails_reporter import post_outcome as _post_ails_outcome
from typing import Optional

from config import (
    EXIT_HARD_BACKSTOP_PCT,
    EXIT_PARTIAL_SELL_FRACTION,
    EXIT_PARTIAL_TRIGGER_PCT,
    EXIT_TRAILING_STOP_PCT,
    EXIT_TRAILING_TIGHTEN_AT_PCT,
    EXIT_TRAILING_TIGHT_PCT,
)
from database import (
    close_position,
    get_open_positions,
    get_position_by_id,
    update_trailing_stop,
)

logger = logging.getLogger("prime_exec.exit_monitor")


def evaluate_exits(
    db_path: str,
    alpaca_client,
    buffer_reporter,
    bot_token: str,
    chat_id:   str,
) -> list[dict]:
    """
    Evaluate all open Prime positions against exit rules.

    Args:
        db_path:         Prime Execution database path.
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
            event = _evaluate_position(pos, db_path, alpaca_client, buffer_reporter, bot_token, chat_id)
            if event:
                exits.append(event)
        except Exception as e:
            logger.error("Exit evaluation error for position %d (%s): %s", pos["id"], pos["ticker"], e)

    return exits


def _evaluate_position(
    pos: dict, db_path: str,
    alpaca_client, buffer_reporter,
    bot_token: str, chat_id: str,
) -> Optional[dict]:
    """Evaluate a single Prime position's exit conditions."""
    pos_id    = pos["id"]
    ticker    = pos["ticker"]
    partial   = bool(pos["partial_closed"])
    trail_pct = pos["trailing_stop_pct"]
    trail_high = pos["trailing_stop_high"]

    current_pnl = _get_current_pnl(pos, alpaca_client)
    if current_pnl is None:
        return None

    # ── Technical Invalidation (primary stop) ────────────────────────────────
    if pos.get("technical_stop_flagged"):
        logger.info("TECHNICAL STOP: position %d (%s) — closing", pos_id, ticker)
        return _do_close(pos, db_path, alpaca_client, buffer_reporter, bot_token, chat_id,
                         reason="TECHNICAL_STOP", pnl_pct=current_pnl)

    # ── Hard Backstop (-18%) ─────────────────────────────────────────────────
    if current_pnl <= EXIT_HARD_BACKSTOP_PCT:
        logger.warning("BACKSTOP HIT: position %d (%s) pnl=%.1f%%", pos_id, ticker, current_pnl * 100)
        return _do_close(pos, db_path, alpaca_client, buffer_reporter, bot_token, chat_id,
                         reason="HARD_BACKSTOP", pnl_pct=current_pnl)

    # ── Trailing Stop Check ───────────────────────────────────────────────────
    if partial and trail_pct and trail_high is not None:
        new_high = max(trail_high, current_pnl)
        if new_high > trail_high:
            update_trailing_stop(db_path, pos_id, trail_pct, new_high, partial_closed=True)
            trail_high = new_high

        # Tighten trail at +50%
        if current_pnl >= EXIT_TRAILING_TIGHTEN_AT_PCT and trail_pct > EXIT_TRAILING_TIGHT_PCT:
            update_trailing_stop(db_path, pos_id, EXIT_TRAILING_TIGHT_PCT, trail_high, partial_closed=True)
            trail_pct = EXIT_TRAILING_TIGHT_PCT
            logger.info("TRAIL TIGHTENED: position %d (%s) → 8%%", pos_id, ticker)

        if current_pnl <= trail_high - trail_pct:
            logger.info("TRAILING STOP: position %d (%s) pnl=%.1f%% high=%.1f%%",
                        pos_id, ticker, current_pnl * 100, trail_high * 100)
            return _do_close(pos, db_path, alpaca_client, buffer_reporter, bot_token, chat_id,
                             reason="TRAILING_STOP", pnl_pct=current_pnl)

    # ── +35% Partial Close ────────────────────────────────────────────────────
    if not partial and current_pnl >= EXIT_PARTIAL_TRIGGER_PCT:
        _do_partial_close(pos, db_path, alpaca_client)
        logger.info("PARTIAL CLOSE: position %d (%s) pnl=%.1f%%", pos_id, ticker, current_pnl * 100)
        return {"event": "PARTIAL_CLOSE", "position_id": pos_id, "ticker": ticker, "pnl_pct": current_pnl}

    return None


def _get_current_pnl(pos: dict, alpaca_client) -> Optional[float]:
    try:
        alpaca_pos = alpaca_client.get_position(pos["ticker"])
        if alpaca_pos is None:
            return None
        return float(alpaca_pos.get("unrealized_plpc", 0))
    except Exception as e:
        logger.debug("P&L fetch failed for %s: %s", pos["ticker"], e)
        return None


def _do_close(pos, db_path, alpaca_client, buffer_reporter, bot_token, chat_id, reason, pnl_pct):
    pos_id  = pos["id"]
    ticker  = pos["ticker"]
    pnl_usd = (pos["position_size_usd"] or 0) * pnl_pct

    # ── Alpaca close FIRST — INV-1: DB write ONLY on confirmed Alpaca success ──
    # OMNI Pass 3 Finding 2: prior code caught Alpaca failure and always called
    # close_position() — creating phantom-closed positions (live on Alpaca,
    # orphaned, unmonitored, no stop-loss). Now mirrors Alpha _execute_close pattern.
    for attempt in range(3):
        try:
            alpaca_client.close_position(ticker)
            break  # Alpaca confirmed — proceed to DB write
        except Exception as e:
            if attempt < 2:
                import time
                time.sleep(2 ** attempt)
            else:
                logger.error(
                    "Alpaca close FAILED after 3 attempts for Prime position %d (%s) — "
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

    close_position(db_path, pos_id, reason, pnl_pct, pnl_usd)
    try:
        buffer_reporter.report_outcome(ticker=ticker, won=pnl_pct > 0, pnl_pct=pnl_pct, pathway=pos.get("pathway"))
    except Exception as e:
        logger.warning("Buffer report failed for %s: %s", ticker, e)
    _notify_close(bot_token, chat_id, ticker, pos_id, reason, pnl_pct, pnl_usd)

    # Post outcome to AILS for Bayesian learning (fire-and-forget)
    _post_ails_outcome(
        ticker=ticker,
        strategy=pos.get("strategy", "swing_long"),
        regime=pos.get("regime", "UNKNOWN"),
        direction=pos.get("direction", "bullish"),
        pnl_usd=pnl_usd,
        win=pnl_pct > 0,
        system="prime",
        concordance_path=pos.get("pathway"),
    )

    return {"event": "FULL_CLOSE", "position_id": pos_id, "ticker": ticker, "reason": reason, "pnl_pct": pnl_pct}


def _do_partial_close(pos, db_path, alpaca_client):
    """
    Sell 50% of remaining shares and activate trailing stop.

    Adversarial fix #2: if remaining shares <= 0 after partial sale, the position is
    fully consumed. Mark it closed — do not leave a phantom zero-share open record.

    Adversarial fix (trailing stop): trailing stop set FIRST, before Alpaca call,
    so monitoring is active even if the API call fails.
    """
    pos_id           = pos["id"]
    ticker           = pos["ticker"]
    shares_remaining = pos.get("shares_remaining") or pos.get("shares", 1)
    # Cipher Pass 3 P3-11: must cast to int — Alpaca equity API rejects fractional
    # qty for non-fractional-trading accounts (e.g., 15 shares × 0.5 = 7.5 → rejected).
    close_shares     = max(1, int(shares_remaining * EXIT_PARTIAL_SELL_FRACTION))
    remaining        = shares_remaining - close_shares

    # Always set trailing stop FIRST — active even if Alpaca call below fails
    update_trailing_stop(db_path, pos_id, EXIT_TRAILING_STOP_PCT,
                         pos.get("trailing_stop_high") or EXIT_PARTIAL_TRIGGER_PCT,
                         partial_closed=True, shares_remaining=max(0.0, remaining))

    try:
        alpaca_client.close_position(ticker, qty=close_shares)
    except Exception as e:
        logger.error(
            "Partial close failed for position %d (%s): %s — "
            "trailing stop is active; full close fires on next tick if stop breaches",
            pos_id, ticker, e,
        )

    # If nothing remains, mark position fully closed
    if remaining <= 0:
        logger.info("Partial close consumed all shares for position %d (%s) — marking closed", pos_id, ticker)
        pnl_pct = _get_current_pnl(pos, alpaca_client) or 0.0
        pnl_usd = (pos.get("position_size_usd") or 0) * pnl_pct
        close_position(db_path, pos_id, reason="PARTIAL_FULLY_CONSUMED", pnl_pct=pnl_pct, pnl_usd=pnl_usd)


def _notify_close(bot_token, chat_id, ticker, pos_id, reason, pnl_pct, pnl_usd):
    import requests
    emoji  = "✅" if pnl_pct > 0 else "❌"
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "parse_mode": "HTML",
                  "text": (f"{emoji} <b>PRIME TRADE CLOSED — {ticker}</b>\n"
                           f"P&L: {pnl_pct*100:+.1f}% (${pnl_usd:+,.0f}) | {reason}\n"
                           f"Position #{pos_id}")},
            timeout=8,
        )
    except Exception:
        pass
