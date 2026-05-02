#!/usr/bin/env python3
"""
morning_diagnostic.py — 7 AM Pre-Market Full System Diagnostic

Checks all 9 V2 services, runs integration tests, validates credentials.
Sends Telegram report to Ahmed by 7:45 AM.

Exit codes:
    0 — All systems GO
    1 — One or more critical failures — manual review required before 9:31 AM

Usage:
    python3 morning_diagnostic.py
"""

import os
import sys
import json
import subprocess
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "../alpha-execution/.env"))
load_dotenv(os.path.join(os.path.dirname(__file__), "../axiom/.env"), override=False)

def _require(var: str) -> str:
    """Return env var value or raise at startup — never silently use a stale fallback."""
    val = os.environ.get(var)
    if not val:
        raise RuntimeError(f"{var} is required but not set. Source .deploy-secrets before running.")
    return val

TELEGRAM_TOKEN = _require("TELEGRAM_BOT_TOKEN")
AHMED_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "8573754783")
NEXUS_SECRET   = _require("NEXUS_SECRET")
ORACLE_SECRET  = _require("ORACLE_SECRET")


def _tg(text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": AHMED_CHAT_ID, "text": text},
            timeout=8,
        )
    except Exception:
        pass


def check_service(name: str, port: int, path: str = "/health",
                  headers: dict = None, timeout: int = 5) -> tuple[bool, str]:
    """Check a service health endpoint. Returns (ok, detail)."""
    try:
        resp = requests.get(
            f"http://localhost:{port}{path}",
            headers=headers or {},
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            exec_valid = data.get("execution_valid")
            detail = f"healthy"
            if exec_valid is not None:
                detail += f" | execution_valid={exec_valid}"
            return True, detail
        return False, f"HTTP {resp.status_code}"
    except requests.exceptions.ConnectionError:
        return False, "CONNECTION REFUSED"
    except requests.exceptions.Timeout:
        return False, f"TIMEOUT >{timeout}s"
    except Exception as e:
        return False, str(e)[:80]


def check_alpaca() -> tuple[bool, str]:
    """Check Alpaca V2 paper account directly."""
    try:
        key    = os.environ.get("ALPACA_API_KEY", "PKGGXWNZTITUTZUNVK2QBLZJQL")
        secret = os.environ.get("ALPACA_SECRET_KEY", "DWwCxfRpv92ZLkGX8Ao4mDsAATafLwcuw8EeFAM7s89i")
        resp = requests.get(
            "https://paper-api.alpaca.markets/v2/account",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            equity = float(data.get("equity", 0))
            return True, f"ACTIVE | equity=${equity:,.0f}"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)[:80]


def run_integration_tests() -> tuple[bool, str]:
    """Run Axiom→agent integration tests."""
    try:
        result = subprocess.run(
            ["/Users/ahmedsadek/nexus/axiom/.venv/bin/pytest",
             "tests/test_axiom_agent_integration.py", "-q", "--tb=no"],
            cwd="/Users/ahmedsadek/nexus/axiom",
            capture_output=True, text=True, timeout=60,
        )
        passed = "failed" not in result.stdout and result.returncode == 0
        lines = result.stdout.strip().split("\n")
        summary = lines[-1] if lines else "no output"
        return passed, summary
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT after 60s"
    except Exception as e:
        return False, str(e)[:80]


def main() -> int:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    print(f"=== V2 MORNING DIAGNOSTIC === {now_str}")

    results = {}

    # Core services
    services = [
        ("Axiom",           8001, "/health",  {}),
        ("Alpha Buffer",    8002, "/health",  {}),
        ("Prime Buffer",    8003, "/health",  {}),
        ("OMNI",            8004, "/health",  {}),
        ("Alpha Execution", 8005, "/health",  {}),
        ("Prime Execution", 8006, "/health",  {}),
        ("ORACLE",          8007, "/health",  {}),
        ("Cipher",          9001, "/health",  {}),
        ("Atlas",           9002, "/health",  {}),
        ("Sage",            9003, "/health",  {}),
    ]
    for name, port, path, hdrs in services:
        ok, detail = check_service(name, port, path, hdrs)
        results[name] = (ok, detail)
        icon = "✅" if ok else "❌"
        print(f"{icon} {name}: {detail}")

    # Alpaca
    alpaca_ok, alpaca_detail = check_alpaca()
    results["Alpaca V2"] = (alpaca_ok, alpaca_detail)
    print(f"{'✅' if alpaca_ok else '❌'} Alpaca V2: {alpaca_detail}")

    # Integration tests
    print("Running integration tests...")
    tests_ok, tests_detail = run_integration_tests()
    results["Integration Tests"] = (tests_ok, tests_detail)
    print(f"{'✅' if tests_ok else '❌'} Integration Tests: {tests_detail}")

    # Summary
    failures = [(k, v[1]) for k, v in results.items() if not v[0]]
    all_ok = len(failures) == 0

    critical = [f for f in failures if f[0] in (
        "Alpha Execution", "Prime Execution", "OMNI", "Alpaca V2",
        "Cipher", "Atlas", "Sage", "Integration Tests"
    )]
    tier1_fail = len(critical) > 0

    lines = [f"{'✅' if all_ok else '⚠️'} 7AM DIAGNOSTIC — {'ALL CLEAR' if all_ok else 'ISSUES FOUND'}", f"{now_str}", "━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for name, (ok, detail) in results.items():
        lines.append(f"{'✅' if ok else '❌'} {name}: {detail}")

    if failures:
        lines.append("\n🔴 FAILURES:")
        for name, detail in failures:
            lines.append(f"  {name}: {detail}")

    if tier1_fail:
        lines.append("\n🚨 TIER 1 FAILURES — DO NOT TRADE TODAY")
        lines.append("Manual intervention required before 9:31 AM")
    elif failures:
        lines.append("\n⚠️ Non-critical issues — monitor during session")
    else:
        lines.append("\n✅ ALL SYSTEMS GO — V2 cleared for 9:31 AM")

    report = "\n".join(lines)
    print("\n" + report)
    _tg(report)
    return 0 if not tier1_fail else 1


if __name__ == "__main__":
    sys.exit(main())
