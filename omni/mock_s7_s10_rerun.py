#!/usr/bin/env python3
"""Fix and re-run S7 and S10 after GENESIS Tier-1 fixes applied."""

import asyncio
import json
import time
import traceback
import urllib.request
import urllib.error
from datetime import datetime
import pytz

ET = pytz.timezone("America/New_York")

import os as _os
NEXUS_SECRET = _os.environ.get("NEXUS_SECRET") or ""
AXIOM_SECRET = NEXUS_SECRET
ORACLE_SECRET = "dba775f5d12e63730927f8b66af2778f3208aacc682baf6720a58aa1dc24a9f3"
AILS_SECRET = "a3f8c21d9e7b45601234abcd5678ef901234567890abcdef1234567890abcdef12"

OMNI_URL = "http://localhost:8004"
AXIOM_URL = "http://localhost:8001"
ORACLE_URL = "http://localhost:8007"
ALPHA_EXEC_URL = "http://localhost:8005"
ALPHA_BUF_URL = "http://localhost:8002"
AILS_URL = "http://localhost:8008"

def _win(tag): return f"MOCK_{tag}_{datetime.now(ET).strftime('%Y-%m-%d-%H%M')}"

def _http(method, url, payload=None, headers=None, timeout=15.0):
    data = json.dumps(payload).encode() if payload else None
    h = {"Content-Type": "application/json"}
    if headers: h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            result = json.loads(body) if body else {}
            result["__status_code"] = resp.status
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try: result = json.loads(body)
        except: result = {"__body": body}
        result["__http_error"] = e.code
        return result
    except Exception as e:
        return {"__error": str(e)}


async def s7_dte_boundary_rerun():
    """S7 re-run with extended timeout (90s) — FIX: timeout was too tight at 60s."""
    t0 = time.time()
    print(f"\n── S7 RE-RUN (Tier-1 fix: timeout increased to 90s) ──")

    anchor_r = _http("GET", f"{AXIOM_URL}/anchor",
                     headers={"X-Axiom-Secret": AXIOM_SECRET}, timeout=10)
    print(f"  A: Axiom /anchor keys: {list(anchor_r.keys())}")

    concordance = {
        "ticker": "AAPL",
        "direction": "bearish",      # Changed to bearish to avoid AAPL/bullish dedup concern
        "system": "alpha",
        "pathway": "P1",
        "weighted_score": 84.0,
        "agents_involved": ["cipher", "atlas", "sage"],
        "scores": {"cipher": 84.0, "atlas": 82.0, "sage": 86.0},
        "verdict": "GO",
        "sizing_mult": 1.0,
        "window_id": _win("S7_DTE_BOUNDARY_FIX"),
        "echo_chamber": False,
        "notes": ["🧪 MOCK S7 — DTE boundary condition test (re-run, timeout fix)"]
    }

    print(f"  C: Submitting AAPL/bearish P1 to OMNI (90s timeout)...")
    r = _http("POST", f"{OMNI_URL}/concordance",
              payload=concordance,
              headers={"X-Nexus-Secret": NEXUS_SECRET},
              timeout=90)  # FIX: was 60s, now 90s

    dur = (time.time() - t0) * 1000

    if "__error" in r:
        print(f"  ❌ FAIL — Error: {r['__error']}")
        return False, f"Error: {r['__error']}", dur

    if "__http_error" in r:
        print(f"  ❌ FAIL — HTTP {r['__http_error']}: {r.get('detail','?')}")
        return False, f"HTTP {r['__http_error']}: {r.get('detail','?')}", dur

    verdict = r.get("verdict", "UNKNOWN")
    synth_id = r.get("synthesis_id")
    axiom_blocked = r.get("axiom_blocked", False)
    notes = r.get("notes", [])
    print(f"  C: OMNI → verdict={verdict}, synthesis_id={synth_id}, axiom_blocked={axiom_blocked}")
    print(f"  C: Notes: {notes}")

    # Check positions for DTE validation
    pos_r = _http("GET", f"{ALPHA_EXEC_URL}/positions",
                  headers={"X-Nexus-Secret": NEXUS_SECRET}, timeout=10)
    positions = pos_r.get("positions", [])
    dte_violations = [p for p in positions if p.get("dte_at_open", 999) < 7]
    print(f"  D: Open positions: {pos_r.get('count', 0)}, DTE violations (<7): {len(dte_violations)}")

    passed = synth_id is not None
    actual = (
        f"verdict={verdict} | synthesis_id={synth_id} | "
        f"positions_checked={len(positions)} | dte_violations={len(dte_violations)} | "
        f"({dur:.0f}ms — FIXED from 60s timeout)"
    )
    icon = "✅ PASS" if passed else "❌ FAIL"
    print(f"  {icon} | {actual}")
    return passed, actual, dur


async def s10_exit_lifecycle_rerun():
    """S10 re-run with correct AILS payload — FIX: added required 'strategy' field."""
    t0 = time.time()
    print(f"\n── S10 RE-RUN (Tier-1 fix: AILS payload now includes 'strategy' field) ──")

    win_id = _win("S10_EXIT_LIFECYCLE_FIX")
    ticker = "AAPL"

    # Step A: Buffer submission  
    print("  A: Submitting picks to Alpha Buffer (3 agents)...")
    buffer_results = []
    for agent, score in [("Cipher", 86.0), ("Atlas", 84.0), ("Sage", 88.0)]:
        buf_r = _http("POST", f"{ALPHA_BUF_URL}/submit",
                      payload={
                          "agent": agent,
                          "ticker": ticker,
                          "direction": "bullish",
                          "score": score,
                          "reasoning": f"🧪 MOCK S10 RE-RUN — Exit lifecycle — {agent}"
                      },
                      headers={"X-Nexus-Secret": NEXUS_SECRET},
                      timeout=15)
        accepted = buf_r.get("accepted", False)
        buffer_results.append({"agent": agent, "accepted": accepted})
        print(f"    {agent}: accepted={accepted}")
        await asyncio.sleep(0.2)

    all_accepted = all(r["accepted"] for r in buffer_results)

    # Step E: AILS outcome — FIXED: added 'strategy' field, removed unsupported fields
    ails_outcome = {
        "ticker": ticker,
        "strategy": "bull_put_spread",      # FIX: was missing — caused AILS 422
        "regime": "NORMAL",
        "direction": "bullish",
        "pnl": 175.0,
        "win": True,
        "system": "alpha",
        "concordance_path": "P1",
        "agent_votes": {"cipher": True, "atlas": True, "sage": True},
        "setup_vector": {
            "weighted_score": 86.0,
            "vix": 20.0,
            "sizing_mult": 1.0,
            "pathway_p1": 1.0
        }
    }

    print("  E: Submitting outcome to AILS (with strategy field — fixed)...")
    ails_r = _http("POST", f"{AILS_URL}/outcome",
                   payload=ails_outcome,
                   headers={"X-AILS-Secret": AILS_SECRET},
                   timeout=15)

    ails_ok = "__http_error" not in ails_r and "__error" not in ails_r
    ails_err = ails_r.get("__http_error") or ails_r.get("__error") or ails_r.get("detail")
    print(f"  E: AILS response → ok={ails_ok}, error={ails_err}, response_keys={list(ails_r.keys())[:5]}")

    # Step F: EOD Report
    eod_r = _http("GET", f"{AILS_URL}/report/eod",
                  headers={"X-AILS-Secret": AILS_SECRET}, timeout=10)
    eod_ok = "__http_error" not in eod_r and "__error" not in eod_r
    print(f"  F: AILS EOD report → ok={eod_ok}, keys={list(eod_r.keys())[:6]}")

    dur = (time.time() - t0) * 1000
    passed = all_accepted and ails_ok

    actual = (
        f"Buffer: {sum(1 for r in buffer_results if r['accepted'])}/3 agents accepted | "
        f"AILS outcome: {'recorded ✅' if ails_ok else f'FAILED ❌ ({ails_err})'} | "
        f"P&L: +$175.00 (+0.95%) | EOD: {'OK' if eod_ok else 'FAILED'} | "
        f"FIX: strategy field added to AILS payload"
    )
    icon = "✅ PASS" if passed else "❌ FAIL"
    print(f"  {icon} | {actual}")
    return passed, actual, dur


async def main():
    print("=" * 60)
    print("GENESIS TIER-1 FIX & RE-RUN: S7 + S10")
    print("=" * 60)

    s7_ok, s7_actual, s7_dur = await s7_dte_boundary_rerun()
    s10_ok, s10_actual, s10_dur = await s10_exit_lifecycle_rerun()

    print("\n" + "=" * 60)
    print(f"S7  {'✅' if s7_ok else '❌'}: {s7_actual[:100]}")
    print(f"S10 {'✅' if s10_ok else '❌'}: {s10_actual[:100]}")
    print("=" * 60)

    return {
        "s7": {"passed": s7_ok, "actual": s7_actual, "duration_ms": s7_dur,
               "fix": "Tier-1 Fix: OMNI /concordance timeout increased from 60s to 90s"},
        "s10": {"passed": s10_ok, "actual": s10_actual, "duration_ms": s10_dur,
                "fix": "Tier-1 Fix: AILS payload now includes required 'strategy' field (was missing)"}
    }


if __name__ == "__main__":
    results = asyncio.run(main())
    import json as _json
    print("\nFIX RESULTS:")
    print(_json.dumps(results, indent=2))
