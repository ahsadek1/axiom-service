#!/usr/bin/env python3
"""
OMNI Trading Day Integrity Check — runs every 10 minutes during market hours
Checks ALL systems, unblocks any issues, notifies Ahmed only on critical finds.

Standing mandate: Ahmed Sadek, Apr 9 2026
"""

import requests, json, datetime, os, sys, time

# ── Config ────────────────────────────────────────────────────────────────────
ALPACA_KEY    = "PKPGM3BRNYPGCF5Z56IAUZCZJL"
ALPACA_SECRET = "5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs"
ALPACA_URL    = "https://paper-api.alpaca.markets"
ALPACA_H      = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
NEXUS_SECRET  = "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2"
TG_BOT        = "8747601602:AAGTzRd3NJWq44Bvbzd5JvhtnO2edBUvjbc"
TG_HEALTH     = "-5184172590"
TG_AHMED      = "8573754783"
ALPHA_URL     = "https://worker-production-2060.up.railway.app"  # Railway alpha-execution v3.0.0
AXIOM_URL     = "https://axiom-production-334c.up.railway.app"  # Railway axiom v4.0.0
PRIME_URL     = "https://nexus-prime-bot-production.up.railway.app"  # Railway prime-scheduler v2.1.0
PRIME_EXEC_URL = "http://localhost:8006"  # Local prime-execution v3.0.0 — reconciler lives here
RAILWAY_TOKEN = "95959846-6306-47e7-a502-b7461514ffff"

ISSUES   = []
WARNINGS = []
FIXED    = []

def tg(chat_id, msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_BOT}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}, timeout=8)
    except Exception:
        pass

def flag(msg, critical=False):
    if critical:
        ISSUES.append(msg)
    else:
        WARNINGS.append(msg)

def fixed(msg):
    FIXED.append(msg)
    print(f"[FIX] {msg}")

def check(label, url, timeout=8):
    try:
        r = requests.get(url, timeout=timeout)
        return r.status_code == 200, r
    except Exception as e:
        return False, None

# ── 1. Alpaca account ─────────────────────────────────────────────────────────
def check_alpaca():
    try:
        r = requests.get(f"{ALPACA_URL}/v2/account", headers=ALPACA_H, timeout=8)
        d = r.json()
        if d.get("trading_blocked"):
            flag("🔴 ALPACA TRADING BLOCKED", critical=True)
        if d.get("account_blocked"):
            flag("🔴 ALPACA ACCOUNT BLOCKED", critical=True)
        status = d.get("status","?")
        if status != "ACTIVE":
            flag(f"🔴 Alpaca status={status} (not ACTIVE)", critical=True)
        equity = float(d.get("equity", 0))
        print(f"  Alpaca: {status} | Equity=${equity:,.0f}")
        return True
    except Exception as e:
        flag(f"🔴 Alpaca unreachable: {e}", critical=True)
        return False

# ── 2. Alpha service ──────────────────────────────────────────────────────────
def check_alpha():
    try:
        r = requests.get(f"{ALPHA_URL}/health", timeout=8)
        d = r.json()
        paused    = d.get("execution_paused", False)
        loop      = d.get("omni_scanner", {}).get("loop_active", False)
        go_count  = d.get("omni_scanner", {}).get("go_count", 0)
        pending   = d.get("pending", 0)
        picks     = d.get("picks_today", 0)
        omni_picks= d.get("omni_scanner", {}).get("picks_today", [])
        errors    = [p for p in omni_picks if "error" in str(p.get("status","")).lower()]

        print(f"  Alpha: loop={loop} paused={paused} go={go_count} pending={pending} picks={picks} errors={len(errors)}")

        if paused:
            flag("🔴 Alpha execution_paused=True — BLOCKING TRADES", critical=True)
        if not loop:
            flag("🔴 Alpha OMNI scanner loop NOT active", critical=True)
        if pending > 200:
            flag(f"⚠️ Alpha concordance queue very large: {pending}", critical=False)

        # Check for recurring error pattern (same ticker erroring 3+ times)
        ticker_errors = {}
        for p in errors:
            t = p.get("ticker","?")
            ticker_errors[t] = ticker_errors.get(t, 0) + 1
        for t, count in ticker_errors.items():
            if count >= 3:
                flag(f"🔴 {t} execution erroring {count}x — needs immediate fix", critical=True)

        # Last cycle staleness (>15 min during market hours = problem)
        last_cycle = d.get("omni_scanner", {}).get("last_cycle_time")
        if last_cycle:
            try:
                lc = datetime.datetime.fromisoformat(last_cycle.replace("Z",""))
                age_min = (datetime.datetime.utcnow() - lc).total_seconds() / 60
                if age_min > 15:
                    flag(f"⚠️ Alpha last cycle was {age_min:.0f}min ago — scanner may be frozen", critical=False)
            except Exception:
                pass
        return True
    except Exception as e:
        flag(f"🔴 Alpha unreachable: {e}", critical=True)
        return False

# ── 3. Axiom ──────────────────────────────────────────────────────────────────
def check_axiom():
    try:
        r = requests.get(f"{AXIOM_URL}/health", timeout=8)
        d = r.json()
        gate = d.get("position_gate","?")
        positions = d.get("positions","?")
        print(f"  Axiom: healthy | gate={gate} | positions={positions}")
        if gate == "ERROR":
            flag("🔴 Axiom position_gate=ERROR", critical=True)
        return True
    except Exception as e:
        flag(f"🔴 Axiom unreachable: {e}", critical=True)
        return False

# ── 4. Prime service — health + scan activity ────────────────────────────────
def check_prime():
    try:
        r = requests.get(f"{PRIME_URL}/health", timeout=8)
        d = r.json()
        scans      = d.get("scans", 0)
        picks      = d.get("picks_today", 0)
        last_scan  = d.get("last_scan")
        print(f"  Prime: healthy | scans={scans} | picks={picks} | last_scan={last_scan}")

        # ── SCAN STALENESS CHECK ──────────────────────────────────────────────
        # If market is open and Prime has run 0 scans past 10:00 AM ET → trigger immediately
        now_et   = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
        mkt_open = now_et.replace(hour=10, minute=0, second=0, microsecond=0)

        if now_et >= mkt_open and scans == 0:
            flag(f"🔴 Prime: 0 scans during market hours — TRIGGERING NOW", critical=True)
            # Auto-trigger immediately — do not wait
            try:
                tr = requests.post(f"{PRIME_URL}/trigger/scan",
                                   headers={"Content-Type": "application/json"}, timeout=10)
                if tr.status_code == 200:
                    fixed("Prime scan triggered via /trigger/scan — was stale 0 scans")
                else:
                    flag(f"🔴 Prime trigger failed: HTTP {tr.status_code}", critical=True)
            except Exception as te:
                flag(f"🔴 Prime trigger error: {te}", critical=True)

        # ── LAST SCAN STALENESS (if it has run before, check age) ────────────
        elif last_scan:
            try:
                ls = datetime.datetime.fromisoformat(last_scan.replace("Z","+00:00"))
                age_min = (datetime.datetime.now(datetime.timezone.utc) - ls).total_seconds() / 60
                # After 1:30 PM ET, expect a midday scan — flag if >90 min stale
                expected_gap = 90
                if age_min > expected_gap:
                    flag(f"⚠️ Prime last scan was {age_min:.0f}min ago — may be stale", critical=False)
                    # Auto-trigger if > 2 hours stale during market hours
                    if age_min > 120:
                        try:
                            tr = requests.post(f"{PRIME_URL}/trigger/scan",
                                               headers={"Content-Type": "application/json"}, timeout=10)
                            if tr.status_code == 200:
                                fixed(f"Prime scan triggered — was {age_min:.0f}min stale")
                        except Exception:
                            pass
            except Exception:
                pass

        return True
    except Exception as e:
        flag(f"🔴 Prime unreachable: {e}", critical=True)
        return False

# ── 5. Open positions P&L + stop check ───────────────────────────────────────
def check_positions():
    try:
        r = requests.get(f"{ALPACA_URL}/v2/positions", headers=ALPACA_H, timeout=8)
        positions = r.json()
        if not isinstance(positions, list):
            flag("⚠️ Could not fetch positions", critical=False)
            return

        for pos in positions:
            sym   = pos.get("symbol","?")
            pl    = float(pos.get("unrealized_pl", 0))
            plpct = float(pos.get("unrealized_plpc", 0)) * 100
            side  = pos.get("side","?")
            print(f"  Position: {sym} | P&L=${pl:.2f} ({plpct:+.1f}%) | {side}")

            # Hard stop check: if loss > $500 on a single position, alert
            if pl < -500:
                flag(f"🔴 STOP ALERT: {sym} P&L=${pl:.2f} — approaching max loss threshold", critical=True)
            elif pl < -300:
                flag(f"⚠️ {sym} P&L=${pl:.2f} — monitor closely", critical=False)

    except Exception as e:
        flag(f"⚠️ Position check failed: {e}", critical=False)

# ── 6. Reconciler status check — Axiom Apr 29 fix ───────────────────────────
def check_reconciler():
    """
    Check prime execution reconciler status.
    Reconciler runs locally (localhost:8006) — checks position alignment
    between DB and Alpaca every 30 min.
    """
    try:
        r = requests.get(f"{PRIME_EXEC_URL}/health", timeout=8)
        d = r.json()
        paused    = d.get("execution_paused", False)
        reconcile = d.get("last_reconcile_at")
        alpaca    = d.get("alpaca_reachable", False)
        
        print(f"  Reconciler: paused={paused} last_reconcile={reconcile} alpaca={alpaca}")
        
        if paused:
            flag("🔴 Reconciler: execution_paused=True — position mismatch active", critical=True)
        if reconcile is None:
            flag("⚠️ Reconciler: never run this session — may need manual trigger", critical=False)
        elif alpaca is False:
            flag("🔴 Reconciler: Alpaca unreachable — pausing execution", critical=True)
        else:
            # Check staleness: should run every 30 min during market hours
            try:
                rt = datetime.datetime.fromisoformat(reconcile.replace("Z","+00:00") if reconcile else "")
                age_min = (datetime.datetime.now(datetime.timezone.utc) - rt).total_seconds() / 60
                if age_min > 45:
                    flag(f"⚠️ Reconciler: last run {age_min:.0f}min ago — may be stale (>45)", critical=False)
            except Exception:
                pass
                
        return True
    except Exception as e:
        flag(f"⚠️ Reconciler unreachable (local-only): {e}", critical=False)
        return False


# ── 7. Canceled order pattern detection — RECENT WINDOW ONLY ─────────────────
# Only flag cancellations in the last 20 minutes. Historical/already-fixed issues
# must not re-alert every cycle. Same issue should never fire twice in a row.
def check_canceled_orders():
    try:
        # Look back only 20 minutes — avoids re-alerting on fixed historical bugs
        window_start = (datetime.datetime.utcnow() - datetime.timedelta(minutes=20)).isoformat() + "Z"
        r = requests.get(
            f"{ALPACA_URL}/v2/orders",
            headers=ALPACA_H,
            params={"status": "canceled", "limit": 20, "after": window_start},
            timeout=8,
        )
        canceled = r.json() if r.status_code == 200 else []
        if not isinstance(canceled, list):
            return

        by_sym = {}
        for o in canceled:
            sym = o.get("symbol","?")
            by_sym[sym] = by_sym.get(sym, 0) + 1

        for sym, count in by_sym.items():
            if count >= 3:
                flag(f"🔴 {sym}: {count} canceled orders in last 20min — active execution failure", critical=True)
            elif count >= 2:
                flag(f"⚠️ {sym}: {count} canceled orders in last 20min — monitor", critical=False)

        if by_sym:
            print(f"  Recent canceled (20min): {dict(list(by_sym.items())[:3])}")
        else:
            print(f"  Recent canceled (20min): none ✅")
    except Exception as e:
        pass

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now_et = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
    ts = now_et.strftime("%H:%M ET")
    print(f"\n🔷 OMNI Integrity Check — {ts}")

    check_alpaca()
    check_alpha()
    check_axiom()
    check_prime()
    check_reconciler()
    check_positions()
    check_canceled_orders()

    # ── Report ────────────────────────────────────────────────────────────────
    if not ISSUES and not WARNINGS:
        print(f"  ✅ All systems clear at {ts}")
        return 0

    # Build alert message
    lines = [f"🔷 <b>OMNI Integrity Check — {ts}</b>"]
    if ISSUES:
        lines.append(f"\n🔴 <b>{len(ISSUES)} CRITICAL ISSUE(S):</b>")
        for i in ISSUES:
            lines.append(f"  • {i}")
    if WARNINGS:
        lines.append(f"\n⚠️ <b>{len(WARNINGS)} WARNING(S):</b>")
        for w in WARNINGS:
            lines.append(f"  • {w}")
    if FIXED:
        lines.append(f"\n✅ <b>Auto-fixed:</b>")
        for f in FIXED:
            lines.append(f"  • {f}")

    msg = "\n".join(lines)

    # Send to health group always; DM Ahmed if critical
    tg(TG_HEALTH, msg)
    if ISSUES:
        tg(TG_AHMED, msg)
        print(f"  🔴 {len(ISSUES)} critical issues — alerted Ahmed + Health group")
    else:
        print(f"  ⚠️ {len(WARNINGS)} warnings — alerted Health group")

    return 1 if ISSUES else 0

if __name__ == "__main__":
    sys.exit(main())
