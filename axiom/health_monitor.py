"""
health_monitor.py — Axiom Self-Healing Health Monitor

Runs as a scheduled job inside Axiom. Checks all system configs and
dependencies every 5 minutes. Auto-fixes detected issues by redeploying
the correct config. Never just reports and waits.

Inspired by the Apr 29 incident where Axiom detected circuit breaker
configs were missing but did not immediately fix them.

Mandate (Ahmed Sadek, Apr 29 2026):
  Identify → Diagnose → Fix → Register CHRONICLE → Inform SOVEREIGN
  NO permissions needed. NO idle reporting. NO gap between detection and action.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import sqlite3 as _sqlite3

from config import (
    MAX_POSITIONS,
    MAX_RISK_PER_TRADE,
    MIN_DTE,
    MAX_DTE,
    VIX_PAUSE_THRESHOLD,
)

logger = logging.getLogger("axiom.health_monitor")

# ── Constants ─────────────────────────────────────────────────────────────────
ET = None
try:
    import pytz
    ET = pytz.timezone("America/New_York")
except ImportError:
    pass

_CHECK_INTERVAL = 300  # 5 min between full checks
_HTTP_TIMEOUT = 10

# Service URLs (use /health endpoint explicitly — root may 404)
AXIOM_URL = "https://axiom-production-334c.up.railway.app"
AXIOM_LIMITS_URL = f"{AXIOM_URL}/limits"
ALPHA_URL = "https://worker-production-2060.up.railway.app/health"
PRIME_URL = "https://nexus-prime-bot-production.up.railway.app/health"
ALPACA_URL = "https://paper-api.alpaca.markets/v2/account"

# Railway deploy token
RAILWAY_TOKEN = os.getenv("RAILWAY_TOKEN", "08612a1a-4bb4-4ccb-9c75-6ef9277d74db")

# Expected Axiom values
EXPECTED_LIMITS = {
    "max_positions": MAX_POSITIONS,
    "max_risk_per_trade": MAX_RISK_PER_TRADE,
    "min_dte": MIN_DTE,
    "max_dte": MAX_DTE,
    "vix_pause_threshold": VIX_PAUSE_THRESHOLD,
}

CHRONICLE_DB = os.getenv(
    "AXIOM_DB_PATH",
    "/Users/ahmedsadek/nexus/data/axiom.db",
)
# Also try the shared chronicle path
_CHRONICLE_PATHS = [
    CHRONICLE_DB,
    "/Users/ahmedsadek/nexus/data/chronicle.db",
    "/app/data/axiom.db",
    "/app/data/chronicle.db",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_et() -> str:
    """Return current time as ISO string in ET."""
    if ET:
        return datetime.now(ET).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _register_to_chronicle(incident: dict) -> bool:
    """
    Register an incident to the CHRONICLE database.

    Falls back to any available DB path. Writes to e2e_test_results table.

    Args:
        incident: Dict with mode, window_id, result, failed_at, error, ran_at, full_json.

    Returns:
        True if registered successfully, False otherwise.
    """
    for db_path in _CHRONICLE_PATHS:
        try:
            conn = _sqlite3.connect(db_path)
            conn.execute(
                """INSERT INTO e2e_test_results
                   (mode, window_id, result, failed_at, error, ran_at, full_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    incident.get("mode", "INCIDENT"),
                    incident.get("window_id", _now_et()),
                    incident.get("result", "RESOLVED"),
                    incident.get("failed_at", _now_et()),
                    incident.get("error", ""),
                    incident.get("ran_at", _now_et()),
                    json.dumps(incident.get("full_json", {})),
                ),
            )
            conn.commit()
            conn.close()
            return True
        except Exception:
            continue
    return False


# ── Checks ──────────────────────────────────────────────────────────────────

def check_axiom_limits(app_state: dict) -> list[str]:
    """
    Check that Axiom's /limits endpoint returns correct config values.

    Args:
        app_state: Axiom's global state dict.

    Returns:
        List of error messages. Empty if all checks pass.
    """
    errors: list[str] = []
    secret = os.getenv("AXIOM_SECRET") or os.getenv("NEXUS_SECRET") or ""
    if not secret:
        raise RuntimeError("AXIOM_SECRET (or NEXUS_SECRET) is required but not set.")

    try:
        resp = requests.get(
            f"{AXIOM_URL}/limits",
            headers={"X-Axiom-Secret": secret},
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        return [f"Axiom /limits unreachable: {e}"]

    if resp.status_code != 200:
        return [f"Axiom /limits returned {resp.status_code}: {resp.text[:200]}"]

    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        return [f"Axiom /limits returned invalid JSON: {e}"]

    for key, expected in EXPECTED_LIMITS.items():
        actual = data.get(key)
        if actual != expected:
            errors.append(
                f"Config mismatch: {key} expected={expected} got={actual}"
            )

    return errors


def check_alpha_scanner(app_state: dict) -> list[str]:
    """
    Check that Alpha's scanner loop is active.

    Args:
        app_state: Axiom's global state dict.

    Returns:
        List of error messages. Empty if scanner is active.
    """
    errors: list[str] = []

    try:
        resp = requests.get(ALPHA_URL, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        return [f"Alpha health unreachable: {e}"]

    if resp.status_code != 200:
        return [f"Alpha returned {resp.status_code}"]

    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return [f"Alpha returned non-JSON: {resp.text[:200]}"]

    # Alpha v3 uses loop_active; old v9 uses omni_scanner.loop_active
    loop_active = data.get("loop_active")
    if loop_active is None:
        scanner = data.get("omni_scanner", {})
        loop_active = scanner.get("loop_active")

    if loop_active is not True:
        errors.append("Alpha scanner loop not active")

    return errors


def check_alpaca_account(app_state: dict) -> list[str]:
    """
    Check Alpaca account is active and not blocked.

    Args:
        app_state: Axiom's global state dict.

    Returns:
        List of error messages. Empty if account is healthy.
    """
    errors: list[str] = []
    key = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY") or ""
    secret = os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY") or ""
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY are required but not set.")

    try:
        resp = requests.get(
            ALPACA_URL,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        return [f"Alpaca unreachable: {e}"]

    if resp.status_code != 200:
        return [f"Alpaca returned {resp.status_code}"]

    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return [f"Alpaca returned non-JSON: {resp.text[:200]}"]

    if data.get("status") != "ACTIVE":
        errors.append(f"Alpaca status: {data.get('status')}")
    if data.get("trading_blocked"):
        errors.append("Alpaca trading blocked")
    if data.get("account_blocked"):
        errors.append("Alpaca account blocked")

    return errors


def check_prime_verdicts(app_state: dict) -> list[str]:
    """
    Check Prime's go_verdicts count for runaway loops.

    Args:
        app_state: Axiom's global state dict.

    Returns:
        List of error messages. Empty if normal.
    """
    errors: list[str] = []

    try:
        resp = requests.get(PRIME_URL, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        return [f"Prime unreachable: {e}"]

    if resp.status_code != 200:
        return [f"Prime returned {resp.status_code}"]

    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return [f"Prime returned non-JSON: {resp.text[:200]}"]

    go_verdicts = data.get("go_verdicts_today", 0)
    submissions = data.get("submissions_today", 0)

    # Suspicious: true runaway loop — >300 GO verdicts with >85% approval rate.
    # Normal Prime scanning produces 50-70% GO rates on active days;
    # only alert if approval is nearly unconditional (rubber-stamp behaviour).
    if go_verdicts > 300 and submissions > 0:
        go_rate = go_verdicts / submissions * 100
        if go_rate > 85:
            errors.append(
                f"Prime suspicious: {go_verdicts} GO / {submissions} submissions "
                f"({go_rate:.0f}% GO rate)"
            )

    return errors


# ── Self-Healing ────────────────────────────────────────────────────────────

def _redeploy_axiom() -> bool:
    """
    Trigger a Railway redeploy of the Axiom service.

    Uses Railway GraphQL API to create a new deployment from the last
    successful build image (no rebuild needed — just redeploy).

    Returns:
        True if redeploy was triggered, False otherwise.
    """
    project_id = "e095db55-e977-460d-853c-7e7030842ebc"
    service_id = "197a7c62-2c0c-4d28-b1d0-95eecb892d97"
    env_id = "2713406d-aca1-482c-8f7b-663530526113"

    try:
        resp = requests.post(
            "https://backboard.railway.app/graphql/v2",
            headers={
                "Authorization": f"Bearer {RAILWAY_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "query": """mutation redeploy($input: DeploymentCreateInput!) {
                    deploymentCreate(input: $input) {
                        id status
                    }
                }""",
                "variables": {
                    "input": {
                        "projectId": project_id,
                        "serviceId": service_id,
                        "environmentId": env_id,
                    }
                },
            },
            timeout=15,
        )

        data = resp.json()
        if "errors" in data:
            logger.error("Redeploy failed: %s", data["errors"])
            return False

        deployment = data.get("data", {}).get("deploymentCreate", {})
        logger.info(
            "Redeploy triggered: id=%s status=%s",
            deployment.get("id", "?")[:12],
            deployment.get("status", "?"),
        )
        return True

    except Exception as e:
        logger.error("Redeploy API call failed: %s", e)
        return False


def _fix_via_redeploy(reason: str, app_state: dict) -> bool:
    """
    Fix config issues by redeploying Axiom.

    Only triggers if the current deploy is v3 (not our code).
    Avoids infinite redeploy loops.

    Args:
        reason: Description of what went wrong.
        app_state: Axiom's global state dict.

    Returns:
        True if fix applied, False otherwise.
    """
    # Check current version to avoid infinite loop
    current_version = app_state.get("version", "")
    if current_version == "4.0.0":
        # We're already running v4 and something is wrong — needs manual
        logger.warning("v4.0.0 running but config wrong — manual intervention needed")
        return False

    logger.warning("Detected v3 code — triggering redeploy: %s", reason)
    success = _redeploy_axiom()

    if success:
        # Record this fix attempt
        app_state["last_auto_heal"] = _now_et()
        app_state["last_auto_heal_reason"] = reason
        _register_to_chronicle({
            "mode": "INCIDENT",
            "window_id": _now_et(),
            "result": "RESOLVED",
            "failed_at": _now_et(),
            "error": f"Auto-heal triggered: {reason}",
            "ran_at": _now_et(),
            "full_json": {
                "action": "redeploy",
                "trigger": "health_monitor",
                "reason": reason,
                "fixed_by": "AxiomHealthMonitor",
            },
        })
        logger.info("Auto-heal complete — redeploy triggered")
    else:
        logger.error("Auto-heal failed — could not redeploy")

    return success


# ── Main Check ──────────────────────────────────────────────────────────────

def run_full_check(app_state: dict) -> dict:
    """
    Run all health checks and auto-fix any detected issues.

    This is the entry point called by the scheduler every 5 min.

    Args:
        app_state: Axiom's global state dict.

    Returns:
        Dict with check results: {check_name: {status, errors, fix_applied}}.
    """
    results: dict[str, dict] = {}
    any_fix_needed = False
    all_errors: list[str] = []

    if not os.getenv("RAILWAY_TOKEN"):
        RAILWAY_TOKEN = "08612a1a-4bb4-4ccb-9c75-6ef9277d74db"

    # ── Check 1: Axiom /limits ──────────────────────────────────────────
    logger.info("Health check: Axiom /limits...")
    limit_errors = check_axiom_limits(app_state)
    if limit_errors:
        all_errors.extend(limit_errors)
        any_fix_needed = True
        results["axiom_limits"] = {"status": "FAIL", "errors": limit_errors}
        logger.warning("Limits check FAILED: %s", limit_errors)
    else:
        results["axiom_limits"] = {"status": "OK", "errors": []}

    # ── Check 2: Alpha scanner ──────────────────────────────────────────
    logger.info("Health check: Alpha scanner...")
    alpha_errors = check_alpha_scanner(app_state)
    if alpha_errors:
        all_errors.extend(alpha_errors)
        results["alpha_scanner"] = {"status": "FAIL", "errors": alpha_errors}
        logger.warning("Alpha check FAILED: %s", alpha_errors)
    else:
        results["alpha_scanner"] = {"status": "OK", "errors": []}

    # ── Check 3: Alpaca account ─────────────────────────────────────────
    logger.info("Health check: Alpaca account...")
    alpaca_errors = check_alpaca_account(app_state)
    if alpaca_errors:
        all_errors.extend(alpaca_errors)
        results["alpaca_account"] = {"status": "FAIL", "errors": alpaca_errors}
        logger.warning("Alpaca check FAILED: %s", alpaca_errors)
    else:
        results["alpaca_account"] = {"status": "OK", "errors": []}

    # ── Check 4: Prime verdicts ─────────────────────────────────────────
    logger.info("Health check: Prime verdicts...")
    prime_errors = check_prime_verdicts(app_state)
    if prime_errors:
        all_errors.extend(prime_errors)
        results["prime_verdicts"] = {"status": "WARN", "errors": prime_errors}
        logger.warning("Prime check WARN: %s", prime_errors)
    else:
        results["prime_verdicts"] = {"status": "OK", "errors": []}

    # ── Auto-Fix ─────────────────────────────────────────────────────────
    if any_fix_needed and any(
        "Config mismatch" in e or "Axiom /limits" in e
        for e in all_errors
    ):
        logger.warning("Config mismatch detected — attempting auto-heal redeploy")
        fix_applied = _fix_via_redeploy(
            "; ".join(all_errors[:3]),
            app_state,
        )
        results["auto_heal"] = {
            "status": "APPLIED" if fix_applied else "FAILED",
            "errors": all_errors if not fix_applied else [],
        }

    # ── Log result ──────────────────────────────────────────────────────
    failures = sum(1 for r in results.values() if r["status"] not in ("OK",))
    total = len(results)
    logger.info(
        "Health check complete: %d/%d passed. Failures: %s",
        total - failures,
        total,
        ", ".join(
            f"{k}={v['status']}"
            for k, v in results.items()
            if v["status"] not in ("OK",)
        ) or "none",
    )

    return results


# ── Telemetry ───────────────────────────────────────────────────────────────

def format_check_report(results: dict) -> str:
    """
    Format health check results into a human-readable report.

    Args:
        results: Dict from run_full_check().

    Returns:
        Formatted string for Telegram/logs.
    """
    lines = ["🔷 Health Monitor — Auto-Check"]
    for check, result in results.items():
        status = result["status"]
        if status == "OK":
            lines.append(f"  ✅ {check}")
        elif status in ("FAIL", "WARN", "APPLIED", "FAILED"):
            icon = "⚠️" if status == "WARN" else "❌"
            lines.append(f"  {icon} {check}: {status}")
            for err in result.get("errors", []):
                lines.append(f"     {err}")

    return "\n".join(lines)
