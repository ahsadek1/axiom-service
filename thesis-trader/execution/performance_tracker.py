"""
performance_tracker.py — THESIS Risk-Adjusted Performance
==========================================================
Layer 7: The truth teller.

Tracks:
  - Sharpe ratio (risk-adjusted returns)
  - Sortino ratio (downside-only risk)
  - Max drawdown (worst peak-to-trough)
  - Win rate vs backtest prediction
  - Premium capture rate (are we getting fair value?)
  - Theta decay curve (is the strategy working as designed?)
  - Expectancy (avg profit per trade)

This is what tells you definitively:
  Is the edge REAL or just lucky?
  Is the system ready for live capital?

Live capital threshold:
  Sharpe > 1.5 over 30+ trades
  Win rate within 5% of backtest prediction
  Max drawdown < 15% of capital
  Expectancy > $200 per trade
"""
from __future__ import annotations
import logging
import os
import sqlite3
import statistics
import time
from datetime import datetime, date, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("thesis.performance")
_ET = ZoneInfo("America/New_York")

POSITIONS_DB  = os.getenv("THESIS_POSITIONS_DB",
                          "/Users/ahmedsadek/nexus/data/thesis_positions.db")
TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN","")
TG_CHAT       = os.getenv("THESIS_CHAT_ID", os.getenv("AHMED_CHAT_ID",""))

# Live capital readiness thresholds
LIVE_SHARPE_MIN      = 1.5
LIVE_WINRATE_TOL     = 0.05   # within 5% of backtest
LIVE_DRAWDOWN_MAX    = 0.15   # max 15% drawdown
LIVE_EXPECTANCY_MIN  = 200.0  # min $200 per trade
LIVE_MIN_TRADES      = 30     # minimum sample size


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


def get_closed_positions(days: int = 90) -> list[dict]:
    """Load closed positions for performance calculation."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    try:
        conn = sqlite3.connect(POSITIONS_DB, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM positions
            WHERE status='CLOSED' AND DATE(exit_time) >= ?
            ORDER BY exit_time ASC
        """, (cutoff,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.error("Failed to load positions: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Core metrics
# ---------------------------------------------------------------------------

def calculate_sharpe(pnls: list[float], periods_per_year: int = 252) -> float:
    """Annualized Sharpe ratio."""
    if len(pnls) < 2:
        return 0.0
    avg = statistics.mean(pnls)
    std = statistics.stdev(pnls)
    if std == 0:
        return 0.0
    return round((avg / std) * (periods_per_year ** 0.5), 2)


def calculate_sortino(pnls: list[float], periods_per_year: int = 252) -> float:
    """Sortino ratio — only penalizes downside volatility."""
    if len(pnls) < 2:
        return 0.0
    avg = statistics.mean(pnls)
    losses = [p for p in pnls if p < 0]
    if not losses:
        return 10.0  # no losses = perfect Sortino
    downside_std = statistics.stdev(losses) if len(losses) > 1 else abs(losses[0])
    if downside_std == 0:
        return 0.0
    return round((avg / downside_std) * (periods_per_year ** 0.5), 2)


def calculate_max_drawdown(pnls: list[float]) -> tuple[float, float]:
    """
    Max drawdown as dollar amount and percentage of peak capital.
    Returns (max_drawdown_dollars, max_drawdown_pct).
    """
    if not pnls:
        return 0.0, 0.0

    cumulative = 0.0
    peak       = 0.0
    max_dd     = 0.0

    for pnl in pnls:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # As percentage of starting capital
    starting_capital = float(os.getenv("THESIS_STARTING_CAPITAL", "200000"))
    dd_pct = max_dd / starting_capital if starting_capital > 0 else 0

    return round(max_dd, 2), round(dd_pct, 4)


def calculate_expectancy(positions: list[dict]) -> dict:
    """
    Expectancy = (win_rate × avg_win) - (loss_rate × avg_loss)
    Tells you the expected value of each trade.
    """
    if not positions:
        return {"expectancy": 0, "avg_win": 0, "avg_loss": 0,
                "win_rate": 0, "profit_factor": 0}

    wins   = [p["exit_pnl"] for p in positions if (p["exit_pnl"] or 0) > 0]
    losses = [abs(p["exit_pnl"]) for p in positions if (p["exit_pnl"] or 0) <= 0]

    win_rate  = len(wins) / len(positions) if positions else 0
    avg_win   = statistics.mean(wins)   if wins   else 0
    avg_loss  = statistics.mean(losses) if losses else 0

    expectancy     = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
    profit_factor  = (win_rate * avg_win) / ((1-win_rate) * avg_loss) \
                    if losses and (1-win_rate) > 0 else float("inf")

    return {
        "expectancy":    round(expectancy, 2),
        "avg_win":       round(avg_win, 2),
        "avg_loss":      round(avg_loss, 2),
        "win_rate":      round(win_rate, 3),
        "profit_factor": round(profit_factor, 2),
    }


def calculate_premium_capture(positions: list[dict]) -> float:
    """
    Premium capture rate: what % of theoretical max profit did we actually capture?
    Target: > 40% (theta decay strategy benchmark)
    """
    if not positions:
        return 0.0

    captures = []
    for p in positions:
        max_profit = p.get("max_profit", 0)
        exit_pnl   = p.get("exit_pnl", 0) or 0
        if max_profit and max_profit > 0:
            captures.append(exit_pnl / max_profit)

    return round(statistics.mean(captures), 3) if captures else 0.0


def calculate_win_rate_calibration(positions: list[dict]) -> dict:
    """
    Compare live win rate to backtest predictions.
    Is the 99% holding up in real trading?
    """
    if not positions:
        return {"live_wr": 0, "predicted_wr": 0, "calibration": 0, "status": "NO_DATA"}

    wins          = sum(1 for p in positions if (p.get("exit_pnl") or 0) > 0)
    live_wr       = wins / len(positions)
    predicted_wrs = [p.get("win_rate", 0) or 0 for p in positions if p.get("win_rate")]
    predicted_wr  = statistics.mean(predicted_wrs) if predicted_wrs else 0

    calibration = live_wr - predicted_wr
    if abs(calibration) <= LIVE_WINRATE_TOL:
        status = "CALIBRATED"
    elif calibration < -LIVE_WINRATE_TOL:
        status = "UNDERPERFORMING"
    else:
        status = "OUTPERFORMING"

    return {
        "live_wr":     round(live_wr, 3),
        "predicted_wr": round(predicted_wr, 3),
        "calibration":  round(calibration, 3),
        "status":       status,
    }


# ---------------------------------------------------------------------------
# Complete performance report
# ---------------------------------------------------------------------------

def generate_performance_report(days: int = 30) -> dict:
    """Generate comprehensive performance report."""
    positions = get_closed_positions(days)

    if not positions:
        return {
            "status":  "NO_DATA",
            "message": f"No closed positions in last {days} days",
            "trades":  0,
        }

    pnls = [float(p.get("exit_pnl") or 0) for p in positions]

    sharpe       = calculate_sharpe(pnls)
    sortino      = calculate_sortino(pnls)
    max_dd, dd_pct = calculate_max_drawdown(pnls)
    expectancy   = calculate_expectancy(positions)
    capture_rate = calculate_premium_capture(positions)
    calibration  = calculate_win_rate_calibration(positions)

    total_pnl    = sum(pnls)
    win_rate     = expectancy["win_rate"]
    trade_count  = len(positions)

    # Live capital readiness assessment
    ready_checks = {
        "sharpe":    sharpe >= LIVE_SHARPE_MIN,
        "win_rate":  calibration["status"] in ("CALIBRATED", "OUTPERFORMING"),
        "drawdown":  dd_pct <= LIVE_DRAWDOWN_MAX,
        "expectancy": expectancy["expectancy"] >= LIVE_EXPECTANCY_MIN,
        "sample":    trade_count >= LIVE_MIN_TRADES,
    }
    ready_count = sum(ready_checks.values())
    live_ready  = all(ready_checks.values())

    report = {
        "period_days":    days,
        "trade_count":    trade_count,
        "total_pnl":      round(total_pnl, 2),
        "win_rate":       win_rate,
        "sharpe":         sharpe,
        "sortino":        sortino,
        "max_drawdown":   max_dd,
        "max_dd_pct":     dd_pct,
        "expectancy":     expectancy,
        "premium_capture": capture_rate,
        "calibration":    calibration,
        "live_ready":     live_ready,
        "live_checks":    ready_checks,
        "live_score":     f"{ready_count}/5",
        "timestamp":      datetime.now(timezone.utc).isoformat(),
    }

    log.info(
        "Performance: %d trades | Sharpe=%.2f | WR=%.0f%% | "
        "DD=%.1f%% | Expectancy=$%.0f | Live ready: %s",
        trade_count, sharpe, win_rate*100, dd_pct*100,
        expectancy["expectancy"], live_ready
    )

    return report


def send_performance_report(days: int = 30) -> None:
    """Send performance report to Telegram."""
    r = generate_performance_report(days)

    if r.get("status") == "NO_DATA":
        _notify(f"<b>THESIS Performance</b>\nNo closed positions yet.")
        return

    checks = r.get("live_checks", {})
    check_lines = [
        f"  {'✅' if checks.get('sharpe') else '❌'} Sharpe > 1.5: {r['sharpe']:.2f}",
        f"  {'✅' if checks.get('win_rate') else '❌'} Win rate calibrated: {r['calibration']['status']}",
        f"  {'✅' if checks.get('drawdown') else '❌'} Drawdown < 15%: {r['max_dd_pct']:.1%}",
        f"  {'✅' if checks.get('expectancy') else '❌'} Expectancy > $200: ${r['expectancy']['expectancy']:,.0f}",
        f"  {'✅' if checks.get('sample') else '❌'} Sample >= 30: {r['trade_count']} trades",
    ]

    live_emoji = "🟢" if r["live_ready"] else "🔴"

    msg = (
        f"<b>THESIS Performance Report ({r['period_days']}d)</b>\n\n"
        f"Trades: {r['trade_count']} | P&L: ${r['total_pnl']:+,.0f}\n"
        f"Win rate: {r['win_rate']:.0%} | Expectancy: ${r['expectancy']['expectancy']:+,.0f}\n"
        f"Sharpe: {r['sharpe']:.2f} | Sortino: {r['sortino']:.2f}\n"
        f"Max drawdown: {r['max_dd_pct']:.1%}\n"
        f"Premium capture: {r['premium_capture']:.0%}\n"
        f"WR calibration: {r['calibration']['live_wr']:.0%} live "
        f"vs {r['calibration']['predicted_wr']:.0%} predicted\n\n"
        f"{live_emoji} <b>Live Capital Ready: {r['live_score']}</b>\n"
        + "\n".join(check_lines)
    )

    _notify(msg)
