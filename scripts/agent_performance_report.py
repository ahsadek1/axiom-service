#!/usr/bin/env python3
"""
agent_performance_report.py — Statistical agent performance framework.

Reads from AILS agent_calibration and live_outcomes to produce:
  - Per-agent win rate with Wilson score 95% confidence intervals
  - Calibration delta (predicted vs actual win rate)
  - Minimum sample size gate (n >= 20 for statistical validity)
  - Automated alert if actual win rate falls below predicted - 10pp with n >= 20
  - Daily summary suitable for EOD report or Cipher blocker sweep

Commercial-grade rationale:
  With only raw win rates, there's no way to know if Sage's 66% win rate
  (n=4) is meaningfully different from Cipher's 72% (n=4). Wilson score
  confidence intervals make the uncertainty explicit and prevent over-tuning
  on noise. The 20-sample gate prevents false alarms in early trading.

Usage:
  python3 agent_performance_report.py [--alert] [--telegram]
  --alert:    Exit 1 if any agent is underperforming vs calibration
  --telegram: Send report to Ahmed DM via Telegram bot
"""
import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

AILS_DB     = "/Users/ahmedsadek/nexus/data/ails.db"
CHRONICLE_DB = "/Users/ahmedsadek/nexus/data/chronicle.db"
TELEGRAM_BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = "8573754783"
MIN_SAMPLE   = 20       # minimum outcomes for statistical significance
ALERT_DELTA  = 0.10     # flag if actual win rate < predicted - 10pp (with n >= MIN_SAMPLE)


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple:
    """
    Wilson score confidence interval for a proportion.
    More accurate than normal approximation especially at extremes.
    Returns (lower, upper) as fractions.
    """
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2*n)) / denom
    spread = z * math.sqrt(p*(1-p)/n + z**2/(4*n**2)) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def load_agent_stats() -> List[Dict]:
    """Load per-agent stats from AILS."""
    conn = sqlite3.connect(AILS_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT agent, strategy, regime,
               predicted_confidence, actual_win_rate, n, last_updated
        FROM agent_calibration
        ORDER BY agent, strategy, regime
    """).fetchall()

    # Also pull raw outcome counts per agent from agent_votes JSON field
    # agent_votes looks like: {"Cipher": true, "Atlas": false, "Sage": true}
    # Pull all distinct agent keys and their vote+outcome
    outcome_rows = conn.execute("""
        SELECT agent_votes, win
        FROM live_outcomes
        WHERE agent_votes IS NOT NULL
    """).fetchall()
    conn.close()

    outcome_map: Dict[str, Dict] = {}
    for r in outcome_rows:
        try:
            votes = json.loads(r["agent_votes"])
        except Exception:
            continue
        win = int(r["win"] or 0)
        for agent, voted in votes.items():
            key = agent.lower()
            if key not in outcome_map:
                outcome_map[key] = {"wins": 0, "n": 0}
            outcome_map[key]["n"] += 1
            if win:
                outcome_map[key]["wins"] += 1

    results = []
    seen_agents = set()
    for r in rows:
        agent = r["agent"]
        seen_agents.add(agent)
        raw = outcome_map.get(agent, {"wins": 0, "n": 0})
        actual_wr = r["actual_win_rate"]
        predicted  = r["predicted_confidence"]
        n_cal      = r["n"]
        # Use raw outcome data for CI if available and larger sample
        n_for_ci   = max(n_cal, raw["n"])
        wins_for_ci = int(actual_wr * n_for_ci)
        ci_lo, ci_hi = wilson_ci(wins_for_ci, n_for_ci)
        calibrated = n_cal >= MIN_SAMPLE
        underperform = calibrated and (actual_wr < predicted - ALERT_DELTA)
        results.append({
            "agent":      agent,
            "strategy":   r["strategy"],
            "regime":     r["regime"],
            "predicted":  round(predicted, 3),
            "actual":     round(actual_wr, 3),
            "n":          n_cal,
            "ci_lo":      round(ci_lo, 3),
            "ci_hi":      round(ci_hi, 3),
            "calibrated": calibrated,
            "underperform": underperform,
            "delta":      round(actual_wr - predicted, 3),
            "last_updated": r["last_updated"],
        })

    # Add agents with outcome data but no calibration rows yet
    for agent, raw in outcome_map.items():
        if agent.lower() not in seen_agents and raw["n"] > 0:
            ci_lo, ci_hi = wilson_ci(raw["wins"], raw["n"])
            results.append({
                "agent":      agent,
                "strategy":   "any",
                "regime":     "any",
                "predicted":  None,
                "actual":     round(raw["wins"] / raw["n"], 3) if raw["n"] else None,
                "n":          raw["n"],
                "ci_lo":      round(ci_lo, 3),
                "ci_hi":      round(ci_hi, 3),
                "calibrated": raw["n"] >= MIN_SAMPLE,
                "underperform": False,
                "delta":      None,
                "last_updated": None,
            })

    return results


def format_report(stats: List[Dict], date: str) -> str:
    """Format the performance report for Telegram/console."""
    lines = [
        f"📊 AGENT PERFORMANCE REPORT — {date}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Group by agent
    agents = {}
    for s in stats:
        a = s["agent"]
        if a not in agents:
            agents[a] = []
        agents[a].append(s)

    any_alert = False
    for agent, entries in sorted(agents.items()):
        lines.append(f"\n🤖 {agent.upper()}")
        for e in entries:
            n = e["n"]
            actual = e["actual"]
            predicted = e["predicted"]
            ci_lo, ci_hi = e["ci_lo"], e["ci_hi"]
            calibrated = e["calibrated"]

            status = "✅" if not e["underperform"] else "🔴"
            if not calibrated:
                status = "⏳"  # not enough data yet

            actual_str = f"{actual*100:.1f}%" if actual is not None else "N/A"
            pred_str   = f"{predicted*100:.1f}%" if predicted is not None else "N/A"
            ci_str     = f"[{ci_lo*100:.0f}%–{ci_hi*100:.0f}%]" if calibrated else "(n<20)"
            delta_str  = f"{e['delta']*100:+.1f}pp" if e["delta"] is not None else ""
            regime = e.get("regime", "?")
            strat  = e.get("strategy", "?")

            lines.append(
                f"  {status} {strat}/{regime}: "
                f"actual={actual_str} {ci_str} | "
                f"predicted={pred_str} {delta_str} | n={n}"
            )
            if e["underperform"]:
                any_alert = True
                lines.append(f"  ⚠️  UNDERPERFORMING: actual {actual_str} < predicted {pred_str} - 10pp threshold")

    if not any_alert:
        lines.append("\n✅ All agents within calibration bounds.")
    else:
        lines.append("\n🔴 ACTION: Review underperforming agents — threshold or signal recalibration needed.")

    lines.append(f"\nGenerated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    """Send report to Ahmed via Telegram."""
    if not TELEGRAM_BOT:
        return False
    try:
        import urllib.request
        payload = json.dumps({"chat_id": TELEGRAM_CHAT, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except Exception as e:
        print(f"[agent_performance] Telegram send failed: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--alert", action="store_true",
                        help="Exit 1 if any agent underperforming")
    parser.add_argument("--telegram", action="store_true",
                        help="Send report to Ahmed via Telegram")
    parser.add_argument("--json", action="store_true",
                        help="Output raw JSON instead of formatted report")
    args = parser.parse_args()

    stats = load_agent_stats()
    date  = datetime.now().strftime("%Y-%m-%d")

    if args.json:
        print(json.dumps(stats, indent=2))
        return 0

    report = format_report(stats, date)
    print(report)

    if args.telegram:
        ok = send_telegram(report)
        if not ok:
            print("[agent_performance] Telegram delivery failed", file=sys.stderr)

    underperformers = [s for s in stats if s.get("underperform")]
    if args.alert and underperformers:
        print(f"\n[ALERT] {len(underperformers)} agent(s) underperforming — exiting 1")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
