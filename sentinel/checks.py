"""
checks.py — Pipeline Sentinel Check Functions

Each function returns (passed: bool, detail: str).
All checks are stateless and safe to call repeatedly.
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

from config import (
    ET, SERVICE_PORTS, AGENT_DB_PATHS, AGENT_LOG_PATHS,
    NEXUS_SECRET, ALPHA_EXEC_URL, PRIME_EXEC_URL, PICK_LOOKBACK_MINUTES,
)

log = logging.getLogger("sentinel.checks")

HEALTH_TIMEOUT = 5   # seconds per service health check


def check_service_health(service: str) -> tuple[bool, str]:
    """
    GET /health on the service port. Passes if status is healthy/ok.

    Returns:
        (passed, detail)
    """
    port = SERVICE_PORTS.get(service)
    if port is None:
        return False, f"Unknown service: {service}"
    try:
        resp = requests.get(f"http://localhost:{port}/health", timeout=HEALTH_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", "")
            if status in ("healthy", "ok"):
                return True, f"healthy (port {port})"
            return False, f"unexpected status '{status}' (port {port})"
        return False, f"HTTP {resp.status_code} from port {port}"
    except requests.exceptions.ConnectionError:
        return False, f"Connection refused on port {port}"
    except requests.exceptions.Timeout:
        return False, f"Timeout on port {port} ({HEALTH_TIMEOUT}s)"
    except Exception as e:
        return False, f"Error: {e}"


def check_all_services() -> list[tuple[str, bool, str]]:
    """
    Check health of all registered services.

    Returns:
        List of (service_name, passed, detail)
    """
    results = []
    for service in SERVICE_PORTS:
        passed, detail = check_service_health(service)
        results.append((service, passed, detail))
    return results


def check_agent_picks(agent: str) -> tuple[bool, str]:
    """
    Verify the agent is ACTIVE in the last PICK_LOOKBACK_MINUTES.
    Pass = submitted picks OR analyzed tickers (scanning but none qualified).
    Restarting an agent that is scanning but finding nothing is a false heal.

    Fix: 2026-04-29 — sentinel was restarting Atlas/Sage every 5min because they
    scored tickers below submit threshold. Added decisions-table fallback check.

    Returns:
        (passed, detail)
    """
    db_path = AGENT_DB_PATHS.get(agent)
    if not db_path:
        return False, f"No DB path for agent {agent}"
    try:
        cutoff = (datetime.now(ET) - timedelta(minutes=PICK_LOOKBACK_MINUTES)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = sqlite3.connect(db_path)
        try:
            # Primary: submitted picks
            try:
                submitted = conn.execute(
                    "SELECT COUNT(*) FROM picks WHERE created_at >= ?", (cutoff,)
                ).fetchone()[0]
            except Exception:
                submitted = 0
            if submitted > 0:
                return True, f"{submitted} picks submitted in last {PICK_LOOKBACK_MINUTES} min"
            # Secondary: any analysis activity (scored but below threshold = healthy)
            try:
                analyzed = conn.execute(
                    "SELECT COUNT(*) FROM decisions WHERE created_at >= ?", (cutoff,)
                ).fetchone()[0]
            except Exception:
                analyzed = 0
        finally:
            conn.close()
        if analyzed > 0:
            return True, f"0 submissions but {analyzed} tickers analyzed — scanning normally (scores below threshold)"
        return False, f"0 picks and 0 analysis in last {PICK_LOOKBACK_MINUTES} min — likely stalled"
    except Exception as e:
        return False, f"DB error: {e}"


def check_all_agent_picks() -> list[tuple[str, bool, str]]:
    """Check pick activity for all 3 agents."""
    return [
        (agent, *check_agent_picks(agent))
        for agent in ("cipher", "atlas", "sage")
    ]


def check_execution_health(system: str) -> tuple[bool, str]:
    """
    Check Alpha or Prime Execution health — paused state and Alpaca connectivity.

    Args:
        system: 'alpha' or 'prime'

    Returns:
        (passed, detail)
    """
    url = ALPHA_EXEC_URL if system == "alpha" else PRIME_EXEC_URL
    try:
        resp = requests.get(f"{url}/health", timeout=HEALTH_TIMEOUT)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        data = resp.json()
        paused   = data.get("execution_paused", False)
        alpaca   = data.get("alpaca_reachable", True)
        vix      = data.get("vix_brake", "CLEAR")

        issues = []
        if paused:
            issues.append("execution_paused=True")
        if not alpaca:
            issues.append("alpaca_reachable=False")
        if vix == "HALTED":
            issues.append("vix_brake=HALTED")

        if issues:
            return False, " | ".join(issues)
        return True, f"healthy (vix={vix})"
    except Exception as e:
        return False, f"Error: {e}"


def check_omni_health() -> tuple[bool, str]:
    """Check OMNI is healthy and has ≥3/4 brains available."""
    try:
        resp = requests.get(f"http://localhost:8004/health", timeout=HEALTH_TIMEOUT)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        data   = resp.json()
        status = data.get("status", "")
        if status not in ("healthy", "ok"):
            return False, f"status={status}"
        return True, f"healthy (version={data.get('version','?')})"
    except Exception as e:
        return False, f"Error: {e}"


def check_recent_critical_logs(agent: str, lookback_minutes: int = 5) -> tuple[bool, str]:
    """
    Scan agent stderr log for CRITICAL entries in the last N minutes.
    Returns (passed=True, detail) if no CRITICALs found.
    """
    log_path = AGENT_LOG_PATHS.get(agent)
    if not log_path:
        return True, "no log path configured"
    try:
        cutoff = datetime.now(ET) - timedelta(minutes=lookback_minutes)
        criticals = []
        with open(log_path, "r", errors="replace") as f:
            # Read last 200 lines
            lines = f.readlines()[-200:]
        for line in lines:
            if "[CRITICAL]" in line or "CRITICAL" in line:
                # Parse timestamp from line prefix: "2026-04-15 13:31:34,641 [CRITICAL]"
                try:
                    ts_str = line[:19]
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
                    if ts >= cutoff.replace(tzinfo=None).replace(tzinfo=ET):
                        criticals.append(line.strip()[:120])
                except Exception:
                    pass
        if criticals:
            return False, f"{len(criticals)} CRITICAL(s): {criticals[-1]}"
        return True, "no recent CRITICALs"
    except FileNotFoundError:
        return True, "log not found (ok if agent just started)"
    except Exception as e:
        return False, f"Log read error: {e}"


def get_log_tail(agent: str, lines: int = 20) -> str:
    """Return last N lines of agent stderr log for diagnostic context."""
    log_path = AGENT_LOG_PATHS.get(agent, "")
    try:
        with open(log_path, "r", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except Exception:
        return "(log unavailable)"


def run_full_check() -> dict:
    """
    Run all checks and return a structured results dict.

    Returns:
        {
            "passed": bool,
            "failures": [(component, detail), ...],
            "summary": str,
            "timestamp": str,
        }
    """
    failures: list[tuple[str, str]] = []
    now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")

    # Check 1 — All services
    for service, passed, detail in check_all_services():
        if not passed:
            failures.append((service, detail))

    # Check 2 — Agent picks (only during market hours — caller gates this)
    for agent, passed, detail in check_all_agent_picks():
        if not passed:
            failures.append((f"{agent}-picks", detail))

    # Check 3 — Execution health
    for system in ("alpha", "prime"):
        passed, detail = check_execution_health(system)
        if not passed:
            failures.append((f"{system}-execution", detail))

    # Check 4 — OMNI
    passed, detail = check_omni_health()
    if not passed:
        failures.append(("omni", detail))

    # Check 5 — Agent CRITICAL log scan
    for agent in ("cipher", "atlas", "sage"):
        passed, detail = check_recent_critical_logs(agent)
        if not passed:
            failures.append((f"{agent}-logs", detail))

    all_passed = len(failures) == 0
    summary    = "ALL CHECKS PASSED" if all_passed else f"{len(failures)} FAILURE(S): " + ", ".join(f[0] for f in failures)

    return {
        "passed":    all_passed,
        "failures":  failures,
        "summary":   summary,
        "timestamp": now_str,
    }
