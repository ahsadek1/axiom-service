#!/usr/bin/env python3
"""
nexus_status.py — Unified Nexus Observability Dashboard

Single-pane-of-glass status for the entire Nexus system.
Aggregates: all local services, Railway deployments, Alpaca account,
Chronicle open items, agent calibration, and SQS capital state.

Commercial-grade rationale:
  Previously: Cipher manually queried 17 ports + 4 Railway URLs + Alpaca
  + Chronicle every blocker sweep to build a mental picture. This script
  does it in parallel in ~2-3 seconds and returns a structured snapshot.

Usage:
  python3 nexus_status.py              # Full status, console output
  python3 nexus_status.py --telegram   # Send snapshot to Ahmed DM
  python3 nexus_status.py --json       # Raw JSON for programmatic use
  python3 nexus_status.py --brief      # One-line-per-service summary

Exit codes:
  0 — all healthy
  1 — one or more services degraded/down
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

# ── Service registry ─────────────────────────────────────────────────────────
LOCAL_SERVICES = [
    {"name": "Axiom",        "port": 8001, "key": "regime"},
    {"name": "Alpha-Buf",    "port": 8002, "key": "cb_status"},
    {"name": "Prime-Buf",    "port": 8003, "key": "cb_status"},
    {"name": "OMNI",         "port": 8004, "key": "syntheses_today"},
    {"name": "Alpha-Exec",   "port": 8005, "key": "trades_today"},
    {"name": "Prime-Exec",   "port": 8006, "key": "trades_today"},
    {"name": "Oracle",       "port": 8007, "path": "/ping", "key": "status"},
    {"name": "AILS",         "port": 8008, "key": "status"},
    {"name": "Guardian",     "port": 8009, "key": "status"},
    {"name": "Cap-Router",   "port": 8000, "key": "total_deployed"},
    {"name": "Cipher",       "port": 9001, "key": "picks_today"},
    {"name": "Atlas",        "port": 9002, "key": "picks_today"},
    {"name": "Sage",         "port": 9003, "key": "picks_today"},
    {"name": "ATM-Multi",    "port": 9004, "key": "total_trades"},
    {"name": "ATM-0DTE",     "port": 9005, "key": "daily_trades"},
    {"name": "ATG-Swing",    "port": 9006, "key": "total_trades"},
    {"name": "ATG-Intraday", "port": 9007, "key": "scan_count"},
]

RAILWAY_SERVICES = [
    {"name": "Alpha-Worker",  "url": "https://worker-production-2060.up.railway.app/health",      "key": "loop_active"},
    {"name": "Prime-Worker",  "url": "https://nexus-prime-bot-production.up.railway.app/health",   "key": "go_verdicts_today"},
]

ALPACA_URL    = "https://paper-api.alpaca.markets/v2/account"
ALPACA_KEY    = os.environ.get("ALPACA_API_KEY",    "PKPGM3BRNYPGCF5Z56IAUZCZJL")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET", "5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs")
CHRONICLE_DB  = "/Users/ahmedsadek/nexus/data/chronicle.db"
AILS_DB       = "/Users/ahmedsadek/nexus/data/ails.db"
TELEGRAM_BOT  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = "8573754783"
TIMEOUT       = 4  # seconds per HTTP call


def _fetch(url: str, headers: Optional[Dict] = None) -> Tuple[Optional[Dict], Optional[str]]:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, str(e.reason)
    except Exception as e:
        return None, str(e)[:60]


def check_local(svc: Dict) -> Dict:
    port = svc["port"]
    path = svc.get("path", "/health")
    url  = f"http://localhost:{port}{path}"
    data, err = _fetch(url)
    if err or data is None:
        return {"name": svc["name"], "port": port, "up": False, "error": err, "metric": None}
    metric_key = svc.get("key", "status")
    metric = data.get(metric_key, data.get("status", "?"))
    status = data.get("status", "?")
    up = status not in ("error", "down", False) and not isinstance(status, bool) or status is True
    # Treat "ok", "healthy", "trading" as up
    if isinstance(status, str):
        up = status.lower() in ("ok", "healthy", "trading", "live", "running", "active")
    return {"name": svc["name"], "port": port, "up": up, "status": status, "metric": metric, "raw": data}


def check_railway(svc: Dict) -> Dict:
    data, err = _fetch(svc["url"])
    if err or data is None:
        return {"name": svc["name"], "up": False, "error": err, "metric": None}
    metric = data.get(svc.get("key", "status"), "?")
    status = data.get("status", "healthy")
    up = isinstance(status, str) and status.lower() in ("ok", "healthy", "live", "running", "active")
    if not up and data.get("loop_active"):
        up = True
    return {"name": svc["name"], "up": up, "status": status, "metric": metric}


def check_alpaca() -> Dict:
    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    data, err = _fetch(ALPACA_URL, headers)
    if err or data is None:
        return {"up": False, "error": err}
    return {
        "up": data.get("status") == "ACTIVE",
        "equity": float(data.get("equity", 0)),
        "buying_power": float(data.get("buying_power", 0)),
        "daytrade_count": data.get("daytrade_count", 0),
        "pdt": data.get("pattern_day_trader", False),
        "open_positions": None,  # fetched separately if needed
    }


def check_chronicle() -> Dict:
    try:
        conn = sqlite3.connect(CHRONICLE_DB, timeout=3)
        conn.row_factory = sqlite3.Row
        open_items = conn.execute("""
            SELECT COUNT(*) as n FROM intervention_log
            WHERE outcome IN ('unsolved', 'in_progress')
        """).fetchone()["n"]
        today_items = conn.execute("""
            SELECT COUNT(*) as n FROM intervention_log
            WHERE date(created_at) = date('now')
        """).fetchone()["n"]
        conn.close()
        return {"open_p0_p1": open_items, "today_total": today_items, "accessible": True}
    except Exception as e:
        return {"accessible": False, "error": str(e)[:60]}


def check_ails() -> Dict:
    try:
        conn = sqlite3.connect(AILS_DB, timeout=3)
        conn.row_factory = sqlite3.Row
        cal = conn.execute("""
            SELECT agent, actual_win_rate, n FROM agent_calibration
            ORDER BY agent
        """).fetchall()
        conn.close()
        return {
            "accessible": True,
            "calibration": [{"agent": r["agent"], "win_rate": round(r["actual_win_rate"], 3), "n": r["n"]} for r in cal]
        }
    except Exception as e:
        return {"accessible": False, "error": str(e)[:60]}


def run_all() -> Dict:
    start = time.monotonic()
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "local": [],
        "railway": [],
        "alpaca": {},
        "chronicle": {},
        "ails": {},
        "elapsed_ms": 0,
    }

    with ThreadPoolExecutor(max_workers=24) as ex:
        local_futures  = {ex.submit(check_local, svc): svc for svc in LOCAL_SERVICES}
        railway_futures = {ex.submit(check_railway, svc): svc for svc in RAILWAY_SERVICES}
        alpaca_future  = ex.submit(check_alpaca)
        chronicle_future = ex.submit(check_chronicle)
        ails_future    = ex.submit(check_ails)

        for f in as_completed(local_futures):
            results["local"].append(f.result())
        for f in as_completed(railway_futures):
            results["railway"].append(f.result())
        results["alpaca"]    = alpaca_future.result()
        results["chronicle"] = chronicle_future.result()
        results["ails"]      = ails_future.result()

    results["local"].sort(key=lambda x: x.get("port", 0))
    results["elapsed_ms"] = int((time.monotonic() - start) * 1000)
    return results


def format_snapshot(data: Dict, brief: bool = False) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    lines = [f"🔐 NEXUS STATUS — {now}"]
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Local services
    lines.append("\n📡 LOCAL SERVICES")
    down = []
    for svc in data["local"]:
        icon = "✅" if svc.get("up") else "❌"
        name = svc["name"].ljust(12)
        metric = svc.get("metric", "")
        err = svc.get("error", "")
        if svc.get("up"):
            lines.append(f"  {icon} {name} | {svc.get('status','?')} | {metric}")
        else:
            lines.append(f"  {icon} {name} | DOWN | {err}")
            down.append(svc["name"])

    # Railway
    lines.append("\n🚂 RAILWAY")
    for svc in data["railway"]:
        icon = "✅" if svc.get("up") else "❌"
        lines.append(f"  {icon} {svc['name'].ljust(14)} | {svc.get('status','?')} | {svc.get('metric','?')}")

    # Alpaca
    alp = data["alpaca"]
    lines.append("\n💰 ALPACA (PAPER)")
    if alp.get("up"):
        lines.append(f"  ✅ ACTIVE | equity=${alp['equity']:,.0f} | BP=${alp['buying_power']:,.0f} | DT={alp['daytrade_count']} | PDT={alp['pdt']}")
    else:
        lines.append(f"  ❌ {alp.get('error','?')}")

    # Chronicle
    ch = data["chronicle"]
    lines.append("\n📚 CHRONICLE")
    if ch.get("accessible"):
        lines.append(f"  Open P0/P1: {ch['open_p0_p1']} | Today total: {ch['today_total']}")
    else:
        lines.append(f"  ❌ {ch.get('error','?')}")

    # AILS calibration
    ails = data["ails"]
    lines.append("\n🧠 AGENT CALIBRATION")
    if ails.get("accessible") and ails.get("calibration"):
        for c in ails["calibration"]:
            flag = "⏳" if c["n"] < 20 else "✅"
            lines.append(f"  {flag} {c['agent'].ljust(8)} win={c['win_rate']*100:.0f}% n={c['n']}")
    else:
        lines.append("  No calibration data yet")

    # Summary
    total = len(data["local"]) + len(data["railway"])
    up_count = sum(1 for s in data["local"] if s.get("up")) + sum(1 for s in data["railway"] if s.get("up"))
    lines.append(f"\n{'✅ ALL CLEAR' if not down else '🔴 DEGRADED'} — {up_count}/{total} services up | {data['elapsed_ms']}ms")
    if down:
        lines.append(f"Down: {', '.join(down)}")

    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT:
        print("[nexus_status] No TELEGRAM_BOT_TOKEN set", file=sys.stderr)
        return False
    try:
        payload = json.dumps({"chat_id": TELEGRAM_CHAT, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except Exception as e:
        print(f"[nexus_status] Telegram failed: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Nexus unified status dashboard")
    parser.add_argument("--json",     action="store_true", help="Output raw JSON")
    parser.add_argument("--telegram", action="store_true", help="Send to Ahmed DM")
    parser.add_argument("--brief",    action="store_true", help="Brief one-liner output")
    args = parser.parse_args()

    data = run_all()

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        report = format_snapshot(data, brief=args.brief)
        print(report)
        if args.telegram:
            send_telegram(report)

    # Exit 1 if any local service is down
    any_down = any(not s.get("up") for s in data["local"]) or any(not s.get("up") for s in data["railway"])
    return 1 if any_down else 0


if __name__ == "__main__":
    sys.exit(main())
