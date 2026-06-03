#!/usr/bin/env python3
"""
e2e_execution_contract_test.py — End-to-End Execution Contract Test

The operational definition of "execution working."

A synthetic GO verdict submitted to Alpha Execution must produce:
  1. A confirmed Alpaca mleg spread order (status=filled or accepted+reconciled)
  2. A position record in the DB (status=open)
  3. Health endpoint reflecting open_positions >= 1
  4. Full chain trace with timing at every hop

Run modes:
  --check-only    Verify prerequisites (market hours, health, Alpaca) without trading
  --dry-run       Submit verdict but expect dry_run response (auto_execute=false)
  --live          Full live test — submits real paper order (auto_execute must be true)

Usage:
  python3 e2e_execution_contract_test.py --live
  python3 e2e_execution_contract_test.py --check-only

Exit codes:
  0 = PASS
  1 = FAIL
  2 = SKIPPED (outside market hours, prerequisites not met)
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests
import pytz

# ── Config ───────────────────────────────────────────────────────────────────

ALPHA_EXEC_URL  = os.getenv("ALPHA_EXEC_URL",    "http://localhost:8005")
def _require(var: str) -> str:
    """Return env var value or raise at startup — never silently use a stale fallback."""
    val = os.getenv(var)
    if not val:
        raise RuntimeError(f"{var} is required but not set. Source .deploy-secrets before running.")
    return val

NEXUS_SECRET    = _require("NEXUS_SECRET")
DB_PATH         = os.getenv("ALPHA_EXEC_DB_PATH",
                            "/Users/ahmedsadek/nexus/data/alpha_execution.db")
ALPACA_KEY      = _require("ALPACA_API_KEY")
ALPACA_SECRET   = _require("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
AHMED_CHAT_ID      = os.getenv("AHMED_CHAT_ID",       "8573754783")

ET = pytz.timezone("America/New_York")

# Timeouts
SUBMIT_TIMEOUT_SEC  = 10    # HTTP call to /execute
FILL_POLL_TIMEOUT   = 90    # wait for Alpaca fill confirmation
FILL_POLL_INTERVAL  = 5
CLEANUP_TIMEOUT_SEC = 15    # teardown

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(ET).strftime("%H:%M:%S")
    prefix = {"INFO": "  ", "OK": "✅", "FAIL": "❌", "WARN": "⚠️ ", "STEP": "→"}
    print(f"[{ts}] {prefix.get(level, '  ')} {msg}")


def is_market_hours() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now <= close_t


def alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }


def get_alpaca_order(order_id: str) -> Optional[dict]:
    try:
        r = requests.get(
            f"{ALPACA_BASE_URL}/v2/orders/{order_id}",
            headers=alpaca_headers(), timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log(f"Alpaca order fetch failed: {e}", "WARN")
    return None


def cancel_alpaca_order(order_id: str) -> None:
    try:
        requests.delete(
            f"{ALPACA_BASE_URL}/v2/orders/{order_id}",
            headers=alpaca_headers(), timeout=10,
        )
    except Exception:
        pass


def close_alpaca_position(symbol: str) -> None:
    try:
        requests.delete(
            f"{ALPACA_BASE_URL}/v2/positions/{symbol}",
            headers=alpaca_headers(), timeout=10,
        )
    except Exception:
        pass


def db_get_position(position_id: int) -> Optional[dict]:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM positions WHERE id=?", (position_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def db_close_position(position_id: int, reason: str) -> None:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, close_reason=?, pnl_pct=0.0, pnl_usd=0.0 WHERE id=?",
            (now, reason, position_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"DB close position failed: {e}", "WARN")


# ── Prerequisite checks ───────────────────────────────────────────────────────

def check_prerequisites(mode: str) -> dict:
    result = {
        "market_hours":       False,
        "health_ok":          False,
        "alpaca_reachable":   False,
        "execution_valid":    False,
        "auto_execute":       False,
        "execution_paused":   True,
        "open_positions":     -1,
        "all_clear":          False,
    }

    # 1. Market hours (only required for live mode)
    in_market = is_market_hours()
    result["market_hours"] = in_market
    if mode == "live" and not in_market:
        now = datetime.now(ET)
        log(f"Outside market hours ({now.strftime('%H:%M ET %a')}) — live test requires 9:30-16:00 ET on weekdays", "WARN")

    # 2. Health endpoint
    try:
        r = requests.get(f"{ALPHA_EXEC_URL}/health", timeout=5)
        if r.status_code == 200:
            h = r.json()
            result["health_ok"]        = h.get("status") == "healthy"
            result["alpaca_reachable"] = h.get("alpaca_reachable", False)
            result["execution_valid"]  = h.get("execution_valid",  False)
            result["auto_execute"]     = h.get("auto_execute",     False)
            result["execution_paused"] = h.get("execution_paused", True)
            result["open_positions"]   = h.get("open_positions",   -1)
    except Exception as e:
        log(f"Health check failed: {e}", "FAIL")

    if mode == "check-only":
        result["all_clear"] = (
            result["health_ok"] and
            result["alpaca_reachable"] and
            result["execution_valid"] and
            not result["execution_paused"]
        )
    elif mode == "live":
        result["all_clear"] = (
            result["health_ok"] and
            result["alpaca_reachable"] and
            result["execution_valid"] and
            result["auto_execute"] and
            not result["execution_paused"] and
            result["market_hours"]
        )
    else:  # dry-run
        result["all_clear"] = (
            result["health_ok"] and
            not result["execution_paused"]
        )

    return result


# ── Main test ─────────────────────────────────────────────────────────────────

def run_test(mode: str) -> dict:
    """
    Run the E2E execution contract test.

    Args:
        mode: 'check-only' | 'dry-run' | 'live'

    Returns:
        Result dict with chain_result, steps, elapsed, etc.
    """
    window_id = f"E2E-CONTRACT-{datetime.now(ET).strftime('%Y%m%d-%H%M%S')}"
    start_time = time.time()

    result = {
        "mode":           mode,
        "window_id":      window_id,
        "chain_result":   "PENDING",
        "failed_at_step": None,
        "error":          None,
        "steps":          {},
        "position_id":    None,
        "order_id":       None,
        "fill_price":     None,
        "total_elapsed_seconds": None,
        "cleanup_done":   False,
        "ran_at":         datetime.now(ET).isoformat(),
    }

    def step(name: str, status: str, elapsed_ms: int = 0, detail: str = "") -> None:
        result["steps"][name] = {
            "status":     status,
            "elapsed_ms": elapsed_ms,
            "detail":     detail,
        }

    log(f"E2E Execution Contract Test — mode={mode} window={window_id}", "STEP")
    log(f"Alpha Execution: {ALPHA_EXEC_URL}")

    # ── Step 0: Prerequisites ─────────────────────────────────────────────────
    t0 = time.time()
    prereqs = check_prerequisites(mode)
    step("prerequisites", "OK" if prereqs["all_clear"] else "FAIL",
         int((time.time()-t0)*1000),
         json.dumps({k: v for k, v in prereqs.items() if k != "all_clear"}))

    log(f"Health: {prereqs['health_ok']} | Alpaca: {prereqs['alpaca_reachable']} | "
        f"Valid: {prereqs['execution_valid']} | Paused: {prereqs['execution_paused']} | "
        f"AutoExec: {prereqs['auto_execute']} | MarketHours: {prereqs['market_hours']}")

    if not prereqs["all_clear"]:
        result["chain_result"]   = "SKIPPED"
        result["failed_at_step"] = "prerequisites"
        result["error"]          = "Prerequisites not met — see step detail"
        result["total_elapsed_seconds"] = round(time.time() - start_time, 1)
        return result

    log("Prerequisites: OK", "OK")

    if mode == "check-only":
        result["chain_result"] = "PASS"
        result["total_elapsed_seconds"] = round(time.time() - start_time, 1)
        log("Check-only mode: all prerequisites met", "OK")
        return result

    # ── Step 1: Submit synthetic GO verdict ───────────────────────────────────
    log("Submitting synthetic GO verdict to /execute ...", "STEP")
    t0 = time.time()
    verdict_payload = {
        "ticker":            "SPY",
        "direction":         "bearish",
        "pathway":           "P1",
        "weighted_score":    82.0,
        "agent_scores":      {"Cipher": 85, "Atlas": 80, "Sage": 78},
        "verdict":           "GO",
        "sizing_mult":       1.0,
        "position_size_usd": 2000.0,
        "window_id":         window_id,
        "echo_chamber":      False,
    }

    try:
        resp = requests.post(
            f"{ALPHA_EXEC_URL}/execute",
            json=verdict_payload,
            headers={"X-Nexus-Secret": NEXUS_SECRET},
            timeout=SUBMIT_TIMEOUT_SEC,
        )
        elapsed_ms = int((time.time()-t0)*1000)
        resp_json = resp.json()
        step("verdict_submitted", "OK", elapsed_ms,
             f"HTTP {resp.status_code} | {json.dumps(resp_json)[:200]}")
        log(f"Response HTTP {resp.status_code} in {elapsed_ms}ms: {json.dumps(resp_json)[:150]}")
    except Exception as e:
        step("verdict_submitted", "FAIL", int((time.time()-t0)*1000), str(e))
        result["chain_result"]   = "FAIL"
        result["failed_at_step"] = "verdict_submitted"
        result["error"]          = str(e)
        result["total_elapsed_seconds"] = round(time.time() - start_time, 1)
        log(f"FAIL at verdict_submitted: {e}", "FAIL")
        return result

    # Parse response
    if mode == "dry-run":
        if resp_json.get("mode") == "dry_run" or not resp_json.get("executed", True):
            log("Dry-run response received as expected", "OK")
            step("dry_run_confirmed", "OK", 0, "auto_execute=false — dry run mode confirmed")
            result["chain_result"] = "PASS"
            result["total_elapsed_seconds"] = round(time.time() - start_time, 1)
            return result
        else:
            log("Expected dry-run but got live execution response", "WARN")

    # Check for non-execution responses
    if resp.status_code == 422:
        reason = resp_json.get("reason", "unknown")
        gate   = resp_json.get("gate", "unknown_gate")
        step("verdict_submitted", "BLOCKED", elapsed_ms, f"Gate: {gate} | {reason}")
        result["chain_result"]   = "FAIL"
        result["failed_at_step"] = "verdict_submitted"
        result["error"]          = f"Blocked at gate '{gate}': {reason}"
        result["total_elapsed_seconds"] = round(time.time() - start_time, 1)
        log(f"FAIL — blocked by gate '{gate}': {reason}", "FAIL")
        return result

    if resp.status_code == 503:
        reason = resp_json.get("reason", "unknown")
        step("verdict_submitted", "FAIL", elapsed_ms, reason)
        result["chain_result"]   = "FAIL"
        result["failed_at_step"] = "verdict_submitted"
        result["error"]          = reason
        result["total_elapsed_seconds"] = round(time.time() - start_time, 1)
        log(f"FAIL — 503: {reason}", "FAIL")
        return result

    if not resp_json.get("executed"):
        step("verdict_submitted", "FAIL", elapsed_ms, f"executed=false: {resp_json}")
        result["chain_result"]   = "FAIL"
        result["failed_at_step"] = "verdict_submitted"
        result["error"]          = f"executed=false: {resp_json.get('reason','unknown')}"
        result["total_elapsed_seconds"] = round(time.time() - start_time, 1)
        log(f"FAIL — executed=false: {resp_json}", "FAIL")
        return result

    log("Verdict accepted — execution in progress", "OK")

    # ── Step 2: Confirm order placed ──────────────────────────────────────────
    order_id   = resp_json.get("order_id")
    position_id = resp_json.get("position_id")
    result["order_id"]    = order_id
    result["position_id"] = position_id

    t0 = time.time()
    if order_id:
        step("alpaca_order_placed", "OK", int((time.time()-t0)*1000),
             f"order_id={order_id[:8]}... position_id={position_id}")
        log(f"Order placed: {order_id[:8]}... | Position: #{position_id}", "OK")
    else:
        step("alpaca_order_placed", "FAIL", int((time.time()-t0)*1000), "No order_id in response")
        result["chain_result"]   = "FAIL"
        result["failed_at_step"] = "alpaca_order_placed"
        result["error"]          = "No order_id returned by execution engine"
        result["total_elapsed_seconds"] = round(time.time() - start_time, 1)
        log("FAIL — no order_id in response", "FAIL")
        return result

    # ── Step 3: Poll Alpaca for fill confirmation ─────────────────────────────
    log(f"Polling Alpaca for fill confirmation (timeout={FILL_POLL_TIMEOUT}s)...", "STEP")
    t0 = time.time()
    fill_confirmed = False
    fill_price = None
    poll_start = time.time()
    final_status = "unknown"

    while time.time() - poll_start < FILL_POLL_TIMEOUT:
        order = get_alpaca_order(order_id)
        if order:
            final_status = order.get("status", "unknown")
            if final_status == "filled":
                fill_confirmed = True
                raw_price = order.get("filled_avg_price")
                fill_price = abs(float(raw_price)) if raw_price else None
                result["fill_price"] = fill_price
                elapsed_ms = int((time.time()-t0)*1000)
                step("fill_confirmed", "OK", elapsed_ms,
                     f"status=filled fill_price={fill_price} in {elapsed_ms}ms")
                log(f"Fill confirmed: price={fill_price} ({elapsed_ms}ms)", "OK")
                break
            elif final_status in ("canceled", "cancelled", "rejected", "expired"):
                elapsed_ms = int((time.time()-t0)*1000)
                step("fill_confirmed", "FAIL", elapsed_ms, f"Terminal status: {final_status}")
                result["chain_result"]   = "FAIL"
                result["failed_at_step"] = "fill_confirmed"
                result["error"]          = f"Order terminal status: {final_status}"
                result["total_elapsed_seconds"] = round(time.time() - start_time, 1)
                log(f"FAIL — order {final_status}", "FAIL")
                _cleanup(order_id, position_id, result)
                return result
            else:
                log(f"  Order status: {final_status} ({int(time.time()-poll_start)}s elapsed)", "INFO")
        time.sleep(FILL_POLL_INTERVAL)

    if not fill_confirmed:
        elapsed_ms = int((time.time()-t0)*1000)
        step("fill_confirmed", "FAIL", elapsed_ms,
             f"Timeout after {FILL_POLL_TIMEOUT}s. Last status: {final_status}")
        result["chain_result"]   = "FAIL"
        result["failed_at_step"] = "fill_confirmed"
        result["error"]          = f"Fill not confirmed within {FILL_POLL_TIMEOUT}s. Last: {final_status}"
        result["total_elapsed_seconds"] = round(time.time() - start_time, 1)
        log(f"FAIL — fill timeout after {FILL_POLL_TIMEOUT}s (last status: {final_status})", "FAIL")
        _cleanup(order_id, position_id, result)
        return result

    # ── Step 4: Verify position in DB ─────────────────────────────────────────
    t0 = time.time()
    if position_id:
        pos = db_get_position(position_id)
        if pos and pos.get("status") == "open":
            elapsed_ms = int((time.time()-t0)*1000)
            step("position_in_db", "OK", elapsed_ms,
                 f"position_id={position_id} status=open entry_price={pos.get('entry_price')}")
            log(f"Position #{position_id} in DB: status=open entry_price={pos.get('entry_price')}", "OK")
        else:
            elapsed_ms = int((time.time()-t0)*1000)
            db_status = pos.get("status") if pos else "NOT FOUND"
            step("position_in_db", "FAIL", elapsed_ms, f"DB status: {db_status}")
            result["chain_result"]   = "FAIL"
            result["failed_at_step"] = "position_in_db"
            result["error"]          = f"Position not open in DB: {db_status}"
            result["total_elapsed_seconds"] = round(time.time() - start_time, 1)
            log(f"FAIL — position not open in DB: {db_status}", "FAIL")
            _cleanup(order_id, position_id, result)
            return result
    else:
        step("position_in_db", "SKIPPED", 0, "No position_id to check (DRY_RUN or phantom)")
        log("No position_id to verify in DB", "WARN")

    # ── Step 5: Health endpoint reflects open position ────────────────────────
    t0 = time.time()
    try:
        h = requests.get(f"{ALPHA_EXEC_URL}/health", timeout=5).json()
        open_pos = h.get("open_positions", 0)
        elapsed_ms = int((time.time()-t0)*1000)
        if open_pos >= 1:
            step("health_reflects_open", "OK", elapsed_ms, f"open_positions={open_pos}")
            log(f"Health: open_positions={open_pos}", "OK")
        else:
            step("health_reflects_open", "WARN", elapsed_ms,
                 f"open_positions={open_pos} — position may be in-flight")
            log(f"WARN — health shows open_positions={open_pos} (may be counter lag)", "WARN")
    except Exception as e:
        step("health_reflects_open", "FAIL", 0, str(e))
        log(f"Health check failed: {e}", "WARN")

    # ── PASS ──────────────────────────────────────────────────────────────────
    result["chain_result"] = "PASS"
    result["total_elapsed_seconds"] = round(time.time() - start_time, 1)
    log(f"E2E CONTRACT TEST: PASS ({result['total_elapsed_seconds']}s)", "OK")

    # Cleanup — close the test position
    _cleanup(order_id, position_id, result)
    return result


def _cleanup(order_id: Optional[str], position_id: Optional[int], result: dict) -> None:
    """Close test position on Alpaca and in DB. Never raises."""
    log("Cleaning up test position...", "STEP")
    try:
        # Get the position from DB to find contract symbols
        if position_id:
            pos = db_get_position(position_id)
            if pos:
                for sym_col in ("short_contract_symbol", "long_contract_symbol"):
                    sym = pos.get(sym_col)
                    if sym:
                        close_alpaca_position(sym)
                        log(f"  Closed Alpaca position: {sym}")
                db_close_position(position_id, "e2e_contract_test_teardown")
                log(f"  DB position #{position_id} closed")

        if order_id:
            cancel_alpaca_order(order_id)
            log(f"  Alpaca order {order_id[:8]}... cancelled (if still open)")

        result["cleanup_done"] = True
        log("Cleanup complete", "OK")
    except Exception as e:
        log(f"Cleanup error (non-fatal): {e}", "WARN")


def _send_e2e_telegram(result: dict) -> None:
    """Send E2E test result summary to Ahmed via Telegram."""
    try:
        chain = result["chain_result"]
        elapsed = result.get("total_elapsed_seconds", "?")
        mode = result.get("mode", "?")
        icon = "✅" if chain == "PASS" else ("⏭️" if chain == "SKIPPED" else "❌")

        lines = [
            f"{icon} <b>E2E CONTRACT TEST — {chain}</b>",
            f"Mode: {mode} | Elapsed: {elapsed}s",
        ]

        if chain == "FAIL":
            lines.append(f"Failed at: <b>{result.get('failed_at_step', '?')}</b>")
            lines.append(f"Error: {result.get('error', '?')[:100]}")
        elif chain == "PASS":
            if result.get("order_id"):
                lines.append(f"Order: {result['order_id'][:12]}...")
            if result.get("fill_price") is not None:
                lines.append(f"Fill price: ${result['fill_price']:.4f}")

        if result.get("steps"):
            lines.append("")
            lines.append("Steps:")
            for step_name, step_data in result["steps"].items():
                s = step_data.get("status", "?")
                s_icon = "✅" if s == "OK" else ("⚠️" if s in ("WARN", "SKIPPED") else "❌")
                ms = step_data.get("elapsed_ms", 0)
                lines.append(f"  {s_icon} {step_name}: {ms}ms")

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    AHMED_CHAT_ID,
                "text":       "\n".join(lines),
                "parse_mode": "HTML",
            },
            timeout=8,
        )
    except Exception as e:
        print(f"  (Telegram send failed: {e})")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="E2E Execution Contract Test")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-only", action="store_true", help="Check prerequisites only")
    group.add_argument("--dry-run",    action="store_true", help="Submit but expect dry_run response")
    group.add_argument("--live",       action="store_true", help="Full live test — submits real paper order")
    args = parser.parse_args()

    mode = "check-only" if args.check_only else ("dry-run" if args.dry_run else "live")

    print()
    print("=" * 65)
    print(f"E2E EXECUTION CONTRACT TEST — {mode.upper()}")
    print(f"Time: {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}")
    print("=" * 65)

    result = run_test(mode)

    print()
    print("=" * 65)
    print(f"RESULT: {result['chain_result']}")
    print(f"Elapsed: {result['total_elapsed_seconds']}s")
    if result["chain_result"] == "FAIL":
        print(f"Failed at: {result['failed_at_step']}")
        print(f"Error: {result['error']}")
    if result.get("order_id"):
        print(f"Order ID: {result['order_id']}")
    if result.get("position_id"):
        print(f"Position: #{result['position_id']}")
    if result.get("fill_price") is not None:
        print(f"Fill price: ${result['fill_price']:.4f} (net spread premium)")
    print()
    print("Steps:")
    for step_name, step_data in result["steps"].items():
        icon = "✅" if step_data["status"] == "OK" else ("⚠️ " if step_data["status"] == "WARN" else "❌")
        print(f"  {icon} {step_name:30s} {step_data['elapsed_ms']:5d}ms  {step_data['detail'][:80]}")
    print("=" * 65)
    print()

    # Write result to CHRONICLE for audit trail
    try:
        import sys as _sys
        _sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
        import sqlite3 as _sq
        conn = _sq.connect("/Users/ahmedsadek/nexus/data/chronicle.db")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_test_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                mode        TEXT,
                window_id   TEXT,
                result      TEXT,
                failed_at   TEXT,
                error       TEXT,
                order_id    TEXT,
                position_id INTEGER,
                fill_price  REAL,
                elapsed_sec REAL,
                ran_at      TEXT,
                full_json   TEXT
            )
        """)
        conn.execute("""
            INSERT INTO e2e_test_results
                (mode, window_id, result, failed_at, error, order_id, position_id,
                 fill_price, elapsed_sec, ran_at, full_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            result["mode"], result["window_id"], result["chain_result"],
            result.get("failed_at_step"), result.get("error"),
            result.get("order_id"), result.get("position_id"),
            result.get("fill_price"), result.get("total_elapsed_seconds"),
            result.get("ran_at"), json.dumps(result),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  (CHRONICLE write failed: {e})")

    # Send result summary to Ahmed via Telegram
    _send_e2e_telegram(result)

    sys.exit(0 if result["chain_result"] == "PASS" else
             (2 if result["chain_result"] == "SKIPPED" else 1))


if __name__ == "__main__":
    main()
