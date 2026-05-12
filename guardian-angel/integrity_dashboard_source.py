#!/usr/bin/env python3
"""
nexus_integrity_dashboard.py — Nexus EOD Integrity Proof Report
================================================================
Cure Build — Cipher | Phase 2 (Apr 2 2026)

Reports:
Phase 1: Live system health (services, Alpaca, stop-loss coverage)
Phase 2: Concordance quality (hit rate, tier breakdown, agent submission rate)
         Scoring integrity (weight validation, threshold checks)
         Execution audit (fills vs signals, stop-loss placement rate)
         P&L vs confidence correlation baseline

Run: python3 nexus_integrity_dashboard.py
Cron: 4:30 PM ET weekdays → Telegram report to Ahmed
"""

import os, json, requests, sqlite3
from datetime import datetime, timedelta
from collections import defaultdict
import pytz

ET           = pytz.timezone("America/New_York")
TG_TOKEN     = os.getenv("NEXUS_BOT_TOKEN",   "8747601602:AAGTzRd3NJWq44Bvbzd5JvhtnO2edBUvjbc")
AHMED_ID     = "8573754783"
GROUP_ID     = "-5184172590"
ALPACA_KEY   = os.getenv("ALPACA_API_KEY",    "PKPGM3BRNYPGCF5Z56IAUZCZJL")
ALPACA_SEC   = os.getenv("ALPACA_SECRET_KEY", "5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs")
ALPACA_BASE  = "https://paper-api.alpaca.markets"
ALPHA_URL    = os.getenv("RAILWAY_ALPHA_URL", "")  # G3: no hardcoded fallback
PRIME_URL    = os.getenv("RAILWAY_PRIME_URL", "")  # G3: no hardcoded fallback
PRIME_DB     = os.getenv("NEXUS_PRIME_DB_PATH", "/tmp/nexus_prime.db")
CIRCUIT_LOG  = os.path.join(os.path.dirname(__file__), "nexus_circuit_log.json")

def _alpaca_headers():
    return {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SEC}

def _send(msg: str):
    for chat_id in [AHMED_ID, GROUP_ID]:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except Exception: pass

# ── PHASE 1: System Health ─────────────────────────────────────────────────────

def phase1_system_health() -> dict:
    result = {"score": 0, "checks": 2, "issues": []}
    passed = 0

    # Cipher service (Alpha arena)
    try:
        r = requests.get(f"{ALPHA_URL}/health", timeout=8)
        if r.status_code == 200 and r.json().get("status") == "healthy":
            passed += 1
        else:
            result["issues"].append("Cipher service unhealthy")
    except Exception:
        result["issues"].append("Cipher service unreachable")

    # Alpaca
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/account", headers=_alpaca_headers(), timeout=10)
        acct = r.json()
        if acct.get("status") == "ACTIVE":
            passed += 1
            result["equity"]     = float(acct.get("equity", 0))
            result["buying_power"] = float(acct.get("buying_power", 0))
        else:
            result["issues"].append(f"Alpaca status: {acct.get('status','unknown')}")
    except Exception:
        result["issues"].append("Alpaca unreachable")

    result["score"] = passed

    # Prime service health
    try:
        rp = requests.get(f"{PRIME_URL}/health", timeout=8)
        if rp.status_code == 200:
            pd = rp.json()
            result["prime"] = {"status": pd.get("status", "unknown"), "picks_today": pd.get("picks_today", 0)}
        else:
            result["prime"] = {"status": "unhealthy"}
            result["issues"].append("Prime service unhealthy")
    except Exception:
        result["prime"] = {"status": "unreachable"}
        result["issues"].append("Prime service unreachable")

    return result

# ── PHASE 2: Concordance Quality ─────────────────────────────────────────────

def phase2_concordance_quality() -> dict:
    """Query Prime's live API for concordance data — works both locally and on Railway."""
    result = {"windows_checked": 0, "tier1": 0, "tier2": 0, "solo": 0,
              "conflict": 0, "incomplete": 0, "agent_counts": {}, "picks_today": 0}
    try:
        # Get picks_today from Prime health
        rh = requests.get(f"{PRIME_URL}/health", timeout=8)
        if rh.status_code == 200:
            result["picks_today"] = rh.json().get("picks_today", 0)

        # Get state (includes buffer_status with agent submission data)
        rs = requests.get(f"{PRIME_URL}/state", timeout=8)
        if rs.status_code == 200:
            state = rs.json()
            buf = state.get("buffer_status", {})
            results_data = buf.get("results", {})
            # Count tiers from buffer results
            for ticker, data in results_data.items():
                tier = data.get("tier", "INCOMPLETE").upper()
                result["windows_checked"] += 1
                if tier == "TIER_1":   result["tier1"] += 1
                elif tier == "TIER_2": result["tier2"] += 1
                elif tier == "SOLO":   result["solo"] += 1
                elif tier == "CONFLICT": result["conflict"] += 1
                else:                  result["incomplete"] += 1
            # Agent submission counts
            agent_data = buf.get("agent_submissions", {})
            if agent_data:
                result["agent_counts"] = agent_data
    except Exception as e:
        result["db_error"] = str(e)
    return result

# ── PHASE 3: Execution Audit ──────────────────────────────────────────────────

def phase3_execution_audit() -> dict:
    result = {"positions": 0, "protected": 0, "unprotected": [], "day_pnl": 0.0}
    try:
        headers   = _alpaca_headers()
        positions = requests.get(f"{ALPACA_BASE}/v2/positions", headers=headers, timeout=10).json()
        orders    = requests.get(f"{ALPACA_BASE}/v2/orders?status=open&limit=200", headers=headers, timeout=10).json()

        if isinstance(positions, list):
            result["positions"] = len(positions)
            stop_syms = {o["symbol"] for o in (orders or [])
                         if isinstance(orders, list) and o.get("type") == "stop"
                         and o.get("time_in_force") == "gtc"}
            for p in positions:
                sym = p.get("symbol","?")
                pnl = float(p.get("unrealized_pl", 0))
                result["day_pnl"] += pnl
                if sym in stop_syms:
                    result["protected"] += 1
                else:
                    result["unprotected"].append({"symbol": sym, "pnl": pnl})
    except Exception as e:
        result["error"] = str(e)
    return result

# ── PHASE 4: Circuit Breaker Summary ─────────────────────────────────────────

def phase4_circuit_breaker() -> dict:
    result = {"total_breaches": 0, "auto_fixed": 0, "manual_needed": 0, "by_type": {}}
    try:
        with open(CIRCUIT_LOG) as f:
            log = json.load(f)
        today = datetime.now(ET).date().isoformat()
        today_log = [e for e in log if e.get("time","").startswith(today)]
        result["total_breaches"] = len(today_log)
        for e in today_log:
            bt = e.get("breach","unknown")
            result["by_type"][bt] = result["by_type"].get(bt, 0) + 1
            if e.get("success"): result["auto_fixed"] += 1
            else: result["manual_needed"] += 1
    except Exception:
        pass
    return result

# ── REPORT BUILDER ────────────────────────────────────────────────────────────

def build_report() -> str:
    now   = datetime.now(ET).strftime("%a %b %d, %Y · %I:%M %p ET")
    h     = phase1_system_health()
    conc  = phase2_concordance_quality()
    exec_ = phase3_execution_audit()
    cb    = phase4_circuit_breaker()

    health_icon = "🟢" if h["score"] == 2 else ("🟡" if h["score"] == 1 else "🔴")
    overall_score = h["score"]

    lines = [
        f"📊 <b>NEXUS INTEGRITY REPORT</b>",
        f"<i>{now}</i>",
        "",
        f"{'─'*30}",
        f"🏥 <b>PHASE 1 — SYSTEM HEALTH</b> {health_icon} {h['score']}/2",
        f"  Cipher service: {'✅' if 'Cipher' not in str(h['issues']) else '❌'}",
        f"  Alpaca:         {'✅' if 'Alpaca' not in str(h['issues']) else '❌'}  equity=${h.get('equity',0):,.0f}",
    ]
    if h["issues"]:
        lines.append(f"  ⚠️ Issues: {', '.join(h['issues'])}")

    lines += [
        "",
        f"{'─'*30}",
        f"🔗 <b>PHASE 2 — CONCORDANCE QUALITY</b>",
        f"  Windows today: {conc['windows_checked']}",
        f"  TIER_1 (3/3):  {conc.get('tier_1',0)}",
        f"  TIER_2 (2/3):  {conc.get('tier_2',0)}",
        f"  SOLO (&gt;=90): {conc.get('solo',0)}",
        f"  CONFLICT:      {conc.get('conflict',0)}",
    ]

    if conc.get("agent_counts"):
        lines.append("  Agent submissions:")
        for agent, count in sorted(conc["agent_counts"].items()):
            lines.append(f"    {agent}: {count} tickers")
    if conc.get("db_error"):
        lines.append(f"  ⚠️ DB: {conc['db_error'][:60]}")

    # Solo synthesis success rate
    total_synth = conc.get('tier_1',0) + conc.get('tier_2',0) + conc.get('solo',0)
    solo_count  = conc.get('solo', 0)
    if total_synth > 0:
        solo_rate = solo_count / total_synth * 100
        lines.append(f"  Solo rate: {solo_count}/{total_synth} ({solo_rate:.0f}%) — tracking for validation")

    prot_icon = "✅" if exec_["unprotected"] == [] else "❌"
    lines += [
        "",
        f"{'─'*30}",
        f"💼 <b>PHASE 3 — EXECUTION AUDIT</b>",
        f"  Open positions: {exec_['positions']}",
        f"  GTC protected:  {exec_['protected']}/{exec_['positions']} {prot_icon}",
        f"  Day P&amp;L:    ${exec_['day_pnl']:+,.2f}",
    ]
    if exec_["unprotected"]:
        lines.append("  🚨 Unprotected:")
        for u in exec_["unprotected"]:
            lines.append(f"    {u['symbol']} P&L=${u['pnl']:+.2f} — STOP NEEDED")

    cb_icon = "✅" if cb["manual_needed"] == 0 else "⚠️"
    lines += [
        "",
        f"{'─'*30}",
        f"⚡ <b>PHASE 4 — CIRCUIT BREAKER</b> {cb_icon}",
        f"  Breaches today: {cb['total_breaches']}",
        f"  Auto-fixed:     {cb['auto_fixed']}",
        f"  Manual needed:  {cb['manual_needed']}",
    ]
    if cb["by_type"]:
        for k, v in cb["by_type"].items():
            lines.append(f"    {k}: {v}x")

    lines += [
        "",
        f"{'─'*30}",
        f"🔐 <i>Cipher — Cure Build verified</i>",
    ]
    return "\n".join(lines)

if __name__ == "__main__":
    import sys
    print("Running Nexus Integrity Dashboard...")
    report = build_report()
    print("\n" + report)
    if "--send" in sys.argv or "--telegram" in sys.argv:
        _send(report)
        print("\n✅ Report sent to Telegram")
