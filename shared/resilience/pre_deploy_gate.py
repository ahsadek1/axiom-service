"""
pre_deploy_gate.py — GENESIS G1: Pre-Deploy Contract Gate
Spec: genesis-resilience-v1.md v1.2 — BLOCK G1
Author: GENESIS | Built: 2026-05-02

Before any code is deployed to a production service:
  1. Full test suite must pass
  2. No hardcoded secrets in changed files
  3. No new TODO/FIXME/HACK markers in changed files
  4. Cipher pre-deploy checklist file created

WAL logging is advisory — failures logged loudly, deploy never blocked by WAL.
Circuit breaker registry uses local cache — CHRONICLE is refresh-only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("resilience.pre_deploy_gate")

# ---------------------------------------------------------------------------
# Gate result
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    passed: bool
    checks: dict = field(default_factory=dict)  # check_name → (passed, detail)
    blockers: list = field(default_factory=list)  # list of blocking issues
    warnings: list = field(default_factory=list)  # non-blocking issues

    def summary(self) -> str:
        if self.passed:
            return f"GATE PASSED — {len(self.checks)} checks, {len(self.warnings)} warnings"
        return f"GATE BLOCKED — {len(self.blockers)} blocker(s): {'; '.join(self.blockers[:3])}"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r'(?i)(api_key|secret|password|token)\s*=\s*["\'][^"\']{8,}["\']'),
    re.compile(r'sk-[a-zA-Z0-9]{20,}'),   # OpenAI-style keys
    re.compile(r'ghp_[a-zA-Z0-9]{36}'),   # GitHub PAT
]

_DEBT_MARKERS = re.compile(r'\b(TODO|FIXME|HACK)\b')


def _check_secrets(changed_files: list[str]) -> tuple[bool, str]:
    """Scan changed files for hardcoded secrets."""
    hits = []
    for path in changed_files:
        try:
            content = Path(path).read_text(errors="ignore")
            for pattern in _SECRET_PATTERNS:
                if pattern.search(content):
                    hits.append(f"{path}: potential hardcoded secret")
                    break
        except Exception:
            pass
    if hits:
        return False, f"Hardcoded secrets detected: {hits[:3]}"
    return True, "No hardcoded secrets found"


def _check_debt_markers(changed_files: list[str]) -> tuple[bool, str]:
    """Warn (not block) on new TODO/FIXME/HACK markers."""
    hits = []
    for path in changed_files:
        try:
            content = Path(path).read_text(errors="ignore")
            matches = _DEBT_MARKERS.findall(content)
            if matches:
                hits.append(f"{path}: {len(matches)} marker(s)")
        except Exception:
            pass
    if hits:
        return False, f"Debt markers found (non-blocking): {hits[:3]}"
    return True, "No debt markers found"


def _run_tests(service_dir: str) -> tuple[bool, str]:
    """Run pytest in the service directory. Returns (passed, detail)."""
    try:
        result = subprocess.run(
            ["python3", "-m", "pytest", "tests/", "-q", "--tb=no"],
            cwd=service_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            # Extract pass count
            last_line = result.stdout.strip().split("\n")[-1] if result.stdout else ""
            return True, f"Tests passed: {last_line}"
        else:
            last_lines = "\n".join(result.stdout.strip().split("\n")[-5:])
            return False, f"Tests failed:\n{last_lines}"
    except subprocess.TimeoutExpired:
        return False, "Test suite timed out (>120s)"
    except Exception as e:
        return False, f"Test run error: {e}"


# ---------------------------------------------------------------------------
# Cipher checklist file
# ---------------------------------------------------------------------------

_CIPHER_REVIEW_DIR = Path("/Users/ahmedsadek/nexus/.cipher_review")


def _create_cipher_checklist(service: str, changes: list[str]) -> str:
    """
    Create Cipher pre-deploy checklist file.
    Returns path to the created file.
    """
    _CIPHER_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    path = _CIPHER_REVIEW_DIR / f"{service}_{timestamp}.json"
    checklist = {
        "agent": "genesis",
        "service": service,
        "changes": changes,
        "cipher_approved": False,
        "cipher_approved_at": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path.write_text(json.dumps(checklist, indent=2))
    logger.info("Cipher checklist created: %s", path)
    return str(path)


def _is_cipher_approved(checklist_path: str) -> bool:
    """Check if Cipher has approved the checklist file."""
    try:
        data = json.loads(Path(checklist_path).read_text())
        return bool(data.get("cipher_approved"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Execution path watchlist
# ---------------------------------------------------------------------------

_EXECUTION_PATH_FILES = {
    "alpha-execution/main.py",
    "prime-execution/main.py",
    "shared/risk_engine.py",
    "axiom/risk_engine.py",
}

_EXECUTION_PATH_PATTERNS = [
    re.compile(r'HARD_RULES'),
    re.compile(r'MIN_SUBMISSION_SCORE'),
    re.compile(r'MAX_POSITIONS'),
]


def _touches_execution_path(changed_files: list[str]) -> bool:
    """Returns True if any changed file is on the execution path watchlist."""
    nexus_root = Path("/Users/ahmedsadek/nexus")
    for f in changed_files:
        try:
            rel = str(Path(f).relative_to(nexus_root))
        except ValueError:
            rel = f  # not under nexus root — use as-is for content scan
        if rel in _EXECUTION_PATH_FILES:
            return True
        try:
            content = Path(f).read_text(errors="ignore")
            for pattern in _EXECUTION_PATH_PATTERNS:
                if pattern.search(content):
                    return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_pre_deploy_gate(
    service: str,
    service_dir: str,
    changed_files: list[str],
    require_cipher_approval: bool = False,
) -> GateResult:
    """
    Run the pre-deploy gate for a service.

    Args:
        service:                 Service name (e.g. "alpha-execution")
        service_dir:             Path to service directory (for test run)
        changed_files:           List of changed file paths
        require_cipher_approval: If True, block until Cipher approves checklist

    Returns:
        GateResult — check .passed before deploying
    """
    result = GateResult(passed=True)

    # Check 1: No hardcoded secrets (BLOCKING)
    ok, detail = _check_secrets(changed_files)
    result.checks["secrets"] = (ok, detail)
    if not ok:
        result.passed = False
        result.blockers.append(detail)

    # Check 2: Debt markers (WARNING only)
    ok, detail = _check_debt_markers(changed_files)
    result.checks["debt_markers"] = (ok, detail)
    if not ok:
        result.warnings.append(detail)

    # Check 3: Test suite (BLOCKING)
    ok, detail = _run_tests(service_dir)
    result.checks["tests"] = (ok, detail)
    if not ok:
        result.passed = False
        result.blockers.append(detail)

    # Check 4: Cipher checklist (always create; block if require_cipher_approval=True)
    touches_exec = _touches_execution_path(changed_files)
    checklist_path = _create_cipher_checklist(service, changed_files)
    result.checks["cipher_checklist"] = (True, f"Created: {checklist_path}")

    if touches_exec or require_cipher_approval:
        approved = _is_cipher_approved(checklist_path)
        result.checks["cipher_approval"] = (approved, checklist_path)
        if not approved:
            result.passed = False
            result.blockers.append(
                f"Cipher approval required (execution path touched). "
                f"Checklist: {checklist_path}"
            )

    # Log to RESILIENCE-DB
    try:
        from shared.resilience.triad_db import log_intervention
        log_intervention(
            agent="genesis",
            action_type="pre_deploy_gate",
            target=service,
            trigger="deploy_requested",
            outcome="recovered" if result.passed else "failed",
            pre_state={"changed_files": changed_files[:10]},
            post_state={"checks": {k: v[0] for k, v in result.checks.items()},
                        "blockers": result.blockers},
            error="; ".join(result.blockers) if result.blockers else None,
        )
    except Exception as e:
        logger.error("Gate result logging failed (non-fatal): %s", e)

    logger.info("Pre-deploy gate for %s: %s", service, result.summary())
    return result
