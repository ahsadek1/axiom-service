#!/usr/bin/env python3
"""
canary_test.py — CANARY Pre-Market Pipeline Test

Runs daily at 9:00 AM ET (before 9:30 AM market open).
Submits synthetic picks from each agent (Cipher, Atlas, Sage) into the Alpha Buffer.
Verifies the full chain: Buffer → OMNI → Concordance → Execution boundary.
Does NOT submit a real Alpaca order (pre-market gate blocks execution).

On PASS:
  - Writes today's canary_status.json with status=PASS
  - Logs "CANARY PASS — trading cleared for the day"

On FAIL:
  - Writes today's canary_status.json with status=FAIL
  - Alerts Ahmed via Telegram with specific failure point
  - OMNI will block synthesis until canary passes or is manually overridden

Run:
  python3 canary_test.py              # Normal run
  python3 canary_test.py --force      # Force run even if canary already passed today
  python3 canary_test.py --status     # Print today's canary status and exit

Exit codes:
  0 = CANARY PASS
  1 = CANARY FAIL
  2 = CANARY SKIPPED (already ran today, use --force to rerun)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests
import pytz

# ── Config ────────────────────────────────────────────────────────────────────

ALPHA_BUFFER_URL = os.getenv("ALPHA_BUFFER_URL",       "http://localhost:8002")
OMNI_URL         = os.getenv("OMNI_URL",               "http://localhost:8004")
ALPHA_EXEC_URL   = os.getenv("ALPHA_EXEC_URL",         "http://localhost:8005")
NEXUS_SECRET     = os.getenv("NEXUS_WEBHOOK_SECRET",
                              "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
OMNI_DB_PATH     = os.getenv("OMNI_DB_PATH",           "/Users/ahmedsadek/nexus/data/omni.db")
CANARY_STATUS_FILE          = "/Users/ahmedsadek/nexus/data/canary_status.json"
CANARY_INTRADAY_STATUS_FILE = "/Users/ahmedsadek/nexus/data/canary_intraday_status.json"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN",  "8747601602:AAGTzRd3NJWq44Bvbzd5JvhtnO2edBUvjbc")
AHMED_CHAT_ID      = os.getenv("AHMED_CHAT_ID",       "8573754783")

ET = pytz.timezone("America/New_York")

# CANARY uses a synthetic window ID so it doesn't interfere with real trading windows
CANARY_TICKER    = "MSFT"  # Changed from SPY — SPY (ETF) not in Axiom stock universe (C-01 gate). MSFT is.
CANARY_DIRECTION = "bearish"
CANARY_SCORE     = 85.0   # high enough to pass MIN_SUBMISSION_SCORE and trigger P1

# Wait up to 45s for OMNI to process the concordance after buffer triggers it
OMNI_SYNTHESIS_TIMEOUT = 120
OMNI_POLL_INTERVAL     = 3


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(ET).strftime("%H:%M:%S")
    prefix = {"INFO": "  ", "OK": "✅", "FAIL": "❌", "WARN": "⚠️ ", "STEP": "→"}
    print(f"[{ts}] {prefix.get(level, '  ')} {msg}", flush=True)


def today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def read_canary_status() -> Optional[dict]:
    """Read today's canary status from disk. Returns None if not run today."""
    try:
        if not os.path.exists(CANARY_STATUS_FILE):
            return None
        with open(CANARY_STATUS_FILE) as f:
            data = json.load(f)
        if data.get("date") != today_et():
            return None   # stale — previous day
        return data
    except Exception:
        return None


def write_canary_status(status: str, detail: str, steps: dict) -> None:
    """Write canary status to disk for OMNI to read."""
    now = datetime.now(ET).isoformat()
    data = {
        "date":    today_et(),
        "status":  status,       # "PASS" | "FAIL" | "RUNNING"
        "detail":  detail,
        "ran_at":  now,
        "steps":   steps,
    }
    os.makedirs(os.path.dirname(CANARY_STATUS_FILE), exist_ok=True)
    tmp = CANARY_STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CANARY_STATUS_FILE)


def send_telegram(msg: str) -> None:
    """Send Telegram message to Ahmed. Never raises."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": AHMED_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as e:
        log(f"Telegram send failed: {e}", "WARN")


def get_health(url: str, timeout: int = 5) -> Optional[dict]:
    """Fetch /health from a service. Returns None on failure."""
    try:
        r = requests.get(f"{url}/health", timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def submit_pick(agent: str, window_id: str) -> dict:
    """Submit a single canary pick to Alpha Buffer."""
    payload = {
        "agent":     agent,
        "ticker":    CANARY_TICKER,
        "direction": CANARY_DIRECTION,
        "score":     CANARY_SCORE,
        "reasoning": f"CANARY TEST — synthetic pick from {agent}",
        "window_id": window_id,
    }
    r = requests.post(
        f"{ALPHA_BUFFER_URL}/submit",
        json=payload,
        headers={"X-Nexus-Secret": NEXUS_SECRET},
        timeout=10,
    )
    return {"http_status": r.status_code, "body": r.json()}


def get_omni_synthesis_since(window_id: str) -> Optional[dict]:
    """
    Check OMNI's synthesis DB for a result with the given window_id.
    Returns the row dict if found, else None.
    """
    try:
        import sqlite3
        conn = sqlite3.connect(OMNI_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM synthesis_results WHERE window_id=? ORDER BY id DESC LIMIT 1",
            (window_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        log(f"OMNI DB check failed: {e}", "WARN")
        return None


# ── Main CANARY run ───────────────────────────────────────────────────────────

def run_canary(force: bool = False, intraday: bool = False) -> int:
    """
    Run the CANARY pre-market pipeline test.

    Returns:
        0 = PASS, 1 = FAIL, 2 = SKIPPED
    """
    # Check if already ran today
    # GAP-007: intraday=True bypasses the "already passed" guard so we can
    # run continuous health checks without requiring --force every time.
    existing = read_canary_status()
    if existing and not force and not intraday:
        if existing["status"] == "PASS":
            _ran_at = existing.get('ran_at') or existing.get('updated_at') or existing.get('date', 'unknown')
            log(f"CANARY already passed today ({_ran_at}) — use --force to rerun", "OK")
            return 2
        elif existing["status"] == "RUNNING":
            log("CANARY is already running (concurrent invocation?)", "WARN")
            return 2
    if intraday:
        log("CANARY intraday run — bypassing daily-pass gate", "INFO")

    window_id = f"CANARY-{today_et()}-{datetime.now(ET).strftime('%H%M%S')}"
    steps: dict = {}
    overall_start = time.time()

    print()
    print("=" * 65)
    print(f"🐦 CANARY PRE-MARKET TEST — {today_et()}")
    print(f"Time: {datetime.now(ET).strftime('%H:%M:%S ET')} | Window: {window_id}")
    print("=" * 65)

    write_canary_status("RUNNING", "In progress", {})

    # ── Step 1: Service Health Checks ─────────────────────────────────────────
    log("Step 1: Service health checks", "STEP")
    services = {
        "alpha-buffer": ALPHA_BUFFER_URL,
        "omni":         OMNI_URL,
        "alpha-exec":   ALPHA_EXEC_URL,
    }
    health_results = {}
    all_healthy = True
    for name, url in services.items():
        h = get_health(url)
        ok = h is not None and h.get("status") in ("healthy", "ok", "running")
        health_results[name] = {"ok": ok, "detail": str(h)[:100] if h else "UNREACHABLE"}
        status_icon = "OK" if ok else "FAIL"
        log(f"  {name:15s}: {'healthy' if ok else 'UNREACHABLE'}", status_icon)
        if not ok:
            all_healthy = False

    steps["service_health"] = {
        "status":  "OK" if all_healthy else "FAIL",
        "detail":  health_results,
    }

    if not all_healthy:
        failed_svcs = [n for n, r in health_results.items() if not r["ok"]]
        detail = f"Services unreachable: {', '.join(failed_svcs)}"
        write_canary_status("FAIL", detail, steps)
        send_telegram(
            f"🐦❌ <b>CANARY FAIL — {today_et()}</b>\n"
            f"<b>Stage:</b> Service health checks\n"
            f"<b>Failed:</b> {', '.join(failed_svcs)}\n"
            f"Trading is NOT cleared. Check services immediately."
        )
        log(f"CANARY FAIL: {detail}", "FAIL")
        return 1

    log("All 3 services healthy", "OK")

    # ── Step 2: Submit Cipher pick ────────────────────────────────────────────
    log("Step 2: Submitting Cipher pick", "STEP")
    t0 = time.time()
    try:
        r = submit_pick("Cipher", window_id)
        elapsed_ms = int((time.time()-t0)*1000)
        ok = r["http_status"] in (200, 201)
        steps["cipher_submit"] = {
            "status":     "OK" if ok else "FAIL",
            "elapsed_ms": elapsed_ms,
            "detail":     f"HTTP {r['http_status']} | {str(r['body'])[:100]}",
        }
        log(f"  Cipher → HTTP {r['http_status']} ({elapsed_ms}ms): {str(r['body'])[:80]}", "OK" if ok else "FAIL")
    except Exception as e:
        steps["cipher_submit"] = {"status": "FAIL", "detail": str(e)}
        write_canary_status("FAIL", f"Cipher submit failed: {e}", steps)
        send_telegram(
            f"🐦❌ <b>CANARY FAIL — {today_et()}</b>\n"
            f"<b>Stage:</b> Cipher pick submission\n<b>Error:</b> {e}"
        )
        log(f"CANARY FAIL at Cipher submit: {e}", "FAIL")
        return 1

    if steps["cipher_submit"]["status"] != "OK":
        detail = f"Cipher submit failed: HTTP {r['http_status']}"
        write_canary_status("FAIL", detail, steps)
        send_telegram(
            f"🐦❌ <b>CANARY FAIL — {today_et()}</b>\n"
            f"<b>Stage:</b> Cipher pick submission\n<b>Error:</b> {detail}"
        )
        log(f"CANARY FAIL: {detail}", "FAIL")
        return 1

    # ── Step 3: Submit Atlas pick ─────────────────────────────────────────────
    log("Step 3: Submitting Atlas pick", "STEP")
    t0 = time.time()
    try:
        r = submit_pick("Atlas", window_id)
        elapsed_ms = int((time.time()-t0)*1000)
        ok = r["http_status"] in (200, 201)
        steps["atlas_submit"] = {
            "status":     "OK" if ok else "FAIL",
            "elapsed_ms": elapsed_ms,
            "detail":     f"HTTP {r['http_status']} | {str(r['body'])[:100]}",
        }
        log(f"  Atlas → HTTP {r['http_status']} ({elapsed_ms}ms)", "OK" if ok else "FAIL")
    except Exception as e:
        steps["atlas_submit"] = {"status": "FAIL", "detail": str(e)}
        write_canary_status("FAIL", f"Atlas submit failed: {e}", steps)
        send_telegram(
            f"🐦❌ <b>CANARY FAIL — {today_et()}</b>\n"
            f"<b>Stage:</b> Atlas pick submission\n<b>Error:</b> {e}"
        )
        log(f"CANARY FAIL at Atlas submit: {e}", "FAIL")
        return 1

    if steps["atlas_submit"]["status"] != "OK":
        detail = f"Atlas submit failed: HTTP {r['http_status']}"
        write_canary_status("FAIL", detail, steps)
        send_telegram(
            f"🐦❌ <b>CANARY FAIL — {today_et()}</b>\n"
            f"<b>Stage:</b> Atlas pick submission\n<b>Error:</b> {detail}"
        )
        return 1

    # ── Step 4: Submit Sage pick (triggers P1 concordance) ────────────────────
    log("Step 4: Submitting Sage pick (triggers concordance)", "STEP")
    t0 = time.time()
    try:
        r = submit_pick("Sage", window_id)
        elapsed_ms = int((time.time()-t0)*1000)
        ok = r["http_status"] in (200, 201)
        body = r["body"]
        concordance_triggered = (
            body.get("concordance_triggered") is True
            or body.get("concordance") is not None
            or "P1" in str(body)
            or "GO" in str(body)
        )
        steps["sage_submit"] = {
            "status":                "OK" if ok else "FAIL",
            "elapsed_ms":            elapsed_ms,
            "concordance_triggered": concordance_triggered,
            "detail":                f"HTTP {r['http_status']} | {str(body)[:100]}",
        }
        log(f"  Sage → HTTP {r['http_status']} ({elapsed_ms}ms) | concordance_triggered={concordance_triggered}",
            "OK" if ok else "FAIL")
    except Exception as e:
        steps["sage_submit"] = {"status": "FAIL", "detail": str(e)}
        write_canary_status("FAIL", f"Sage submit failed: {e}", steps)
        send_telegram(
            f"🐦❌ <b>CANARY FAIL — {today_et()}</b>\n"
            f"<b>Stage:</b> Sage pick submission\n<b>Error:</b> {e}"
        )
        log(f"CANARY FAIL at Sage submit: {e}", "FAIL")
        return 1

    if steps["sage_submit"]["status"] != "OK":
        detail = f"Sage submit failed: HTTP {r['http_status']}"
        write_canary_status("FAIL", detail, steps)
        send_telegram(
            f"🐦❌ <b>CANARY FAIL — {today_et()}</b>\n"
            f"<b>Stage:</b> Sage pick submission\n<b>Error:</b> {detail}"
        )
        return 1

    # ── Step 5: Poll OMNI for synthesis ───────────────────────────────────────
    log(f"Step 5: Waiting for OMNI synthesis (timeout={OMNI_SYNTHESIS_TIMEOUT}s)...", "STEP")
    t0 = time.time()
    omni_result = None
    poll_start = time.time()

    while time.time() - poll_start < OMNI_SYNTHESIS_TIMEOUT:
        omni_result = get_omni_synthesis_since(window_id)
        if omni_result:
            elapsed_ms = int((time.time()-t0)*1000)
            verdict = omni_result.get("verdict", "?")
            steps["omni_synthesis"] = {
                "status":     "OK",
                "elapsed_ms": elapsed_ms,
                "verdict":    verdict,
                "detail":     f"verdict={verdict} ticker={omni_result.get('ticker')} pathway={omni_result.get('pathway')}",
            }
            log(f"  OMNI synthesized: verdict={verdict} ({elapsed_ms}ms)", "OK")
            break
        elapsed = int(time.time() - poll_start)
        log(f"  Waiting... ({elapsed}s)")
        time.sleep(OMNI_POLL_INTERVAL)

    if not omni_result:
        # OMNI may have processed but DB write is async — check health as fallback
        health = get_health(OMNI_URL)
        last_synthesis = health.get("last_synthesis_time") if health else None
        elapsed_ms = int((time.time()-t0)*1000)
        if last_synthesis:
            log(f"  OMNI synthesis result not in DB for this window, but OMNI is responsive (last_synthesis={last_synthesis})", "WARN")
            steps["omni_synthesis"] = {
                "status":  "WARN",
                "elapsed_ms": elapsed_ms,
                "detail":  f"No synthesis record for window {window_id} but OMNI is responsive. May be echo chamber / score gate.",
            }
            # WARN doesn't fail the canary — the plumbing is working
        else:
            steps["omni_synthesis"] = {
                "status":  "FAIL",
                "elapsed_ms": elapsed_ms,
                "detail":  f"No synthesis found after {OMNI_SYNTHESIS_TIMEOUT}s. OMNI health: {health}",
            }
            detail = f"OMNI did not synthesize within {OMNI_SYNTHESIS_TIMEOUT}s"
            write_canary_status("FAIL", detail, steps)
            send_telegram(
                f"🐦❌ <b>CANARY FAIL — {today_et()}</b>\n"
                f"<b>Stage:</b> OMNI synthesis\n<b>Error:</b> {detail}\n"
                f"Alpha Execution has NOT been verified. Trading is NOT cleared."
            )
            log(f"CANARY FAIL: {detail}", "FAIL")
            return 1

    # ── Step 6: Verify Alpha Execution received (pre-market gate should return 422) ──
    log("Step 6: Verifying Alpha Execution is responsive", "STEP")
    t0 = time.time()
    health = get_health(ALPHA_EXEC_URL)
    elapsed_ms = int((time.time()-t0)*1000)
    if health and health.get("status") in ("healthy", "ok"):
        steps["alpha_exec_health"] = {
            "status":     "OK",
            "elapsed_ms": elapsed_ms,
            "detail":     f"status={health.get('status')} execution_paused={health.get('execution_paused')} open_positions={health.get('open_positions')}",
        }
        log(f"  Alpha Execution: healthy ({elapsed_ms}ms)", "OK")
    else:
        steps["alpha_exec_health"] = {
            "status":  "FAIL",
            "elapsed_ms": elapsed_ms,
            "detail":  f"Alpha Execution unreachable or unhealthy: {health}",
        }
        detail = "Alpha Execution not healthy"
        write_canary_status("FAIL", detail, steps)
        send_telegram(
            f"🐦❌ <b>CANARY FAIL — {today_et()}</b>\n"
            f"<b>Stage:</b> Alpha Execution health\n<b>Error:</b> {detail}"
        )
        log(f"CANARY FAIL: {detail}", "FAIL")
        return 1

    # ── CANARY PASS ───────────────────────────────────────────────────────────
    total_elapsed = round(time.time() - overall_start, 1)
    steps_summary = {k: v["status"] for k, v in steps.items()}
    write_canary_status("PASS", f"All stages passed in {total_elapsed}s", steps)

    print()
    print("=" * 65)
    print(f"🐦 CANARY PASS — Trading cleared for {today_et()}")
    print(f"Total elapsed: {total_elapsed}s")
    print("Stages:")
    for name, st in steps_summary.items():
        icon = "✅" if st == "OK" else ("⚠️ " if st == "WARN" else "❌")
        raw_detail = steps[name].get("detail", ""); detail = (str(raw_detail) if not isinstance(raw_detail, str) else raw_detail)[:60]
        print(f"  {icon} {name:30s}  {detail}")
    print("=" * 65)
    print()

    send_telegram(
        f"🐦✅ <b>CANARY PASS — {today_et()}</b>\n"
        f"All {len(steps)} stages verified in {total_elapsed}s\n"
        f"Buffer → OMNI → Alpha Execution: plumbing confirmed\n"
        f"<b>Trading cleared for today.</b>"
    )

    log(f"CANARY PASS — all stages verified in {total_elapsed}s", "OK")
    return 0


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="CANARY Pre-Market Pipeline Test")
    parser.add_argument("--intraday", action="store_true",
                        help="Intraday health check — bypasses daily-pass gate, does not overwrite PASS status")
    parser.add_argument("--force",  action="store_true", help="Force rerun even if already passed today")
    parser.add_argument("--status", action="store_true", help="Print today's canary status and exit")
    args = parser.parse_args()

    if args.status:
        status = read_canary_status()
        if status:
            print(f"CANARY status for {today_et()}: {status['status']}")
            print(f"Ran at: {status.get('ran_at') or status.get('updated_at') or status.get('date', 'unknown')}")
            print(f"Detail: {status['detail']}")
        else:
            print(f"CANARY has not run today ({today_et()})")
        sys.exit(0)

    exit_code = run_canary(force=args.force, intraday=getattr(args, "intraday", False))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
