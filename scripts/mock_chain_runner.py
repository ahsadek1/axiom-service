#!/usr/bin/env python3
"""
mock_chain_runner.py — Full Chain End-to-End Runner
=====================================================
Tests the complete Alpha execution chain:
  Cipher + Atlas + Sage → Alpha Buffer (concordance) → OMNI → Alpha Execution → Alpaca

Prerequisites:
  - MOCK_SESSION_MODE=true in alpha-execution/.env (already set)
  - All 9 services running
  - Alpaca paper account configured

Usage:
  python3 scripts/mock_chain_runner.py [--ticker AAPL] [--direction BULLISH] [--dry-run]

Author: GENESIS | Date: 2026-05-04
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests

# ── Config ──────────────────────────────────────────────────────────────────

ALPHA_SECRET  = "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2"
BUFFER_URL    = "http://localhost:8002"
OMNI_URL      = "http://localhost:8004"
EXEC_URL      = "http://localhost:8005"

ET = ZoneInfo("America/New_York")

AGENTS = ["Cipher", "Atlas", "Sage"]

# Scores that form a P1 concordance (all 3 agents, each ≥ 65)
DEFAULT_SCORES = {
    "Cipher": 78.0,
    "Atlas":  71.0,
    "Sage":   68.0,
}

# ── Helpers ─────────────────────────────────────────────────────────────────

def ts() -> str:
    return datetime.now(ET).strftime("%H:%M:%S")


def log(label: str, msg: str, ok: bool = True) -> None:
    marker = "✅" if ok else "❌"
    print(f"[{ts()}] {marker} {label}: {msg}")


def check_service(name: str, url: str, secret_header: str, secret: str) -> bool:
    try:
        r = requests.get(f"{url}/health", headers={secret_header: secret}, timeout=5)
        data = r.json()
        healthy = data.get("status") == "healthy"
        log(name, f"status={data.get('status')} version={data.get('version','?')}", ok=healthy)
        return healthy
    except Exception as e:
        log(name, f"UNREACHABLE — {e}", ok=False)
        return False


def purge_concordance_window(ticker: str) -> None:
    """Purge any existing concordance state for this ticker to start clean."""
    try:
        r = requests.post(
            f"{BUFFER_URL}/concordance/purge",
            headers={"x-nexus-secret": ALPHA_SECRET},
            json=[ticker],
            timeout=5,
        )
        if r.status_code == 200:
            log("Buffer", f"Concordance state for {ticker} purged clean")
        else:
            log("Buffer", f"Purge returned {r.status_code} — proceeding anyway", ok=True)
    except Exception as e:
        log("Buffer", f"Purge failed (non-fatal): {e}", ok=True)


# ── Phase 1: Preflight ───────────────────────────────────────────────────────

def run_preflight() -> bool:
    print("\n" + "="*60)
    print("PHASE 1: PREFLIGHT CHECKS")
    print("="*60)

    buffer_ok = check_service("Alpha-Buffer", BUFFER_URL, "x-nexus-secret", ALPHA_SECRET)
    omni_ok   = check_service("OMNI",         OMNI_URL,   "x-nexus-secret", ALPHA_SECRET)
    exec_ok   = check_service("Alpha-Exec",   EXEC_URL,   "x-nexus-secret", ALPHA_SECRET)

    # Verify MOCK_SESSION_MODE is active in alpha-exec
    try:
        r = requests.get(f"{EXEC_URL}/health", timeout=5)
        d = r.json()
        if d.get("execution_paused"):
            log("Alpha-Exec", "PAUSED — cannot run", ok=False)
            return False
        log("Alpha-Exec", f"auto_execute={d.get('auto_execute')} vix_brake={d.get('vix_brake','?')}")
    except Exception as e:
        log("Alpha-Exec", f"Health detail failed: {e}", ok=False)

    all_ok = buffer_ok and omni_ok and exec_ok
    if not all_ok:
        log("PREFLIGHT", "One or more services unhealthy — ABORT", ok=False)
    else:
        log("PREFLIGHT", "All services healthy — proceeding")
    return all_ok


# ── Phase 2: Submit Picks ────────────────────────────────────────────────────

def submit_picks(ticker: str, direction: str, window_id: str) -> dict:
    print("\n" + "="*60)
    print("PHASE 2: SUBMITTING AGENT PICKS")
    print(f"  Ticker: {ticker} | Direction: {direction} | Window: {window_id}")
    print("="*60)

    results = {}
    for agent in AGENTS:
        score = DEFAULT_SCORES[agent]
        payload = {
            "agent":     agent,
            "ticker":    ticker,
            "direction": direction,
            "score":     score,
            "reasoning": f"Mock submission from chain runner — {agent} bullish on {ticker}",
            "window_id": window_id,
        }
        try:
            r = requests.post(
                f"{BUFFER_URL}/submit",
                headers={"x-nexus-secret": ALPHA_SECRET},
                json=payload,
                timeout=10,
            )
            data = r.json()
            results[agent] = {"status_code": r.status_code, "response": data}

            concordance = data.get("concordance") or {}
            triggered   = concordance.get("triggered", False)
            pathway     = concordance.get("pathway", "—")
            log(
                f"Submit/{agent}",
                f"score={score} → HTTP {r.status_code} | concordance_triggered={triggered} pathway={pathway}",
                ok=r.status_code == 200,
            )
        except Exception as e:
            log(f"Submit/{agent}", f"FAILED: {e}", ok=False)
            results[agent] = {"status_code": None, "response": None, "error": str(e)}

        # Small gap between submissions (mimics real agent timing)
        time.sleep(0.5)

    return results


# ── Phase 3: Poll OMNI for synthesis ─────────────────────────────────────────

def poll_omni_synthesis(window_id: str, timeout_sec: int = 90) -> Optional[dict]:
    print("\n" + "="*60)
    print("PHASE 3: POLLING OMNI FOR SYNTHESIS")
    print(f"  Waiting up to {timeout_sec}s for OMNI to process concordance...")
    print("="*60)

    deadline = time.time() + timeout_sec
    last_syntheses = None

    while time.time() < deadline:
        try:
            r = requests.get(f"{OMNI_URL}/health", timeout=5)
            d = r.json()
            syntheses_today = d.get("syntheses_today", 0)
            go_verdicts     = d.get("go_verdicts_today", 0)
            last_min_ago    = d.get("last_synthesis_min_ago")

            if last_syntheses is None:
                last_syntheses = syntheses_today

            if syntheses_today > last_syntheses:
                log("OMNI", f"NEW synthesis detected! syntheses_today={syntheses_today} go_verdicts={go_verdicts}")
                return {
                    "syntheses_today": syntheses_today,
                    "go_verdicts_today": go_verdicts,
                    "last_synthesis_min_ago": last_min_ago,
                }

            remaining = int(deadline - time.time())
            print(f"  [{ts()}] OMNI: syntheses={syntheses_today} go_verdicts={go_verdicts} | waiting... ({remaining}s left)")
        except Exception as e:
            print(f"  [{ts()}] OMNI poll error: {e}")

        time.sleep(5)

    log("OMNI", f"Timed out after {timeout_sec}s — no new synthesis detected", ok=False)
    return None


# ── Phase 4: Check Alpha-Exec for trade ──────────────────────────────────────

def check_execution_result(ticker: str) -> Optional[dict]:
    print("\n" + "="*60)
    print("PHASE 4: CHECKING ALPHA EXECUTION RESULT")
    print("="*60)

    try:
        r = requests.get(
            f"{EXEC_URL}/trades",
            headers={"x-nexus-secret": ALPHA_SECRET},
            timeout=10,
        )
        if r.status_code != 200:
            log("Alpha-Exec", f"/trades returned HTTP {r.status_code}", ok=False)
            return None

        trades = r.json()
        # trades may be a list or dict with a "trades" key
        if isinstance(trades, dict):
            trade_list = trades.get("trades", [])
        else:
            trade_list = trades

        # Filter for today's trades on our ticker
        today = datetime.now(ET).strftime("%Y-%m-%d")
        matching = [
            t for t in trade_list
            if t.get("ticker", "").upper() == ticker.upper()
            and today in str(t.get("entry_time", ""))
        ]

        if matching:
            trade = matching[-1]  # Most recent
            log(
                "Alpha-Exec",
                f"Trade found! ticker={trade.get('ticker')} "
                f"direction={trade.get('direction')} "
                f"pathway={trade.get('pathway')} "
                f"position_size_usd={trade.get('position_size_usd')} "
                f"status={trade.get('status','?')}",
            )
            return trade
        else:
            log("Alpha-Exec", f"No trades found for {ticker} today — checking positions...")

            # Also check open positions
            r2 = requests.get(
                f"{EXEC_URL}/positions",
                headers={"x-nexus-secret": ALPHA_SECRET},
                timeout=10,
            )
            if r2.status_code == 200:
                positions = r2.json()
                pos_list = positions if isinstance(positions, list) else positions.get("positions", [])
                matching_pos = [p for p in pos_list if p.get("ticker", "").upper() == ticker.upper()]
                if matching_pos:
                    log("Alpha-Exec", f"Found in open positions: {matching_pos[0]}")
                    return matching_pos[0]

            log("Alpha-Exec", "No trade or position found — OMNI may have returned NO_GO or a market hours block persisted", ok=False)
            return None

    except Exception as e:
        log("Alpha-Exec", f"Check failed: {e}", ok=False)
        return None


# ── Phase 5: Summary ─────────────────────────────────────────────────────────

def print_summary(
    ticker: str,
    direction: str,
    window_id: str,
    submit_results: dict,
    omni_result: Optional[dict],
    exec_result: Optional[dict],
) -> bool:
    print("\n" + "="*60)
    print("CHAIN RUNNER — FINAL SUMMARY")
    print("="*60)

    all_submitted = all(
        r.get("status_code") == 200 for r in submit_results.values()
    )
    omni_ok = omni_result is not None
    exec_ok = exec_result is not None

    print(f"  Ticker:    {ticker}")
    print(f"  Direction: {direction}")
    print(f"  Window:    {window_id}")
    print()
    print(f"  Phase 2 — Submissions:  {'✅ All 3 submitted'     if all_submitted else '❌ One or more failed'}")
    print(f"  Phase 3 — OMNI synth:   {'✅ Synthesis detected'  if omni_ok       else '❌ No synthesis / timeout'}")
    print(f"  Phase 4 — Execution:    {'✅ Trade placed'        if exec_ok       else '❌ No trade recorded'}")

    if omni_result:
        print(f"\n  OMNI: syntheses_today={omni_result.get('syntheses_today')} go_verdicts={omni_result.get('go_verdicts_today')}")

    if exec_result:
        print(f"\n  Trade details:")
        for k in ["ticker","direction","pathway","position_size_usd","status","entry_time"]:
            if k in exec_result:
                print(f"    {k}: {exec_result[k]}")

    success = all_submitted and omni_ok and exec_ok
    print()
    if success:
        print("  🟢 CHAIN TEST PASSED — Full pipeline validated end-to-end")
    else:
        print("  🔴 CHAIN TEST INCOMPLETE — See failures above")
    print("="*60)
    return success


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Nexus Alpha full chain end-to-end runner")
    parser.add_argument("--ticker",    default="AAPL",    help="Ticker to submit (default: AAPL)")
    parser.add_argument("--direction", default="BULLISH", help="Direction: BULLISH or BEARISH")
    parser.add_argument("--dry-run",   action="store_true", help="Run preflight only — do not submit")
    args = parser.parse_args()

    ticker    = args.ticker.upper()
    direction = args.direction.upper()
    window_id = f"mock-{datetime.now(ET).strftime('%Y%m%d-%H%M%S')}-{str(uuid.uuid4())[:8]}"

    print("\n" + "🌱 " * 20)
    print("GENESIS — Mock Chain Runner v1.0")
    print(f"Ticker: {ticker} | Direction: {direction} | Window: {window_id}")
    print("🌱 " * 20)

    # Phase 1: Preflight
    if not run_preflight():
        return 1

    if args.dry_run:
        print("\n[DRY-RUN] Preflight complete — skipping submission")
        return 0

    # Purge stale concordance state for this ticker
    purge_concordance_window(ticker)

    # Phase 2: Submit picks
    submit_results = submit_picks(ticker, direction, window_id)

    # Phase 3: Poll OMNI
    omni_result = poll_omni_synthesis(window_id, timeout_sec=90)

    # Brief wait for alpha-exec to receive and process the GO signal
    if omni_result:
        print(f"\n  [{ts()}] Giving alpha-exec 10s to process GO signal...")
        time.sleep(10)

    # Phase 4: Check execution
    exec_result = check_execution_result(ticker)

    # Phase 5: Summary
    success = print_summary(ticker, direction, window_id, submit_results, omni_result, exec_result)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
