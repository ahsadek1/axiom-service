#!/usr/bin/env python3
"""
ci_gate.py — Nexus Mechanical CI/CD Gate

The gate that prevents non-compliant code from advancing.
Runs all unit test suites across all active services.
Used as the pre-deploy verification step.

Exit codes:
  0 = ALL PASS — gate clear
  1 = FAILURES — gate blocked
  2 = PARTIAL (some skipped due to missing venvs)

Usage:
  python3 ci_gate.py                   # Run all services
  python3 ci_gate.py --service alpha-execution  # Run single service
  python3 ci_gate.py --fast            # Skip slow/integration tests
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Optional

import pytz
import requests

# ── Config ────────────────────────────────────────────────────────────────────

def _require(var: str) -> str:
    """Return env var value or raise at startup — never silently use a stale fallback."""
    val = os.getenv(var)
    if not val:
        raise RuntimeError(f"{var} is required but not set. Source .deploy-secrets before running.")
    return val

NEXUS_DIR          = "/Users/ahmedsadek/nexus"
TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
AHMED_CHAT_ID      = os.getenv("AHMED_CHAT_ID", "8573754783")
ET                 = pytz.timezone("America/New_York")

# Core Nexus services with active test suites
CORE_SERVICES = [
    "alpha-buffer",
    "alpha-execution",
    "prime-buffer",
    "prime-execution",
    "omni",
    "axiom",
    "oracle",
    "ails",
    "guardian-angel",
]

# Additional services with test suites (run by default)
EXTENDED_SERVICES = [
    "cipher",
    "atlas",
    "sage",
    "shared",
    "pipeline-sentinel",
]

# Services that require special handling (longer timeouts, etc.)
SLOW_SERVICES = ["oracle", "ails"]

# Per-service test timeout in seconds
TEST_TIMEOUT = 120


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(ET).strftime("%H:%M:%S")
    prefix = {"INFO": "  ", "OK": "✅", "FAIL": "❌", "WARN": "⚠️ ", "STEP": "→", "SKIP": "⏭ "}
    print(f"[{ts}] {prefix.get(level, '  ')} {msg}", flush=True)


def send_telegram(msg: str) -> None:
    """Send gate result to Ahmed. Never raises."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": AHMED_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as e:
        log(f"Telegram send failed: {e}", "WARN")


def find_venv_pytest(service_dir: str) -> Optional[str]:
    """Find pytest in service's venv. Returns path or None."""
    candidates = [
        os.path.join(service_dir, ".venv", "bin", "pytest"),
        os.path.join(service_dir, "venv",  "bin", "pytest"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def run_service_tests(
    service: str,
    fast: bool = False,
    timeout: int = TEST_TIMEOUT,
) -> dict:
    """
    Run tests for a single service.

    Returns:
        dict with status, passed, failed, error, elapsed_sec, output
    """
    service_dir = os.path.join(NEXUS_DIR, service)
    tests_dir   = os.path.join(service_dir, "tests")

    if not os.path.isdir(service_dir):
        return {
            "service": service, "status": "SKIP",
            "reason": "Service directory not found",
            "passed": 0, "failed": 0, "elapsed_sec": 0,
        }

    if not os.path.isdir(tests_dir):
        return {
            "service": service, "status": "SKIP",
            "reason": "No tests/ directory",
            "passed": 0, "failed": 0, "elapsed_sec": 0,
        }

    pytest = find_venv_pytest(service_dir)
    if not pytest:
        return {
            "service": service, "status": "SKIP",
            "reason": "No venv pytest found",
            "passed": 0, "failed": 0, "elapsed_sec": 0,
        }

    args = [pytest, "tests/", "-q", "--tb=short", "--no-header"]
    if fast and service in SLOW_SERVICES:
        args.extend(["-m", "not slow"])

    t0 = time.time()
    try:
        result = subprocess.run(
            args,
            cwd=service_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = round(time.time() - t0, 1)
        output = result.stdout + result.stderr

        # Parse pytest summary line
        passed = failed = 0
        for line in output.split("\n"):
            if "passed" in line and ("failed" in line or "passed" in line):
                import re
                m_pass = re.search(r"(\d+) passed", line)
                m_fail = re.search(r"(\d+) failed", line)
                if m_pass:
                    passed = int(m_pass.group(1))
                if m_fail:
                    failed = int(m_fail.group(1))

        status = "PASS" if result.returncode == 0 else "FAIL"
        return {
            "service":     service,
            "status":      status,
            "passed":      passed,
            "failed":      failed,
            "elapsed_sec": elapsed,
            "output":      output[-1000:] if len(output) > 1000 else output,
            "returncode":  result.returncode,
        }

    except subprocess.TimeoutExpired:
        elapsed = round(time.time() - t0, 1)
        return {
            "service":     service,
            "status":      "FAIL",
            "passed":      0,
            "failed":      1,
            "elapsed_sec": elapsed,
            "output":      f"TIMEOUT after {timeout}s",
            "reason":      f"Test suite timed out after {timeout}s",
        }
    except Exception as e:
        elapsed = round(time.time() - t0, 1)
        return {
            "service":     service,
            "status":      "FAIL",
            "passed":      0,
            "failed":      1,
            "elapsed_sec": elapsed,
            "output":      str(e),
            "reason":      f"Unexpected error: {e}",
        }


# ── Main gate ─────────────────────────────────────────────────────────────────

def run_gate(
    services: Optional[list] = None,
    fast: bool = False,
    notify: bool = True,
) -> dict:
    """
    Run the full CI/CD gate.

    Args:
        services:  Specific services to test. None = all core + extended.
        fast:      Skip slow integration tests.
        notify:    Send Telegram notification on completion.

    Returns:
        Gate result dict.
    """
    target_services = services or (CORE_SERVICES + EXTENDED_SERVICES)
    start_time = time.time()
    results = []

    print()
    print("=" * 70)
    print(f"NEXUS CI/CD GATE — {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}")
    print(f"Services: {len(target_services)} | Fast mode: {fast}")
    print("=" * 70)

    for svc in target_services:
        log(f"Testing {svc}...", "STEP")
        result = run_service_tests(svc, fast=fast)
        results.append(result)

        status = result["status"]
        if status == "PASS":
            log(f"  {svc}: {result['passed']} passed ({result['elapsed_sec']}s)", "OK")
        elif status == "SKIP":
            log(f"  {svc}: SKIPPED — {result.get('reason', '?')}", "SKIP")
        else:
            log(f"  {svc}: {result['failed']} FAILED ({result['elapsed_sec']}s)", "FAIL")
            # Print failure details
            output = result.get("output", "")
            for line in output.split("\n")[-20:]:
                if line.strip():
                    print(f"    {line}")

    # Aggregate
    total_elapsed = round(time.time() - start_time, 1)
    passed_svcs   = [r for r in results if r["status"] == "PASS"]
    failed_svcs   = [r for r in results if r["status"] == "FAIL"]
    skipped_svcs  = [r for r in results if r["status"] == "SKIP"]
    total_tests_pass = sum(r.get("passed", 0) for r in results)
    total_tests_fail = sum(r.get("failed", 0) for r in results)

    gate_status = "PASS" if len(failed_svcs) == 0 else "FAIL"
    if gate_status == "PASS" and len(skipped_svcs) > 0:
        gate_status = "PASS_WITH_SKIPS"

    gate_result = {
        "gate_status":   gate_status,
        "total_elapsed": total_elapsed,
        "services_pass": len(passed_svcs),
        "services_fail": len(failed_svcs),
        "services_skip": len(skipped_svcs),
        "tests_pass":    total_tests_pass,
        "tests_fail":    total_tests_fail,
        "results":       results,
        "ran_at":        datetime.now(ET).isoformat(),
    }

    # Print summary
    print()
    print("=" * 70)
    gate_icon = "✅" if gate_status == "PASS" else ("⚠️ " if gate_status == "PASS_WITH_SKIPS" else "❌")
    print(f"{gate_icon} GATE: {gate_status}")
    print(f"   Services: {len(passed_svcs)} pass / {len(failed_svcs)} fail / {len(skipped_svcs)} skip")
    print(f"   Tests:    {total_tests_pass} pass / {total_tests_fail} fail")
    print(f"   Elapsed:  {total_elapsed}s")
    if failed_svcs:
        print(f"   FAILED:   {', '.join(r['service'] for r in failed_svcs)}")
    print("=" * 70)
    print()

    # Write result to CHRONICLE
    try:
        import sqlite3
        conn = sqlite3.connect("/Users/ahmedsadek/nexus/data/chronicle.db")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ci_gate_results (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                gate_status   TEXT,
                services_pass INTEGER,
                services_fail INTEGER,
                services_skip INTEGER,
                tests_pass    INTEGER,
                tests_fail    INTEGER,
                elapsed_sec   REAL,
                ran_at        TEXT,
                full_json     TEXT
            )
        """)
        conn.execute("""
            INSERT INTO ci_gate_results
                (gate_status, services_pass, services_fail, services_skip,
                 tests_pass, tests_fail, elapsed_sec, ran_at, full_json)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            gate_status,
            len(passed_svcs), len(failed_svcs), len(skipped_svcs),
            total_tests_pass, total_tests_fail, total_elapsed,
            gate_result["ran_at"], json.dumps(gate_result),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"CHRONICLE write failed: {e}", "WARN")

    # Send Telegram notification
    if notify:
        icon = "✅" if gate_status.startswith("PASS") else "❌"
        msg_lines = [
            f"{icon} <b>CI/CD GATE — {gate_status}</b>",
            f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')} | {total_elapsed}s",
            f"Services: {len(passed_svcs)}✅ {len(failed_svcs)}❌ {len(skipped_svcs)}⏭",
            f"Tests: {total_tests_pass} pass / {total_tests_fail} fail",
        ]
        if failed_svcs:
            msg_lines.append(f"\nFailed: {', '.join(r['service'] for r in failed_svcs)}")
        send_telegram("\n".join(msg_lines))

    return gate_result


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Nexus CI/CD Gate")
    parser.add_argument("--service",  help="Test a single service only")
    parser.add_argument("--fast",     action="store_true", help="Skip slow integration tests")
    parser.add_argument("--no-notify", action="store_true", help="Skip Telegram notification")
    parser.add_argument("--core-only", action="store_true", help="Run core services only")
    args = parser.parse_args()

    if args.service:
        services = [args.service]
    elif args.core_only:
        services = CORE_SERVICES
    else:
        services = None  # all

    result = run_gate(
        services=services,
        fast=args.fast,
        notify=not args.no_notify,
    )

    status = result["gate_status"]
    sys.exit(0 if status.startswith("PASS") else 1)


if __name__ == "__main__":
    main()
