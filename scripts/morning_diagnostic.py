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
import ast
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


def check_mock_session_mode() -> tuple[bool, str]:
    """CRITICAL: Verify MOCK_SESSION_MODE is not active before market hours.

    If MOCK_SESSION_MODE=true is set, pre-market options orders will bypass the
    market hours gate and fire at 7AM — Alpaca paper cannot fill options pre-market,
    causing confirmation_timeout VOIDs. This is a hard TIER 1 failure.
    """
    mock_val = os.environ.get("MOCK_SESSION_MODE", "").lower()
    if mock_val == "true":
        return False, (
            "MOCK_SESSION_MODE=true — TIER 1 BLOCK. "
            "Pre-market options orders WILL be submitted and WILL void. "
            "Unset this before 9:31 AM or today's pipeline will lose every trade."
        )
    return True, f"MOCK_SESSION_MODE not set ({'clear' if not mock_val else repr(mock_val)})"


def check_exit_monitor_signature() -> tuple[bool, str]:
    """Verify exit_monitor.py's evaluate_exits() accepts ticker_lock_getter kwarg.

    Uses AST parsing (no import/side-effects) to check the function signature.
    A deployment mismatch caused the Prime exit monitor to crash every cycle
    for 47 minutes at open on 2026-05-04. This catches that class of bug early.
    """
    exit_monitor_path = "/Users/ahmedsadek/nexus/prime-execution/exit_monitor.py"
    try:
        with open(exit_monitor_path) as f:
            source = f.read()
        tree = ast.parse(source, filename=exit_monitor_path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "evaluate_exits":
                all_args = (
                    [a.arg for a in node.args.args]
                    + [a.arg for a in node.args.posonlyargs]
                    + [a.arg for a in node.args.kwonlyargs]
                    + ([node.args.vararg.arg] if node.args.vararg else [])
                    + ([node.args.kwarg.arg] if node.args.kwarg else [])
                )
                if "ticker_lock_getter" in all_args:
                    return True, "evaluate_exits() signature OK (ticker_lock_getter present)"
                return False, (
                    "evaluate_exits() missing 'ticker_lock_getter' param — "
                    "deployment mismatch detected: main.py will crash on every exit cycle"
                )
        return False, "evaluate_exits() function not found in exit_monitor.py"
    except FileNotFoundError:
        return False, f"exit_monitor.py not found at {exit_monitor_path}"
    except SyntaxError as e:
        return False, f"exit_monitor.py has syntax error: {e}"
    except Exception as e:
        return False, f"signature check error: {str(e)[:120]}"


def check_thesis_posture() -> tuple[bool, str]:
    """Report THESIS DEFENSIVE sizing posture. Warn (not fail) if sizing=0.

    A sizing_multiplier of 0.0 means THESIS has zeroed all position sizes —
    no trade will execute regardless of pipeline quality. Flag this at 7AM
    so Ahmed can assess whether the gate is appropriate before market open.
    """
    try:
        import sqlite3
        db_path = "/Users/ahmedsadek/nexus/data/chronicle.db"
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT sizing_multiplier, risk_reward_gate, thesis_sentence "
            "FROM thesis_context ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row is None:
            return True, "No THESIS context in CHRONICLE — will use fallback"
        mult = float(row["sizing_multiplier"])
        gate = row["risk_reward_gate"]
        sentence = (row["thesis_sentence"] or "")[:80]
        if mult == 0.0:
            return False, (
                f"⚠️ sizing_multiplier=0.0 (gate={gate}) — THESIS DEFENSIVE is zeroing all "
                f"positions. No trade will execute today unless posture updates. "
                f"Context: {sentence}"
            )
        return True, f"sizing_multiplier={mult:.2f} gate={gate} — OK"
    except Exception as e:
        return True, f"THESIS check skipped: {str(e)[:80]}"  # non-fatal


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

    # --- SAFETY CHECKS (run before anything else) ---
    mock_ok, mock_detail = check_mock_session_mode()
    results["MOCK_SESSION_MODE"] = (mock_ok, mock_detail)
    icon = "✅" if mock_ok else "🚨"
    print(f"{icon} MOCK_SESSION_MODE: {mock_detail}")

    sig_ok, sig_detail = check_exit_monitor_signature()
    results["ExitMonitor Signature"] = (sig_ok, sig_detail)
    icon = "✅" if sig_ok else "🚨"
    print(f"{icon} ExitMonitor Signature: {sig_detail}")

    thesis_ok, thesis_detail = check_thesis_posture()
    results["THESIS Posture"] = (thesis_ok, thesis_detail)
    icon = "✅" if thesis_ok else "⚠️"
    print(f"{icon} THESIS Posture: {thesis_detail}")

    # --- Core services ---
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
        "Cipher", "Atlas", "Sage", "Integration Tests",
        "MOCK_SESSION_MODE", "ExitMonitor Signature",
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
