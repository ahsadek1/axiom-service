"""
learning_loop.py — THESIS Learning Loop
=========================================
THESIS gets smarter every week by tracking what works.

Tracks:
  - Per-legend accuracy (which legend has best predictive power?)
  - Per-regime performance (which regimes produce best results?)
  - Backtest validation (is the 99% holding in live trading?)
  - Parameter adaptation (should endorsement threshold change?)
  - Win rate calibration (recalibrate if live diverges from backtest)

Runs:
  - EOD update: records all closed positions from today
  - Weekly report: comprehensive performance analysis
  - Monthly calibration: adjusts thresholds if needed
"""
from __future__ import annotations
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, date, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("thesis.learning")
_ET = ZoneInfo("America/New_York")

POSITIONS_DB  = os.getenv("THESIS_POSITIONS_DB",
                          "/Users/ahmedsadek/nexus/data/thesis_positions.db")
LEARNING_DB   = os.getenv("THESIS_LEARNING_DB",
                          "/Users/ahmedsadek/nexus/data/thesis_learning.db")
TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT       = os.getenv("THESIS_CHAT_ID", os.getenv("AHMED_CHAT_ID",""))


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


def init_learning_db() -> None:
    conn = sqlite3.connect(LEARNING_DB, timeout=10)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS legend_performance (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            legend          TEXT NOT NULL,
            trade_date      TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            position_id     TEXT NOT NULL,
            win             INTEGER,
            pnl             REAL,
            win_rate_pred   REAL,
            regime          TEXT,
            ts              REAL
        );

        CREATE TABLE IF NOT EXISTS regime_performance (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            regime      TEXT NOT NULL,
            trade_date  TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            win         INTEGER,
            pnl         REAL,
            dte_entry   INTEGER,
            ts          REAL
        );

        CREATE TABLE IF NOT EXISTS weekly_stats (
            week_start      TEXT PRIMARY KEY,
            total_trades    INTEGER,
            winning_trades  INTEGER,
            total_pnl       REAL,
            win_rate        REAL,
            avg_pnl         REAL,
            best_legend     TEXT,
            best_regime     TEXT,
            sharpe_approx   REAL,
            notes           TEXT,
            ts              REAL
        );

        CREATE TABLE IF NOT EXISTS calibration_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            calibration_date    TEXT,
            metric              TEXT,
            old_value           REAL,
            new_value           REAL,
            reason              TEXT,
            ts                  REAL
        );

        CREATE INDEX IF NOT EXISTS idx_legend_perf
            ON legend_performance(legend, trade_date);
        CREATE INDEX IF NOT EXISTS idx_regime_perf
            ON regime_performance(regime, trade_date);
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# EOD position recording
# ---------------------------------------------------------------------------

def record_closed_positions() -> int:
    """
    Record today's closed positions into learning DB.
    Called at EOD.
    """
    init_learning_db()
    today = date.today().isoformat()

    try:
        pos_conn = sqlite3.connect(POSITIONS_DB, timeout=5)
        pos_conn.row_factory = sqlite3.Row
        positions = pos_conn.execute("""
            SELECT * FROM positions
            WHERE status='CLOSED'
            AND DATE(exit_time) = ?
        """, (today,)).fetchall()
        pos_conn.close()
    except Exception as exc:
        log.error("Failed to load closed positions: %s", exc)
        return 0

    recorded = 0
    learn_conn = sqlite3.connect(LEARNING_DB, timeout=10)

    for p in positions:
        p = dict(p)
        win    = 1 if (p.get("exit_pnl", 0) or 0) > 0 else 0
        pnl    = float(p.get("exit_pnl", 0) or 0)
        legends = p.get("legends", "").split(",") if p.get("legends") else []

        # Record per-legend
        for legend in legends:
            if not legend.strip():
                continue
            learn_conn.execute("""
                INSERT OR IGNORE INTO legend_performance
                (legend, trade_date, ticker, position_id, win, pnl,
                 win_rate_pred, ts)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                legend.strip(),
                today,
                p["ticker"],
                p["position_id"],
                win,
                pnl,
                float(p.get("win_rate", 0) or 0),
                time.time(),
            ))

        # Record per-regime (regime not stored on position yet — use NORMAL default)
        learn_conn.execute("""
            INSERT OR IGNORE INTO regime_performance
            (regime, trade_date, ticker, win, pnl, dte_entry, ts)
            VALUES (?,?,?,?,?,?,?)
        """, (
            "NORMAL",  # TODO: store regime at entry in positions table
            today,
            p["ticker"],
            win,
            pnl,
            int(p.get("dte_at_entry", 37) or 37),
            time.time(),
        ))

        recorded += 1

    learn_conn.commit()
    learn_conn.close()
    log.info("Learning loop recorded %d closed positions", recorded)
    return recorded


# ---------------------------------------------------------------------------
# Performance analytics
# ---------------------------------------------------------------------------

def get_legend_accuracy(days: int = 30) -> list[dict]:
    """
    Per-legend win rate and P&L over last N days.
    Reveals which legends have best predictive accuracy.
    """
    init_learning_db()
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    try:
        conn = sqlite3.connect(LEARNING_DB, timeout=5)
        rows = conn.execute("""
            SELECT
                legend,
                COUNT(*) as trades,
                SUM(win) as wins,
                ROUND(AVG(win),3) as win_rate,
                ROUND(SUM(pnl),2) as total_pnl,
                ROUND(AVG(pnl),2) as avg_pnl,
                ROUND(AVG(win_rate_pred),3) as avg_predicted_wr
            FROM legend_performance
            WHERE trade_date >= ?
            GROUP BY legend
            ORDER BY win_rate DESC, total_pnl DESC
        """, (cutoff,)).fetchall()
        conn.close()

        return [
            {
                "legend":         r[0],
                "trades":         r[1],
                "wins":           r[2],
                "win_rate":       r[3],
                "total_pnl":      r[4],
                "avg_pnl":        r[5],
                "predicted_wr":   r[6],
                "calibration":    round((r[3] or 0) - (r[6] or 0), 3),
            }
            for r in rows
        ]
    except Exception as exc:
        log.error("Legend accuracy query failed: %s", exc)
        return []


def get_regime_performance(days: int = 30) -> list[dict]:
    """Per-regime performance over last N days."""
    init_learning_db()
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    try:
        conn = sqlite3.connect(LEARNING_DB, timeout=5)
        rows = conn.execute("""
            SELECT
                regime,
                COUNT(*) as trades,
                SUM(win) as wins,
                ROUND(AVG(win),3) as win_rate,
                ROUND(SUM(pnl),2) as total_pnl,
                ROUND(AVG(pnl),2) as avg_pnl
            FROM regime_performance
            WHERE trade_date >= ?
            GROUP BY regime
            ORDER BY total_pnl DESC
        """, (cutoff,)).fetchall()
        conn.close()

        return [
            {
                "regime":     r[0],
                "trades":     r[1],
                "wins":       r[2],
                "win_rate":   r[3],
                "total_pnl":  r[4],
                "avg_pnl":    r[5],
            }
            for r in rows
        ]
    except Exception as exc:
        log.error("Regime performance query failed: %s", exc)
        return []


def compute_weekly_stats() -> Optional[dict]:
    """Compute weekly performance statistics."""
    init_learning_db()
    today      = date.today()
    week_start = today - timedelta(days=today.weekday())
    cutoff     = week_start.isoformat()

    try:
        pos_conn = sqlite3.connect(POSITIONS_DB, timeout=5)
        pos_conn.row_factory = sqlite3.Row
        positions = pos_conn.execute("""
            SELECT exit_pnl, ticker FROM positions
            WHERE status='CLOSED' AND DATE(exit_time) >= ?
        """, (cutoff,)).fetchall()
        pos_conn.close()

        if not positions:
            return None

        pnls        = [float(p["exit_pnl"] or 0) for p in positions]
        wins        = sum(1 for p in pnls if p > 0)
        total_pnl   = sum(pnls)
        avg_pnl     = total_pnl / len(pnls) if pnls else 0
        win_rate    = wins / len(pnls) if pnls else 0

        # Approximate Sharpe (annualized, assuming 252 trading days)
        import statistics
        std = statistics.stdev(pnls) if len(pnls) > 1 else 1
        sharpe = (avg_pnl / std * (252 ** 0.5)) if std > 0 else 0

        # Best legend this week
        legend_acc = get_legend_accuracy(days=7)
        best_legend = legend_acc[0]["legend"] if legend_acc else "N/A"

        # Best regime this week
        regime_perf = get_regime_performance(days=7)
        best_regime = regime_perf[0]["regime"] if regime_perf else "N/A"

        stats = {
            "week_start":    cutoff,
            "total_trades":  len(pnls),
            "winning_trades": wins,
            "total_pnl":     round(total_pnl, 2),
            "win_rate":      round(win_rate, 3),
            "avg_pnl":       round(avg_pnl, 2),
            "best_legend":   best_legend,
            "best_regime":   best_regime,
            "sharpe_approx": round(sharpe, 2),
        }

        # Save to learning DB
        conn = sqlite3.connect(LEARNING_DB, timeout=10)
        conn.execute("""
            INSERT OR REPLACE INTO weekly_stats
            (week_start, total_trades, winning_trades, total_pnl,
             win_rate, avg_pnl, best_legend, best_regime, sharpe_approx, ts)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            cutoff, stats["total_trades"], stats["winning_trades"],
            stats["total_pnl"], stats["win_rate"], stats["avg_pnl"],
            best_legend, best_regime, stats["sharpe_approx"],
            time.time(),
        ))
        conn.commit()
        conn.close()

        return stats

    except Exception as exc:
        log.error("Weekly stats failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Parameter calibration
# ---------------------------------------------------------------------------

def check_calibration_needed() -> list[dict]:
    """
    Check if any parameters need recalibration based on live performance.
    Returns list of recommended adjustments.
    """
    recommendations = []
    legend_acc = get_legend_accuracy(days=30)

    for legend_data in legend_acc:
        if legend_data["trades"] < 5:
            continue  # not enough data

        live_wr    = legend_data["win_rate"] or 0
        pred_wr    = legend_data["predicted_wr"] or 0
        calibration = live_wr - pred_wr

        # Live win rate significantly below prediction
        if calibration < -0.10 and live_wr < 0.70:
            recommendations.append({
                "type":    "LEGEND_DOWNGRADE",
                "legend":  legend_data["legend"],
                "current": pred_wr,
                "actual":  live_wr,
                "action":  f"Reduce {legend_data['legend']} size multiplier by 20%",
                "urgency": "HIGH" if calibration < -0.20 else "MEDIUM",
            })

        # Live win rate significantly above prediction
        elif calibration > 0.10 and live_wr > 0.90:
            recommendations.append({
                "type":    "LEGEND_UPGRADE",
                "legend":  legend_data["legend"],
                "current": pred_wr,
                "actual":  live_wr,
                "action":  f"Increase {legend_data['legend']} size multiplier by 10%",
                "urgency": "LOW",
            })

    return recommendations


# ---------------------------------------------------------------------------
# EOD learning report
# ---------------------------------------------------------------------------

def run_eod_learning() -> str:
    """
    Run end-of-day learning cycle.
    Records positions, computes stats, checks calibration.
    Returns formatted report string.
    """
    recorded   = record_closed_positions()
    stats      = compute_weekly_stats()
    calibration = check_calibration_needed()
    legend_acc = get_legend_accuracy(days=7)

    lines = ["<b>THESIS LEARNING — EOD UPDATE</b>"]
    lines.append(f"Positions recorded: {recorded}")

    if stats:
        lines += [
            f"",
            f"<b>Week Performance</b>",
            f"Trades: {stats['total_trades']} | Win rate: {stats['win_rate']:.0%}",
            f"Total P&L: ${stats['total_pnl']:+,.0f}",
            f"Avg per trade: ${stats['avg_pnl']:+,.0f}",
            f"Sharpe (approx): {stats['sharpe_approx']:.2f}",
            f"Best legend: {stats['best_legend']}",
            f"Best regime: {stats['best_regime']}",
        ]

    if legend_acc:
        lines.append("")
        lines.append("<b>Legend Accuracy (7d)</b>")
        for la in legend_acc[:3]:
            lines.append(
                f"{la['legend']}: {la['win_rate']:.0%} live "
                f"vs {la['predicted_wr']:.0%} predicted "
                f"({la['trades']} trades)"
            )

    if calibration:
        lines.append("")
        lines.append("<b>Calibration Needed</b>")
        for c in calibration:
            lines.append(f"{c['urgency']}: {c['action']}")

    report = "\n".join(lines)
    _notify(report)
    log.info("EOD learning complete: %d positions, %d calibrations needed",
             recorded, len(calibration))
    return report
