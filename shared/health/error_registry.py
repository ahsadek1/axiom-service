"""
error_registry.py — Universal Error Taxonomy
=============================================
Single source of truth for all error codes, severities, and fix sequences.
Every agent in the system speaks this language.

Error code format: E{category}{number}
  ESV = Service errors
  EDA = Data errors  
  EAG = Agent errors
  EPL = Pipeline errors
  EIN = Infrastructure errors

Ahmed directive May 2026:
  "A standardized approach of all agents towards the list of errors.
   The sequence of the fix is also standardized so there is no judgment call."
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class Severity(str, Enum):
    P0 = "P0"  # Critical — trading blocked
    P1 = "P1"  # High — degraded operation
    P2 = "P2"  # Medium — non-critical impact
    P3 = "P3"  # Low — informational


class Category(str, Enum):
    SERVICE      = "SERVICE"
    DATA         = "DATA"
    AGENT        = "AGENT"
    PIPELINE     = "PIPELINE"
    INFRA        = "INFRA"


@dataclass
class ErrorDefinition:
    code:        str
    name:        str
    category:    Category
    severity:    Severity
    description: str
    detection:   str           # How to detect this error
    fix_steps:   list[str]     # Ordered fix sequence (deterministic)
    can_auto_fix: bool = True   # False = alert only
    escalate_after_attempts: int = 3


# ---------------------------------------------------------------------------
# Universal Error Registry
# ---------------------------------------------------------------------------

ERROR_REGISTRY: dict[str, ErrorDefinition] = {

    # ── SERVICE ERRORS ────────────────────────────────────────────────────────

    "ESV001": ErrorDefinition(
        code="ESV001", name="Service Port Unresponsive",
        category=Category.SERVICE, severity=Severity.P0,
        description="Service health endpoint not responding on expected port",
        detection="HTTP GET /health returns non-200 or connection refused",
        fix_steps=[
            "launchctl unload plist",
            "sleep 2",
            "launchctl load plist",
            "sleep 6",
            "verify health endpoint returns 200",
        ],
    ),

    "ESV002": ErrorDefinition(
        code="ESV002", name="Service Health Degraded",
        category=Category.SERVICE, severity=Severity.P1,
        description="Service responds but status is not healthy/ok",
        detection="HTTP GET /health returns 200 but status != healthy",
        fix_steps=[
            "log current health response",
            "launchctl unload plist",
            "sleep 3",
            "launchctl load plist",
            "sleep 8",
            "verify health status is healthy",
        ],
    ),

    "ESV003": ErrorDefinition(
        code="ESV003", name="Service Crash Loop",
        category=Category.SERVICE, severity=Severity.P0,
        description="Service has crashed and restarted more than 3 times in 30 minutes",
        detection="restart count > 3 in 30 min window from launchd logs",
        fix_steps=[
            "collect crash logs",
            "check for config corruption",
            "check for dependency failures",
            "attempt clean restart with fresh state",
            "verify health endpoint",
        ],
        escalate_after_attempts=2,
    ),

    # ── DATA ERRORS ───────────────────────────────────────────────────────────

    "EDA001": ErrorDefinition(
        code="EDA001", name="Database Connection Timeout",
        category=Category.DATA, severity=Severity.P1,
        description="SQLite connection timeout — possible lock contention",
        detection="sqlite3.connect() timeout after threshold",
        fix_steps=[
            "find and release stale lock files",
            "close all existing connections",
            "reconnect with increased timeout",
            "verify read/write operations succeed",
        ],
    ),

    "EDA002": ErrorDefinition(
        code="EDA002", name="Database Integrity Violation",
        category=Category.DATA, severity=Severity.P0,
        description="SQLite integrity check failed — possible corruption",
        detection="PRAGMA integrity_check returns non-ok",
        fix_steps=[
            "run integrity_check",
            "backup current DB",
            "restore from last known good backup",
            "verify integrity_check passes",
        ],
        escalate_after_attempts=1,
    ),

    "EDA003": ErrorDefinition(
        code="EDA003", name="Stale Cache",
        category=Category.DATA, severity=Severity.P2,
        description="Cached data exceeds TTL threshold",
        detection="cache_age > TTL threshold",
        fix_steps=[
            "clear stale cache entries",
            "trigger cache refresh",
            "verify fresh data loaded",
        ],
    ),

    # ── AGENT ERRORS ──────────────────────────────────────────────────────────

    "EAG001": ErrorDefinition(
        code="EAG001", name="Agent Submission Silence",
        category=Category.AGENT, severity=Severity.P1,
        description="Agent has not submitted any picks during market hours for threshold period",
        detection="MAX(received_at) from submissions WHERE agent=X > 30min ago during market hours",
        fix_steps=[
            "check agent process status",
            "check agent port health endpoint",
            "if port down: restart via OpenClaw",
            "if port up: restart agent process",
            "verify new submission arrives within 10 minutes",
        ],
    ),

    "EAG002": ErrorDefinition(
        code="EAG002", name="Agent Score Distribution Broken",
        category=Category.AGENT, severity=Severity.P1,
        description="Agent submitting but >80% of scores are identical — data failure",
        detection="score distribution analysis: most_common_score_pct > 0.80",
        fix_steps=[
            "log current score distribution",
            "restart agent to clear state",
            "monitor next 3 submissions for distribution",
            "verify scores vary normally",
        ],
    ),

    "EAG003": ErrorDefinition(
        code="EAG003", name="Agent API Key Invalid",
        category=Category.AGENT, severity=Severity.P0,
        description="Agent receiving 401 from its LLM/data API",
        detection="API call returns 401 or 403",
        fix_steps=[
            "log API error details",
            "alert — cannot auto-fix credential issues",
        ],
        can_auto_fix=False,
    ),

    "EAG004": ErrorDefinition(
        code="EAG004", name="Agent API Rate Limited",
        category=Category.AGENT, severity=Severity.P1,
        description="Agent receiving 429 from API — rate limit hit",
        detection="API call returns 429",
        fix_steps=[
            "implement exponential backoff (30s, 60s, 120s)",
            "reduce submission frequency for this cycle",
            "verify API calls succeed after backoff",
        ],
    ),

    # ── PIPELINE ERRORS ───────────────────────────────────────────────────────

    "EPL001": ErrorDefinition(
        code="EPL001", name="Circuit Breaker STOP",
        category=Category.PIPELINE, severity=Severity.P0,
        description="Alpha-buffer circuit breaker in STOP state",
        detection="SELECT status FROM circuit_breaker_state = STOP",
        fix_steps=[
            "check if paper mode (auto-reset allowed off-hours)",
            "check if market hours (do not auto-reset during market)",
            "paper + off-hours: POST /circuit-breaker/reset",
            "live or market-hours: alert Ahmed — requires manual review",
            "verify CB status = NORMAL",
        ],
    ),

    "EPL002": ErrorDefinition(
        code="EPL002", name="VIX Brake Stuck at 999",
        category=Category.PIPELINE, severity=Severity.P1,
        description="Alpha-execution VIX showing 999 — startup race condition",
        detection="GET /health vix = 999 or vix_brake_full = true",
        fix_steps=[
            "verify Axiom is healthy first",
            "restart alpha-execution",
            "verify VIX reads normally on next health check",
        ],
    ),

    "EPL003": ErrorDefinition(
        code="EPL003", name="OMNI Synthesis Silence",
        category=Category.PIPELINE, severity=Severity.P1,
        description="OMNI has not synthesized during market hours for 20+ minutes",
        detection="GET /health last_synthesis_min_ago > 20 during market hours",
        fix_steps=[
            "check if concordances are arriving (agent submissions OK?)",
            "check synthesis pool status",
            "restart OMNI service",
            "verify synthesis activity resumes",
        ],
    ),

    "EPL004": ErrorDefinition(
        code="EPL004", name="DLQ Backlog",
        category=Category.PIPELINE, severity=Severity.P2,
        description="Dead letter queue has 5+ failed executions pending",
        detection="GET /dlq/status pending >= 5",
        fix_steps=[
            "POST /dlq/drain",
            "verify drain successful",
            "monitor for re-accumulation",
        ],
    ),

    "EPL005": ErrorDefinition(
        code="EPL005", name="Zero Concordance Rate",
        category=Category.PIPELINE, severity=Severity.P1,
        description="No concordances formed in 60+ minutes during market hours",
        detection="no new concordance_results in DB for 60min during market hours",
        fix_steps=[
            "check agent submission activity (EAG001)",
            "check scanner is running and scanning",
            "check alpha-buffer health",
            "check OMNI is healthy",
            "restart pipeline in order: axiom → alpha-buffer → omni",
        ],
    ),

    # ── INFRASTRUCTURE ERRORS ─────────────────────────────────────────────────

    "EIN001": ErrorDefinition(
        code="EIN001", name="Memory Critical",
        category=Category.INFRA, severity=Severity.P1,
        description="System memory usage exceeds 85%",
        detection="vm_stat / free memory check > 85%",
        fix_steps=[
            "identify top memory consumers (ps aux sort by mem)",
            "rotate large log files",
            "restart highest-memory non-critical service",
            "verify memory drops below 75%",
        ],
    ),

    "EIN002": ErrorDefinition(
        code="EIN002", name="Disk Space Critical",
        category=Category.INFRA, severity=Severity.P1,
        description="Disk free space below 10%",
        detection="df -h / shows < 10% free",
        fix_steps=[
            "find log files > 500MB and rotate",
            "find temp files > 100MB and archive",
            "verify disk free > 15%",
        ],
    ),

    "EIN003": ErrorDefinition(
        code="EIN003", name="CPU Sustained High",
        category=Category.INFRA, severity=Severity.P1,
        description="CPU usage above 80% for 10+ consecutive minutes",
        detection="top average > 80% for 10min",
        fix_steps=[
            "identify runaway process",
            "log process details",
            "restart identified process",
            "verify CPU normalizes",
        ],
    ),

    "EIN004": ErrorDefinition(
        code="EIN004", name="Log File Oversized",
        category=Category.INFRA, severity=Severity.P2,
        description="Log file exceeds 1GB",
        detection="find logs -size +1G",
        fix_steps=[
            "rotate log file immediately",
            "archive to compressed backup",
            "verify new log file created correctly",
        ],
    ),

    "EIN005": ErrorDefinition(
        code="EIN005", name="Stale Lock File",
        category=Category.INFRA, severity=Severity.P2,
        description="Lock file older than 2 hours detected",
        detection="find lock files -mtime +2h",
        fix_steps=[
            "verify associated process is not running",
            "remove stale lock file",
            "restart associated service",
            "verify service healthy",
        ],
    ),

    "EIN006": ErrorDefinition(
        code="EIN006", name="Cross-Machine Connectivity Lost",
        category=Category.INFRA, severity=Severity.P1,
        description="SSH connectivity to Mac Mini 2 (192.168.1.42) lost",
        detection="ssh timeout after 5s",
        fix_steps=[
            "ping 192.168.1.42",
            "check local network",
            "attempt SSH with extended timeout",
            "if unreachable: alert — cannot auto-fix network partition",
        ],
    ),
}


def get_error(code: str) -> Optional[ErrorDefinition]:
    return ERROR_REGISTRY.get(code)


def get_errors_by_severity(severity: Severity) -> list[ErrorDefinition]:
    return [e for e in ERROR_REGISTRY.values() if e.severity == severity]


def get_errors_by_category(category: Category) -> list[ErrorDefinition]:
    return [e for e in ERROR_REGISTRY.values() if e.category == category]
