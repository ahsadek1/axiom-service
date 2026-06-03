"""
genesis/alerts/verification_checkers.py — Specific Verification Logic for Each Alert Type
==========================================================================================

Individual checker functions for each alert type.
Each checker is responsible for:
1. Gathering evidence (API calls, log checks, process status, etc.)
2. Determining success/failure
3. Returning (VerificationResult, evidence_dict, root_cause_string)

All checkers are called asynchronously by alert_verifier.py.

Author: GENESIS 🌱
Date: 2026-05-16
"""

import json
import logging
import os
import subprocess
import sqlite3
import requests
from datetime import datetime, timezone
from typing import Tuple, Dict, Any, Optional
from pathlib import Path

logger = logging.getLogger("genesis.alerts.verification_checkers")

# Import VerificationResult from alert_verifier
from genesis.alerts.alert_verifier import VerificationResult

# ── Configuration ────────────────────────────────────────────────────────────

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-trading-api.alpaca.markets")
CHRONICLE_DB = os.getenv(
    "NEXUS_CHRONICLE_DB",
    "/Users/ahmedsadek/nexus/data/chronicle.db",
)
NEXUS_ROOT = os.getenv("NEXUS_ROOT", "/Users/ahmedsadek/nexus")

# ── Alpaca API Helpers ───────────────────────────────────────────────────────

def _get_alpaca_headers() -> Dict[str, str]:
    """Get headers for Alpaca API calls."""
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-SECRET-KEY": ALPACA_SECRET_KEY,
    }


def _alpaca_get_positions() -> Optional[list]:
    """
    Get list of open positions from Alpaca.

    Returns:
        List of position dicts, or None on API error
    """
    try:
        resp = requests.get(
            f"{ALPACA_BASE_URL}/v2/positions",
            headers=_get_alpaca_headers(),
            timeout=5,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("verification_checkers: Alpaca /positions API failed: %s", e)
        return None


# ── Process/Log Helpers ──────────────────────────────────────────────────────

def _get_process_status(agent_name: str) -> Tuple[bool, Optional[int]]:
    """
    Check if a process is running.

    Args:
        agent_name: Name of the agent (e.g., "alpha-execution")

    Returns:
        Tuple (is_running, pid)
    """
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.split("\n"):
            if agent_name in line and "python" in line:
                parts = line.split()
                if len(parts) > 1:
                    try:
                        pid = int(parts[1])
                        return (True, pid)
                    except ValueError:
                        pass
        return (False, None)
    except Exception as e:
        logger.error("verification_checkers: Process check failed: %s", e)
        return (False, None)


def _get_recent_log_lines(log_file: str, lines: int = 50) -> list:
    """
    Get recent lines from a log file.

    Args:
        log_file: Path to log file
        lines: Number of lines to retrieve

    Returns:
        List of log lines (most recent first)
    """
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), log_file],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.split("\n") if result.returncode == 0 else []
    except Exception as e:
        logger.error("verification_checkers: Log read failed: %s", e)
        return []


def _find_recent_heartbeat(log_file: str, max_age_seconds: int = 30) -> Tuple[bool, Optional[str]]:
    """
    Check if a recent heartbeat/alive message exists in a log.

    Args:
        log_file: Path to log file
        max_age_seconds: Maximum age of heartbeat to consider (default 30s)

    Returns:
        Tuple (has_recent_heartbeat, last_heartbeat_line)
    """
    lines = _get_recent_log_lines(log_file, lines=100)
    now = datetime.now(timezone.utc)

    for line in lines:
        if any(kw in line.lower() for kw in ["heartbeat", "alive", "tick", "pulse"]):
            # Try to extract timestamp from line
            try:
                # Simple heuristic: assume ISO format timestamp at start
                parts = line.split()
                if parts and "T" in parts[0]:
                    ts = datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
                    age = (now - ts).total_seconds()
                    if age <= max_age_seconds:
                        return (True, line.strip())
            except Exception:
                pass

    return (False, None)


# ── Chronicle Helpers ────────────────────────────────────────────────────────

def _chronicle_query(query: str, params: tuple = ()) -> Optional[list]:
    """
    Execute a query against Chronicle.

    Args:
        query: SQL query
        params: Query parameters

    Returns:
        List of result rows, or None on error
    """
    try:
        conn = sqlite3.connect(CHRONICLE_DB, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(query, params)
        rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("verification_checkers: Chronicle query failed: %s", e)
        return None


# ── Verification Checkers ────────────────────────────────────────────────────

def verify_orphan_position(
    context: Dict[str, Any],
) -> Tuple[VerificationResult, Dict, Optional[str]]:
    """
    Verify that an orphaned position has been closed in Alpaca.

    Args:
        context: Dict with key "position_id" (e.g., "aapl-long-1000")

    Returns:
        Tuple (result, evidence, root_cause)
    """
    position_id = context.get("position_id")
    if not position_id:
        return (
            VerificationResult.MANUAL_REQUIRED,
            {"error": "No position_id in context"},
            "Missing position_id context",
        )

    positions = _alpaca_get_positions()
    if positions is None:
        return (
            VerificationResult.MANUAL_REQUIRED,
            {"error": "Could not fetch positions from Alpaca"},
            "Alpaca API unavailable",
        )

    # Check if position still exists
    for pos in positions:
        if pos.get("symbol") == position_id or pos.get("asset_id") == position_id:
            return (
                VerificationResult.FAILED_VERIFICATION,
                {
                    "check_type": "alpaca_positions",
                    "position_id": position_id,
                    "position_found": True,
                    "position_qty": pos.get("qty"),
                },
                f"Position {position_id} still exists in Alpaca with qty={pos.get('qty')}",
            )

    return (
        VerificationResult.VERIFIED,
        {
            "check_type": "alpaca_positions",
            "position_id": position_id,
            "position_found": False,
            "total_positions_checked": len(positions),
        },
        None,
    )


def verify_agent_hung(
    context: Dict[str, Any],
) -> Tuple[VerificationResult, Dict, Optional[str]]:
    """
    Verify that an agent has restarted and heartbeat is restored.

    Args:
        context: Dict with key "agent_name" (e.g., "alpha-execution")

    Returns:
        Tuple (result, evidence, root_cause)
    """
    agent_name = context.get("agent_name")
    if not agent_name:
        return (
            VerificationResult.MANUAL_REQUIRED,
            {"error": "No agent_name in context"},
            "Missing agent_name context",
        )

    # Check 1: Process running
    is_running, pid = _get_process_status(agent_name)
    if not is_running:
        return (
            VerificationResult.FAILED_VERIFICATION,
            {
                "check_type": "process_status",
                "agent_name": agent_name,
                "is_running": False,
            },
            f"Process {agent_name} is not running",
        )

    # Check 2: Recent heartbeat in logs
    log_file = f"{NEXUS_ROOT}/logs/{agent_name}/stderr.log"
    if not Path(log_file).exists():
        log_file = f"{NEXUS_ROOT}/logs/{agent_name}.log"

    has_heartbeat, heartbeat_line = _find_recent_heartbeat(log_file, max_age_seconds=30)
    if not has_heartbeat:
        return (
            VerificationResult.FAILED_VERIFICATION,
            {
                "check_type": "heartbeat",
                "agent_name": agent_name,
                "log_file": log_file,
                "has_recent_heartbeat": False,
            },
            f"No recent heartbeat for {agent_name} (>30s old)",
        )

    return (
        VerificationResult.VERIFIED,
        {
            "check_type": "agent_health",
            "agent_name": agent_name,
            "is_running": True,
            "pid": pid,
            "has_recent_heartbeat": True,
            "heartbeat_line": heartbeat_line[:100] if heartbeat_line else None,
        },
        None,
    )


def verify_code_failure(
    context: Dict[str, Any],
) -> Tuple[VerificationResult, Dict, Optional[str]]:
    """
    Verify that code has been tested and deployed.

    Args:
        context: Dict with keys "service", "commit_sha" (e.g., "nexus-alpha", "abc123...")

    Returns:
        Tuple (result, evidence, root_cause)
    """
    service = context.get("service")
    commit_sha = context.get("commit_sha")

    if not service:
        return (
            VerificationResult.MANUAL_REQUIRED,
            {"error": "No service in context"},
            "Missing service context",
        )

    # Check 1: Tests pass (try to run pytest on service)
    try:
        service_test_dir = f"{NEXUS_ROOT}/{service}/tests"
        if not Path(service_test_dir).exists():
            service_test_dir = f"{NEXUS_ROOT}/tests/{service}"

        result = subprocess.run(
            ["pytest", service_test_dir, "-v", "--tb=short"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=NEXUS_ROOT,
        )

        tests_passed = result.returncode == 0
        test_output = result.stdout[-500:] if result.stdout else ""  # Last 500 chars

    except Exception as e:
        logger.warning("verification_checkers: Could not run pytest: %s", e)
        tests_passed = False
        test_output = str(e)

    if not tests_passed:
        return (
            VerificationResult.FAILED_VERIFICATION,
            {
                "check_type": "code_tests",
                "service": service,
                "tests_passed": False,
                "test_output": test_output,
            },
            f"Tests failed for {service}",
        )

    # Check 2: Service is deployed and running (simple heuristic)
    is_running, _ = _get_process_status(service)
    if not is_running:
        return (
            VerificationResult.FAILED_VERIFICATION,
            {
                "check_type": "service_status",
                "service": service,
                "is_running": False,
            },
            f"Service {service} is not running after deployment",
        )

    return (
        VerificationResult.VERIFIED,
        {
            "check_type": "code_deployment",
            "service": service,
            "commit_sha": commit_sha,
            "tests_passed": True,
            "is_running": True,
        },
        None,
    )


def verify_config_mismatch(
    context: Dict[str, Any],
) -> Tuple[VerificationResult, Dict, Optional[str]]:
    """
    Verify that config is consistent across all Nexus services.

    Args:
        context: Dict with key "param" (e.g., "MAX_POSITIONS")

    Returns:
        Tuple (result, evidence, root_cause)
    """
    param = context.get("param")
    if not param:
        return (
            VerificationResult.MANUAL_REQUIRED,
            {"error": "No param in context"},
            "Missing param context",
        )

    # Query Chronicle for config consistency
    # This is a simplified check — assumes config values are stored in Chronicle
    query = """
        SELECT service_name, param_name, param_value
        FROM service_config
        WHERE param_name = ?
        ORDER BY service_name
    """
    rows = _chronicle_query(query, (param,))

    if not rows:
        return (
            VerificationResult.MANUAL_REQUIRED,
            {"param": param, "config_found": False},
            "No config found in Chronicle",
        )

    # Check consistency
    values = {row["service_name"]: row["param_value"] for row in rows}
    unique_values = set(values.values())

    if len(unique_values) > 1:
        return (
            VerificationResult.FAILED_VERIFICATION,
            {
                "check_type": "config_consistency",
                "param": param,
                "services_checked": list(values.keys()),
                "inconsistent_values": {k: v for k, v in values.items()},
            },
            f"Config mismatch for {param}: {unique_values}",
        )

    return (
        VerificationResult.VERIFIED,
        {
            "check_type": "config_consistency",
            "param": param,
            "services_checked": list(values.keys()),
            "consistent_value": unique_values.pop(),
        },
        None,
    )


def verify_policy_violation(
    context: Dict[str, Any],
) -> Tuple[VerificationResult, Dict, Optional[str]]:
    """
    Verify that a policy violation has been corrected.

    Args:
        context: Dict with keys "agent_name", "violation_type"

    Returns:
        Tuple (result, evidence, root_cause)
    """
    agent_name = context.get("agent_name")
    violation_type = context.get("violation_type")

    if not agent_name:
        return (
            VerificationResult.MANUAL_REQUIRED,
            {"error": "No agent_name in context"},
            "Missing agent_name context",
        )

    # Query Chronicle for compliance log
    query = """
        SELECT request_id, violation_type, corrected_at, created_at
        FROM compliance_log
        WHERE agent_name = ? AND violation_type = ?
        ORDER BY created_at DESC
        LIMIT 5
    """
    rows = _chronicle_query(query, (agent_name, violation_type or "%"))

    if not rows:
        return (
            VerificationResult.MANUAL_REQUIRED,
            {"agent_name": agent_name, "compliance_found": False},
            "No compliance records found",
        )

    # Check if most recent violation was corrected
    most_recent = rows[0]
    if not most_recent.get("corrected_at"):
        return (
            VerificationResult.FAILED_VERIFICATION,
            {
                "check_type": "compliance",
                "agent_name": agent_name,
                "violation_type": violation_type,
                "most_recent_violation": most_recent.get("created_at"),
                "corrected": False,
            },
            f"Violation {most_recent.get('violation_type')} not yet corrected",
        )

    # Check for compliant behavior in next requests
    query_compliant = """
        SELECT COUNT(*) as count
        FROM request_log
        WHERE agent_name = ? AND compliance_status = 'PASS'
        AND created_at > ?
        LIMIT 3
    """
    compliant_rows = _chronicle_query(
        query_compliant,
        (agent_name, most_recent.get("corrected_at")),
    )

    compliant_count = compliant_rows[0]["count"] if compliant_rows else 0
    if compliant_count < 3:
        return (
            VerificationResult.FAILED_VERIFICATION,
            {
                "check_type": "compliance_behavior",
                "agent_name": agent_name,
                "compliant_requests_after_correction": compliant_count,
            },
            f"Only {compliant_count} compliant requests after correction (need 3+)",
        )

    return (
        VerificationResult.VERIFIED,
        {
            "check_type": "policy_correction",
            "agent_name": agent_name,
            "violation_type": violation_type,
            "corrected_at": most_recent.get("corrected_at"),
            "compliant_requests_verified": compliant_count,
        },
        None,
    )


def verify_threat_detected(
    context: Dict[str, Any],
) -> Tuple[VerificationResult, Dict, Optional[str]]:
    """
    Verify that a threatened agent has been quarantined.

    Args:
        context: Dict with keys "agent_name", "threat_id"

    Returns:
        Tuple (result, evidence, root_cause)
    """
    agent_name = context.get("agent_name")
    threat_id = context.get("threat_id")

    if not agent_name:
        return (
            VerificationResult.MANUAL_REQUIRED,
            {"error": "No agent_name in context"},
            "Missing agent_name context",
        )

    # Check 1: Agent quarantine status
    query = "SELECT quarantine_status FROM agent_state WHERE agent_name = ?"
    rows = _chronicle_query(query, (agent_name,))

    if not rows:
        return (
            VerificationResult.MANUAL_REQUIRED,
            {"agent_name": agent_name, "agent_found": False},
            "Agent not found in Chronicle",
        )

    quarantine_status = rows[0].get("quarantine_status")
    if not quarantine_status:
        return (
            VerificationResult.FAILED_VERIFICATION,
            {
                "check_type": "quarantine_status",
                "agent_name": agent_name,
                "quarantine_status": False,
            },
            f"Agent {agent_name} is not quarantined",
        )

    # Check 2: No recent requests from agent
    query_requests = """
        SELECT COUNT(*) as count
        FROM request_log
        WHERE agent_name = ? AND created_at > datetime('now', '-5 minutes')
    """
    request_rows = _chronicle_query(query_requests, (agent_name,))

    recent_requests = request_rows[0]["count"] if request_rows else 0
    if recent_requests > 0:
        return (
            VerificationResult.FAILED_VERIFICATION,
            {
                "check_type": "request_activity",
                "agent_name": agent_name,
                "recent_requests": recent_requests,
            },
            f"Agent {agent_name} still sending requests ({recent_requests} in last 5min)",
        )

    return (
        VerificationResult.VERIFIED,
        {
            "check_type": "threat_mitigation",
            "agent_name": agent_name,
            "threat_id": threat_id,
            "quarantine_status": True,
            "recent_requests": 0,
        },
        None,
    )
