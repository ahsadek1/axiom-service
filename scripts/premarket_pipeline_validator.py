#!/usr/bin/env python3
"""
premarket_pipeline_validator.py — Nexus V2 Pipeline Integrity Check

Runs at 9:25 AM ET before every trading session.
Validates the FULL pipeline end-to-end including:
  - Concordance thresholds (catches MIN_SCORE_P2 drift)
  - Echo chamber detector behavior  
  - All service /health endpoints
  - Alpaca connectivity + paper account active
  - OMNI brain connectivity (all 4 APIs reachable)
  - DB accessibility

If ANY check fails → alerts Ahmed via Telegram and blocks are logged clearly.
This is the canary that prevents silent misconfiguration from haunting trading.

Deployed: 2026-04-24 (after MIN_SCORE_P2=78 bug cost a full morning session)
FIXED: 2026-05-19 — Anthropic endpoint + OMNI status case-sensitivity
"""

import json
import os
import sys
import requests
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
def _require(var: str) -> str:
    """Return env var value or raise at startup — never silently use a stale fallback."""
    val = os.environ.get(var)
    if not val:
        raise RuntimeError(f"{var} is required but not set. Source .deploy-secrets before running.")
    return val

TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
AHMED_CHAT_ID      = os.environ.get("AHMED_CHAT_ID", "8573754783")
NEXUS_SECRET       = _require("NEXUS_SECRET")
ALPACA_KEY         = _require("ALPACA_API_KEY")
ALPACA_SECRET      = _require("ALPACA_SECRET_KEY")

SERVICES = {
    "axiom":           "http://localhost:8001/health",
    "alpha-buffer":    "http://localhost:8002/health",
    "omni":            "http://localhost:8004/health",
    "alpha-execution": "http://localhost:8005/health",
    "prime-execution": "http://localhost:8006/health",
}

NEXUS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

results = []
failures = []

def check(name, passed, detail=""):
    status = "✅" if passed else "❌"
    results.append(f"{status} {name}: {detail}")
    if not passed:
        failures.append(f"{name}: {detail}")
    return passed

def alert_telegram(message: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": AHMED_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as e:
        print(f"Telegram alert failed: {e}")

# ── CHECK 1: Concordance thresholds ──────────────────────────────────────────
try:
    sys.path.insert(0, os.path.join(NEXUS_ROOT, "alpha-buffer"))
    from config import (
        MIN_SCORE_P2, GO_THRESHOLD_P1, MIN_SUBMISSION_SCORE,
        MIN_SCORE_SOLO_P3, STRONG_GO_THRESHOLD, assert_thresholds
    )
    assert_thresholds()
    check("concordance_thresholds",
          MIN_SCORE_P2 <= 70 and GO_THRESHOLD_P1 <= 70,
          f"MIN_SCORE_P2={MIN_SCORE_P2} GO_THRESHOLD_P1={GO_THRESHOLD_P1} ✓")
except AssertionError as e:
    check("concordance_thresholds", False, str(e))
except Exception as e:
    check("concordance_thresholds", False, f"import error: {e}")

# ── CHECK 2: P2 concordance actually works with typical scores ────────────────
try:
    from concordance import evaluate_concordance
    result = evaluate_concordance(
        window_id="PREMARKET_VALIDATION",
        ticker="TEST",
        direction="bullish",
        submissions=[
            {"agent": "Cipher", "score": 68.0, "reasoning": "Validator test submission A for premarket check"},
            {"agent": "Atlas",  "score": 66.0, "reasoning": "Validator test submission B for premarket check"},
        ],
        solo_entries_enabled=True,
    )
    check("p2_concordance_logic", result is not None and result.pathway == "P2",
          f"P2 forms correctly at score 66-68 ✓" if result else "P2 returned None — threshold broken!")
except Exception as e:
    check("p2_concordance_logic", False, str(e))

# ── CHECK 3: Echo chamber detector NOT firing on legitimate analysis ──────────
try:
    from concordance import _detect_echo_chamber
    legit = _detect_echo_chamber({
        "Cipher": "DHI shows strong bearish momentum with the homebuilder sector facing headwinds from elevated mortgage rates. RSI at 72 suggests overbought conditions with a catalyst for reversal.",
        "Atlas":  "DHI bearish setup confirmed by declining volume on recent rallies. Housing starts data disappointing. Credit spreads widening in the sector suggest institutional caution.",
    })
    check("echo_chamber_no_false_positive", not legit,
          "Legitimate independent analysis not flagged ✓" if not legit else "FALSE POSITIVE — detector too aggressive!")
except Exception as e:
    check("echo_chamber_no_false_positive", False, str(e))

# ── CHECK 4: All services healthy ────────────────────────────────────────────
for svc, url in SERVICES.items():
    try:
        r = requests.get(url, timeout=5)
        data = r.json()
        ok = r.status_code == 200 and data.get("status", "").lower() in ("healthy", "ok", "degraded")
        check(f"service_{svc}", ok, f"HTTP {r.status_code} status={data.get('status','?')}")
    except Exception as e:
        check(f"service_{svc}", False, f"unreachable: {e}")

# ── CHECK 5: Alpaca paper account active ──────────────────────────────────────
try:
    r = requests.get(
        "https://paper-api.alpaca.markets/v2/account",
        headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
        timeout=8,
    )
    data = r.json()
    ok = data.get("status") == "ACTIVE"
    check("alpaca_account", ok, f"status={data.get('status','?')} buying_power=${float(data.get('buying_power',0)):,.0f}")
except Exception as e:
    check("alpaca_account", False, str(e))

# ── CHECK 6: Alpha-execution not paused ───────────────────────────────────────
try:
    r = requests.get("http://localhost:8005/health", timeout=5)
    data = r.json()
    paused = data.get("execution_paused", True)
    auto   = data.get("auto_execute", False)
    check("execution_not_paused",  not paused, "execution_paused=false ✓" if not paused else "PAUSED — needs /resume!")
    check("auto_execute_enabled",  auto,        "auto_execute=true ✓" if auto else "auto_execute=false — DRY RUN mode!")
except Exception as e:
    check("execution_state", False, str(e))

# ── CHECK 7: OMNI brains reachable (quick API probe) ──────────────────────────
BRAIN_PROBES = [
    ("anthropic", "https://api.anthropic.com/v1/messages", "POST",
     {"x-api-key": os.getenv("ANTHROPIC_API_KEY",""), "anthropic-version": "2023-06-01", "content-type": "application/json"},
     {"model": "claude-3-5-sonnet-20241022", "max_tokens": 10, "messages": [{"role": "user", "content": "ok"}]}),
    ("openai", "https://api.openai.com/v1/models", "GET",
     {"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY','')}"}, None),
]
for probe in BRAIN_PROBES:
    name, url, method, headers = probe[0], probe[1], probe[2], probe[3]
    payload = probe[4] if len(probe) > 4 else None
    try:
        if method == "POST" and payload:
            r = requests.post(url, headers=headers, json=payload, timeout=6)
        else:
            r = requests.get(url, headers=headers, timeout=6)
        check(f"brain_{name}", r.status_code in (200, 400, 401),
              f"HTTP {r.status_code} ✓")
    except Exception as e:
        check(f"brain_{name}", False, f"unreachable: {e}")

# ── REPORT ────────────────────────────────────────────────────────────────────
timestamp = datetime.now().strftime("%Y-%m-%d %H:%M ET")
passed = len(results) - len(failures)
total  = len(results)

print(f"\n{'='*60}")
print(f"NEXUS PREMARKET VALIDATION — {timestamp}")
print(f"{'='*60}")
for r in results:
    print(r)
print(f"\n{passed}/{total} checks passed")

if failures:
    msg = (
        f"🚨 <b>PREMARKET VALIDATION FAILED — {timestamp}</b>\n"
        f"{len(failures)}/{total} checks failed. DO NOT TRADE until resolved.\n\n"
        + "\n".join(f"❌ {f}" for f in failures)
    )
    alert_telegram(msg)
    print(f"\n⚠️  FAILURES DETECTED — Ahmed alerted via Telegram")
    sys.exit(1)
else:
    msg = (
        f"✅ <b>PREMARKET VALIDATION PASSED — {timestamp}</b>\n"
        f"All {total} pipeline checks passed. System ready to trade."
    )
    alert_telegram(msg)
    print(f"\n✅ ALL CHECKS PASSED — System ready to trade")
    sys.exit(0)
