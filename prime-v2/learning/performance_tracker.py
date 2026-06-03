"""
performance_tracker.py — Prime V2 Learning & Performance
==========================================================
Tracks performance by strategy arm, tier, and market condition.
Calibrates parameters weekly based on live results.

Tracks:
  - Win rate by strategy (PRE_EARNINGS / POST_EARNINGS / MOMENTUM)
  - Win rate by tier (A / B / C)
  - Avg ROI by strategy and tier
  - Best sectors and market regimes
  - Double signal performance vs single signal
  - Agent count correlation with outcomes

Weekly calibration:
  - Adjusts tier thresholds if actual moves diverge from expected
  - Shifts capital allocation between arms based on performance
  - Promotes/demotes sectors based on recent results
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

log = logging.getLogger("prime_v2.performance")
_ET = ZoneInfo("America/New_York")

PRIME_V2_DB = os.getenv("PRIME_V2_DB",
              "/Users/ahmedsadek/nexus/data/prime_v2.db")
TG_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN","")
TG_CHAT     = os.getenv("AHMED_CHAT_ID","")


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


def get_closed_positions(days: int = 30) -> list[dict]:
    """Load closed positions for analysis."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    try:
        conn = sqlite3.connect(PRIME_V2_DB, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM prime_v2_positions
            WHERE status='CLOSED' AND DATE(exit_time) >= ?
            ORDER BY exit_time ASC
        """, (cutoff,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def analyze_by_strategy(positions: list[dict]) -> dict:
    """Break down performance by strategy arm."""
    strategies = {}
    for p in positions:
        s = p.get("strategy","UNKNOWN")
        if s not in strategies:
            strategies[s] = {"trades":[], "wins":[], "pnls":[]}
        pnl = float(p.get("pnl_pct") or 0)
        strategies[s]["trades"].append(p)
        strategies[s]["pnls"].append(pnl)
        if pnl > 0:
            strategies[s]["wins"].append(pnl)

    results = {}
    for s, data in strategies.items():
        n     = len(data["trades"])
        wins  = len(data["wins"])
        pnls  = data["pnls"]
        results[s] = {
            "trades":    n,
            "win_rate":  round(wins/n, 3) if n > 0 else 0,
            "avg_pnl":   round(statistics.mean(pnls)*100, 2) if pnls else 0,
            "total_pnl": round(sum(p.get("pnl_usd",0) or 0
                               for p in data["trades"]), 2),
            "best":      round(max(pnls)*100, 2) if pnls else 0,
            "worst":     round(min(pnls)*100, 2) if pnls else 0,
        }
    return results


def analyze_by_tier(positions: list[dict]) -> dict:
    """Break down performance by tier."""
    tiers = {}
    for p in positions:
        t = p.get("tier","UNKNOWN")
        if t not in tiers:
            tiers[t] = {"n":0,"wins":0,"pnls":[]}
        pnl = float(p.get("pnl_pct") or 0)
        tiers[t]["n"]    += 1
        tiers[t]["pnls"].append(pnl)
        if pnl > 0:
            tiers[t]["wins"] += 1

    results = {}
    for t, data in tiers.items():
        n    = data["n"]
        pnls = data["pnls"]
        results[t] = {
            "trades":   n,
            "win_rate": round(data["wins"]/n, 3) if n > 0 else 0,
            "avg_pnl":  round(statistics.mean(pnls)*100, 2) if pnls else 0,
        }
    return results


def analyze_double_signals(positions: list[dict]) -> dict:
    """Compare double signal vs single signal performance."""
    double = [p for p in positions if p.get("double_signal")]
    single = [p for p in positions if not p.get("double_signal")]

    def stats(group):
        if not group:
            return {"trades":0,"win_rate":0,"avg_pnl":0}
        pnls = [float(p.get("pnl_pct") or 0) for p in group]
        wins = sum(1 for p in pnls if p > 0)
        return {
            "trades":   len(group),
            "win_rate": round(wins/len(group), 3),
            "avg_pnl":  round(statistics.mean(pnls)*100, 2),
        }

    return {
        "double_signal": stats(double),
        "single_signal": stats(single),
    }


def calculate_sharpe(positions: list[dict]) -> float:
    """Approximate Sharpe ratio."""
    if len(positions) < 3:
        return 0.0
    pnls = [float(p.get("pnl_pct") or 0) for p in positions]
    avg  = statistics.mean(pnls)
    std  = statistics.stdev(pnls)
    if std == 0:
        return 0.0
    return round((avg / std) * (252 ** 0.5), 2)


def generate_report(days: int = 7) -> dict:
    """Generate comprehensive performance report."""
    positions = get_closed_positions(days)

    if not positions:
        return {"status": "NO_DATA", "days": days, "trades": 0}

    pnls      = [float(p.get("pnl_pct") or 0) for p in positions]
    pnl_usd   = [float(p.get("pnl_usd") or 0) for p in positions]
    wins      = sum(1 for p in pnls if p > 0)
    total_pnl = sum(pnl_usd)

    report = {
        "period_days":      days,
        "total_trades":     len(positions),
        "win_rate":         round(wins/len(positions), 3),
        "total_pnl_usd":    round(total_pnl, 2),
        "avg_pnl_pct":      round(statistics.mean(pnls)*100, 2),
        "sharpe":           calculate_sharpe(positions),
        "by_strategy":      analyze_by_strategy(positions),
        "by_tier":          analyze_by_tier(positions),
        "double_signals":   analyze_double_signals(positions),
        "timestamp":        datetime.now(timezone.utc).isoformat(),
    }
    return report


def send_daily_report() -> None:
    """Send EOD performance report to Telegram."""
    report = generate_report(days=1)
    week   = generate_report(days=7)

    if report.get("status") == "NO_DATA":
        return

    by_strat  = report.get("by_strategy", {})
    strat_lines = []
    for s, d in by_strat.items():
        strat_lines.append(
            f"  {s[:15]:15}: {d['trades']} trades | "
            f"WR={d['win_rate']:.0%} | avg={d['avg_pnl']:+.1f}%"
        )

    msg = (
        f"<b>PRIME V2 DAILY REPORT</b>\n\n"
        f"Today: {report['total_trades']} trades | "
        f"WR={report['win_rate']:.0%} | "
        f"P&L=${report['total_pnl_usd']:+,.0f}\n\n"
        f"<b>By Strategy:</b>\n" +
        "\n".join(strat_lines) +
        f"\n\n<b>Week:</b> {week.get('total_trades',0)} trades | "
        f"WR={week.get('win_rate',0):.0%} | "
        f"P&L=${week.get('total_pnl_usd',0):+,.0f} | "
        f"Sharpe={week.get('sharpe',0):.2f}"
    )
    _notify(msg)


def send_weekly_calibration() -> None:
    """
    Weekly calibration — analyze what's working and adjust.
    Sends recommendations to Telegram.
    """
    report = generate_report(days=30)
    if report.get("status") == "NO_DATA":
        return

    by_strat  = report.get("by_strategy", {})
    by_tier   = report.get("by_tier", {})
    doubles   = report.get("double_signals", {})

    lines = [f"<b>PRIME V2 WEEKLY CALIBRATION</b>",
             f"Period: 30 days | Trades: {report['total_trades']}",
             f"Win rate: {report['win_rate']:.0%} | Sharpe: {report['sharpe']:.2f}",
             ""]

    # Strategy recommendations
    lines.append("<b>Strategy Performance:</b>")
    for s, d in sorted(by_strat.items(), key=lambda x: -x[1]["avg_pnl"]):
        emoji = "✅" if d["win_rate"] >= 0.55 else "⚠️"
        lines.append(f"{emoji} {s[:15]:15}: WR={d['win_rate']:.0%} avg={d['avg_pnl']:+.1f}%")

    # Double signal performance
    ds = doubles.get("double_signal",{})
    ss = doubles.get("single_signal",{})
    lines += [
        "",
        "<b>Double Signal Edge:</b>",
        f"Double: WR={ds.get('win_rate',0):.0%} avg={ds.get('avg_pnl',0):+.1f}%",
        f"Single: WR={ss.get('win_rate',0):.0%} avg={ss.get('avg_pnl',0):+.1f}%",
    ]

    # Calibration recommendations
    lines.append("")
    lines.append("<b>Recommendations:</b>")

    pre = by_strat.get("PRE_EARNINGS",{})
    post = by_strat.get("POST_EARNINGS",{})
    mom  = by_strat.get("MOMENTUM_BREAKOUT",{})

    if pre.get("win_rate",0) > post.get("win_rate",0.5):
        lines.append("→ Increase pre-earnings allocation (+5%)")
    if mom.get("avg_pnl",0) < 3.0 and mom.get("trades",0) > 5:
        lines.append("→ Raise momentum Tier C threshold")
    if ds.get("avg_pnl",0) > ss.get("avg_pnl",0) * 1.3:
        lines.append("→ Prioritize double signals — significant edge confirmed")

    _notify("\n".join(lines))
