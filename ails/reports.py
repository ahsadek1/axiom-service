"""
reports.py — EOD and EOW report generation.
Nothing auto-applies. All recommendations require Ahmed approval.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import requests

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_AHMED_CHAT_ID,
    DRIFT_ALERT_PCT,
)
from regime import get_current_regime

log = logging.getLogger(__name__)


def _send_telegram(message: str) -> None:
    """Send message to Ahmed via Telegram."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_AHMED_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        log.error("Telegram send failed: %s", exc)


def generate_eod_report(
    ails_conn: sqlite3.Connection,
    backtest_conn: sqlite3.Connection,
    date_str: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate end-of-day performance and learning report.

    Args:
        ails_conn:     Live AILS DB connection
        backtest_conn: Historical backtest DB connection
        date_str:      Date in YYYY-MM-DD format (defaults to today)

    Returns:
        Report dict with all metrics
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # Today's trades
    today_outcomes = ails_conn.execute(
        "SELECT ticker, strategy, regime, direction, win, pnl, system "
        "FROM live_outcomes WHERE ts LIKE ?",
        (f"{date_str}%",),
    ).fetchall()

    total_trades = len(today_outcomes)
    wins = sum(1 for r in today_outcomes if r["win"])
    win_rate = wins / total_trades if total_trades > 0 else None
    total_pnl = sum(r["pnl"] for r in today_outcomes)

    # Current regime
    regime_info = get_current_regime()
    current_regime = regime_info.get("regime", "UNKNOWN")

    # Compare to historical baseline for this regime
    drift_flags: List[str] = []
    strategy_breakdown: List[Dict[str, Any]] = []

    strategies_seen = set()
    for outcome in today_outcomes:
        strat = outcome["strategy"]
        direction = outcome["direction"]
        strategies_seen.add((strat, direction))

    for strat, direction in strategies_seen:
        # Historical win rate for this strategy/regime
        hist = backtest_conn.execute(
            "SELECT win_rate, sample_count FROM regime_level_rates "
            "WHERE strategy=? AND regime=? AND direction=?",
            (strat, current_regime, direction),
        ).fetchone()

        today_strat = [r for r in today_outcomes
                       if r["strategy"] == strat and r["direction"] == direction]
        today_wins = sum(1 for r in today_strat if r["win"])
        today_wr = today_wins / len(today_strat) if today_strat else None

        entry: Dict[str, Any] = {
            "strategy": strat,
            "direction": direction,
            "today_trades": len(today_strat),
            "today_win_rate": today_wr,
            "historical_win_rate": hist["win_rate"] if hist else None,
        }

        # Drift detection
        if today_wr is not None and hist and len(today_strat) >= 3:
            drift = abs(today_wr - hist["win_rate"])
            if drift >= DRIFT_ALERT_PCT:
                flag = (
                    f"{strat}/{direction}: today {today_wr:.0%} vs "
                    f"historical {hist['win_rate']:.0%} "
                    f"(drift {drift:.0%})"
                )
                drift_flags.append(flag)
                entry["drift_alert"] = flag

        strategy_breakdown.append(entry)

    # Agent calibration summary
    calibration_rows = ails_conn.execute(
        "SELECT agent, AVG(predicted_confidence) as avg_pred, "
        "AVG(actual_win_rate) as avg_actual, SUM(n) as total_n "
        "FROM agent_calibration GROUP BY agent"
    ).fetchall()

    agent_calibration: Dict[str, Any] = {}
    for row in calibration_rows:
        agent_calibration[row["agent"]] = {
            "avg_predicted": round(row["avg_pred"], 3),
            "avg_actual": round(row["avg_actual"], 3),
            "calibration_error": round(abs(row["avg_pred"] - row["avg_actual"]), 3),
            "sample_count": row["total_n"],
        }

    report = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "regime": current_regime,
        "vix": regime_info.get("vix"),
        "summary": {
            "total_trades": total_trades,
            "wins": wins,
            "win_rate": round(win_rate, 3) if win_rate is not None else None,
            "total_pnl": round(total_pnl, 2),
        },
        "strategy_breakdown": strategy_breakdown,
        "agent_calibration": agent_calibration,
        "drift_flags": drift_flags,
        "requires_approval": [],  # Populated by EOW report
    }

    # Store in DB
    now_iso = datetime.now(timezone.utc).isoformat()
    ails_conn.execute(
        "INSERT INTO eod_reports (date, report_json, delivered, created_at) "
        "VALUES (?,?,0,?) ON CONFLICT(date) DO UPDATE SET "
        "report_json=excluded.report_json, created_at=excluded.created_at",
        (date_str, json.dumps(report), now_iso),
    )
    ails_conn.commit()

    return report


def deliver_eod_report(report: Dict[str, Any]) -> None:
    """Format and send EOD report to Ahmed via Telegram."""
    summary = report["summary"]
    date = report["date"]
    regime = report["regime"]
    vix = report.get("vix")
    drift_flags = report.get("drift_flags", [])

    vix_str = f"{vix:.1f}" if vix else "N/A"
    wr_str = f"{summary['win_rate']:.0%}" if summary["win_rate"] is not None else "N/A"

    lines = [
        f"📊 *NEXUS EOD REPORT — {date}*",
        f"\n*Regime:* {regime} | VIX: {vix_str}",
        f"*Trades:* {summary['total_trades']} | *Win Rate:* {wr_str}",
        f"*P&L:* ${summary['total_pnl']:+,.2f}",
    ]

    if drift_flags:
        lines.append(f"\n⚠️ *Parameter Drift Detected:*")
        for flag in drift_flags[:3]:
            lines.append(f"  • {flag}")

    cal = report.get("agent_calibration", {})
    if cal:
        lines.append("\n*Agent Calibration:*")
        for agent, data in cal.items():
            err = data.get("calibration_error", 0)
            emoji = "✅" if err < 0.05 else "⚠️" if err < 0.10 else "🔴"
            lines.append(
                f"  {emoji} {agent}: predicted {data['avg_predicted']:.0%} "
                f"/ actual {data['avg_actual']:.0%}"
            )

    lines.append("\n_All recommendations require Ahmed approval before any parameter changes._")

    _send_telegram("\n".join(lines))
    log.info("EOD report delivered for %s", date)


def generate_eow_report(
    ails_conn: sqlite3.Connection,
    backtest_conn: sqlite3.Connection,
    week_ending: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate end-of-week performance, drift analysis, and agent weight recommendations.
    NOTHING auto-applies — all recommendations go to Ahmed for approval.

    Args:
        ails_conn:     Live AILS DB connection
        backtest_conn: Historical backtest DB connection
        week_ending:   Date string for week end (defaults to today)

    Returns:
        EOW report dict
    """
    if week_ending is None:
        week_ending = datetime.now().strftime("%Y-%m-%d")

    # Cipher Pass 3 P3-7: prior code computed cutoff_ts from week_ending but
    # never used it — query was hardcoded to 'now', '-7 days'. Any retrospective
    # report returned the wrong week's data. Fix: compute exact 7-day window from
    # week_ending so the query respects the requested date.
    try:
        week_end_dt = datetime.strptime(week_ending, "%Y-%m-%d")
    except ValueError:
        week_end_dt = datetime.now()
        week_ending = week_end_dt.strftime("%Y-%m-%d")
    week_start_dt = week_end_dt.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    from datetime import timedelta
    week_start_str = (week_start_dt - timedelta(days=6)).strftime("%Y-%m-%dT00:00:00")
    week_end_str   = week_end_dt.strftime("%Y-%m-%dT23:59:59")

    week_outcomes = ails_conn.execute(
        "SELECT ticker, strategy, regime, direction, win, pnl, system, agent_votes "
        "FROM live_outcomes WHERE ts >= ? AND ts <= ?",
        (week_start_str, week_end_str),
    ).fetchall()

    total_trades = len(week_outcomes)
    wins = sum(1 for r in week_outcomes if r["win"])
    win_rate = wins / total_trades if total_trades > 0 else None
    total_pnl = sum(r["pnl"] for r in week_outcomes)

    # Agent performance this week (from votes)
    agent_performance: Dict[str, Dict[str, int]] = {}
    for outcome in week_outcomes:
        if not outcome["agent_votes"]:
            continue
        try:
            votes = json.loads(outcome["agent_votes"])
        except json.JSONDecodeError:
            continue
        for agent, voted_go in votes.items():
            if agent not in agent_performance:
                agent_performance[agent] = {"correct": 0, "total": 0}
            correct = (bool(voted_go) == bool(outcome["win"]))
            agent_performance[agent]["total"] += 1
            if correct:
                agent_performance[agent]["correct"] += 1

    # Agent weight recommendations (for Ahmed approval — NEVER auto-applied)
    weight_recommendations: Dict[str, Any] = {}
    current_weights = {"Cipher": 45, "Atlas": 30, "Sage": 25}
    for agent, perf in agent_performance.items():
        if perf["total"] < 5:
            continue
        week_accuracy = perf["correct"] / perf["total"]
        current_weight = current_weights.get(agent, 33)
        # Simple recommendation: if >10% above expectation, suggest +5%
        recommendation = None
        if week_accuracy > 0.65:
            recommendation = f"Consider +5% weight (week accuracy: {week_accuracy:.0%})"
        elif week_accuracy < 0.45:
            recommendation = f"Consider -5% weight (week accuracy: {week_accuracy:.0%})"

        weight_recommendations[agent] = {
            "current_weight": current_weight,
            "week_accuracy": round(week_accuracy, 3),
            "recommendation": recommendation,
            "requires_ahmed_approval": True,
        }

    report = {
        "week_ending": week_ending,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_trades": total_trades,
            "wins": wins,
            "win_rate": round(win_rate, 3) if win_rate is not None else None,
            "total_pnl": round(total_pnl, 2),
        },
        "agent_performance": agent_performance,
        "weight_recommendations": weight_recommendations,
        "requires_approval": list(weight_recommendations.keys()),
        "note": "No parameter changes auto-applied. Ahmed approval required for all recommendations.",
    }

    now_iso = datetime.now(timezone.utc).isoformat()
    ails_conn.execute(
        "INSERT INTO eow_reports (week_ending, report_json, delivered, created_at) "
        "VALUES (?,?,0,?) ON CONFLICT(week_ending) DO UPDATE SET "
        "report_json=excluded.report_json, created_at=excluded.created_at",
        (week_ending, json.dumps(report), now_iso),
    )
    ails_conn.commit()

    return report


def deliver_eow_report(report: Dict[str, Any]) -> None:
    """Format and send EOW report to Ahmed."""
    summary = report["summary"]
    week_end = report["week_ending"]
    wr_str = f"{summary['win_rate']:.0%}" if summary["win_rate"] is not None else "N/A"
    recs = report.get("weight_recommendations", {})

    lines = [
        f"📈 *NEXUS EOW REPORT — Week ending {week_end}*",
        f"\n*Trades:* {summary['total_trades']} | *Win Rate:* {wr_str}",
        f"*P&L:* ${summary['total_pnl']:+,.2f}",
    ]

    if recs:
        lines.append("\n*Agent Weight Recommendations (pending your approval):*")
        for agent, data in recs.items():
            rec = data.get("recommendation") or "No change needed"
            lines.append(
                f"  • {agent} ({data['current_weight']}%): {rec}"
            )

    lines.append(
        "\n🔐 *Nothing auto-applies.* Reply to approve or reject recommendations."
    )

    _send_telegram("\n".join(lines))
    log.info("EOW report delivered for week ending %s", week_end)
