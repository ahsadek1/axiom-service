#!/usr/bin/env python3
"""
preflight_runner.py — SQS Preflight Validation Orchestrator

Runs every trading day at 8:30 AM ET (before market open at 9:30 AM).
Tests all 9 subengines, validates critical components, produces GO/NO-GO report.

Architecture:
  1. Load engine registry (9 subengines)
  2. Run all health checks in parallel
  3. Attempt auto-recovery on failures
  4. Generate comprehensive report
  5. Send to Ahmed DM + log to CHRONICLE
  6. Exit with status code (0=GO, 1=NO-GO)

MANDATE:
  - All checks run to completion (no early exit)
  - Every failure is diagnosed and attempted-fix logged
  - Report is actionable (specific remediation steps)
  - Execution time < 30 seconds (market opens at 9:30 AM)
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytz
import requests

# Relative imports from shared
sys.path.insert(0, "/Users/ahmedsadek/nexus")
sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")

from alert_client import send_alert
from health_checks import run_all_checks, HealthCheckResult, EngineStatus
from recovery_actions import attempt_auto_recovery, RecoveryResult

# =====================================================================
# CONFIGURATION
# =====================================================================

ET = pytz.timezone("America/New_York")
LOG_DIR = Path("/Users/ahmedsadek/nexus/logs")
LOG_FILE = LOG_DIR / "sqs_preflight.log"
CHRONICLE_DB = Path("/Users/ahmedsadek/nexus/data/chronicle.db")

# Create logs directory if needed
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("preflight_runner")

# The 9 engines we test
ENGINES = [
    "ATG_SWING",
    "ATG_INTRADAY",
    "AILS",
    "ATM_SWING",
    "ATM_MULTIWEEK",
    "AMAT",
    "ATEM",
    "ATOM",
    "ATG_GAMMA",
]

AHMED_CHAT_ID = os.getenv("AHMED_CHAT_ID", "8573754783")
AXIOM_BOT_TOKEN = os.getenv("AXIOM_BOT_TOKEN", "8664199571:AAF5H9XFvhhjcbAiLuV-qul2humI3yWO2fo")


# =====================================================================
# DATA STRUCTURES
# =====================================================================

class OverallStatus(Enum):
    """System status."""
    READY = "READY"
    NOT_READY = "NOT_READY"
    DEGRADED = "DEGRADED"  # Some engines down, but critical path clear


@dataclass
class PreflightReport:
    """Comprehensive preflight report."""
    timestamp: str
    overall_status: str  # READY, NOT_READY, DEGRADED
    confidence_score: float  # 0.0-1.0
    execution_time_ms: float
    engines_ok: int
    engines_total: int
    engine_results: Dict[str, dict] = field(default_factory=dict)
    critical_failures: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    remediation_steps: List[str] = field(default_factory=list)
    recovery_attempts: List[str] = field(default_factory=list)


# =====================================================================
# MAIN ORCHESTRATION
# =====================================================================

async def run_preflight() -> PreflightReport:
    """Run complete preflight validation."""
    start_time = time.time()
    start_dt = datetime.now(ET)
    
    logger.info("=" * 80)
    logger.info("PREFLIGHT VALIDATION START — %s ET", start_dt.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 80)
    
    # Phase 1: Run all health checks
    logger.info("[PHASE 1] Running health checks for all %d engines...", len(ENGINES))
    health_results = await run_all_checks(ENGINES)
    
    # Phase 2: Analyze results
    logger.info("[PHASE 2] Analyzing health check results...")
    report = _analyze_results(health_results, start_dt)
    
    # Phase 3: Attempt recovery on failures
    if report.critical_failures:
        logger.info("[PHASE 3] Attempting auto-recovery for %d failures...", len(report.critical_failures))
        recovery_results = await _attempt_recovery_phase(report, health_results)
        report.recovery_attempts = recovery_results
        
        # Re-run checks on recovered services
        logger.info("[PHASE 3b] Re-testing recovered services...")
        health_results = await run_all_checks(ENGINES)
        report = _analyze_results(health_results, start_dt)
    
    # Phase 4: Calculate final metrics
    report.execution_time_ms = (time.time() - start_time) * 1000
    
    logger.info("[PHASE 4] Finalizing report...")
    logger.info("  Overall Status: %s", report.overall_status)
    logger.info("  Confidence: %.1f%%", report.confidence_score * 100)
    logger.info("  Engines: %d/%d OK", report.engines_ok, report.engines_total)
    logger.info("  Execution Time: %.1f ms", report.execution_time_ms)
    
    return report


def _analyze_results(
    health_results: Dict[str, HealthCheckResult],
    timestamp: datetime,
) -> PreflightReport:
    """Analyze health check results and build report."""
    report = PreflightReport(
        timestamp=timestamp.isoformat(),
        overall_status="READY",  # Will be updated
        confidence_score=1.0,
        execution_time_ms=0,
        engines_ok=0,
        engines_total=len(ENGINES),
        engine_results={},
    )
    
    for engine_name in ENGINES:
        result = health_results.get(engine_name)
        
        if not result:
            logger.error("Missing health check result for %s", engine_name)
            report.engine_results[engine_name] = {
                "status": "UNKNOWN",
                "checks_passed": 0,
                "checks_total": 0,
                "failures": ["No health check result"],
            }
            report.critical_failures.append(f"Missing health check: {engine_name}")
            report.confidence_score *= 0.95
            continue
        
        # Extract engine status
        engine_ok = result.overall_pass
        report.engine_results[engine_name] = {
            "status": "OK" if engine_ok else "FAIL",
            "checks_passed": sum(1 for c in result.checks if c.passed),
            "checks_total": len(result.checks),
            "failures": [c.error_message for c in result.checks if not c.passed],
        }
        
        if engine_ok:
            report.engines_ok += 1
        else:
            # Log specific failures
            for check in result.checks:
                if not check.passed:
                    failure_msg = f"{engine_name}: {check.check_type} — {check.error_message}"
                    report.critical_failures.append(failure_msg)
                    logger.error("FAILURE: %s", failure_msg)
            
            # Apply confidence penalty
            report.confidence_score *= 0.85
    
    # Determine overall status
    if report.engines_ok == report.engines_total:
        report.overall_status = "READY"
    elif report.engines_ok >= report.engines_total * 0.8:  # 80% engines OK
        report.overall_status = "DEGRADED"
        report.warnings.append(f"Only {report.engines_ok}/{report.engines_total} engines ready")
    else:
        report.overall_status = "NOT_READY"
    
    # Generate remediation steps
    if report.critical_failures:
        report.remediation_steps = _generate_remediation_steps(report.critical_failures)
    
    return report


def _generate_remediation_steps(failures: List[str]) -> List[str]:
    """Generate actionable remediation steps based on failures."""
    steps = []
    failure_text = "\n".join(failures)
    
    if "process health" in failure_text.lower():
        steps.append("Restart failed engine processes: `restart_engines.sh`")
    
    if "database" in failure_text.lower():
        steps.append("Check database locks: `lsof | grep *.db`")
        steps.append("Rebuild database if corrupted: `rebuild_capital_db.sh`")
    
    if "sqs" in failure_text.lower():
        steps.append("Verify AWS credentials in .env")
        steps.append("Check network: `curl https://sqs.us-east-1.amazonaws.com`")
    
    if "alpaca" in failure_text.lower():
        steps.append("Verify Alpaca API keys in alpha-execution/.env")
        steps.append("Check paper trading enabled: ALPACA_PAPER=true")
    
    if "telegram" in failure_text.lower():
        steps.append("Verify Telegram bot tokens in .env")
        steps.append("Test bot: `curl https://api.telegram.org/bot<token>/getMe`")
    
    if "fred" in failure_text.lower():
        steps.append("Verify FRED API key in .env")
        steps.append("Check FRED service: `curl https://api.stlouisfed.org/fred/series`")
    
    if not steps:
        steps.append("Review detailed logs: `tail -100 /Users/ahmedsadek/nexus/logs/sqs_preflight.log`")
        steps.append("Contact Ahmed for manual intervention")
    
    return steps


async def _attempt_recovery_phase(
    report: PreflightReport,
    health_results: Dict[str, HealthCheckResult],
) -> List[str]:
    """Attempt automatic recovery for failed services."""
    recovery_attempts = []
    
    for failure in report.critical_failures[:5]:  # Max 5 recovery attempts
        logger.info("Attempting recovery for: %s", failure)
        
        try:
            recovery_result = await attempt_auto_recovery(failure)
            status = "SUCCESS" if recovery_result.success else "FAILED"
            attempt_log = f"Recovery for '{failure}': {status} — {recovery_result.details}"
            recovery_attempts.append(attempt_log)
            logger.info(attempt_log)
        except Exception as e:
            attempt_log = f"Recovery failed for '{failure}': {str(e)}"
            recovery_attempts.append(attempt_log)
            logger.error(attempt_log)
    
    return recovery_attempts


def _send_report_to_ahmed(report: PreflightReport) -> None:
    """Send preflight report to Ahmed via Telegram."""
    try:
        # Build message
        emoji = "✅" if report.overall_status == "READY" else "🚨" if report.overall_status == "NOT_READY" else "⚠️"
        
        message = f"""{emoji} **SQS PREFLIGHT REPORT**

*Status:* **{report.overall_status}**
*Confidence:* {report.confidence_score * 100:.0f}%
*Engines:* {report.engines_ok}/{report.engines_total} OK
*Exec Time:* {report.execution_time_ms:.0f} ms
*Timestamp:* {report.timestamp}

**Engine Status Table:**
```
Engine          Status
─────────────────────────"""
        
        for engine_name in ENGINES:
            result = report.engine_results.get(engine_name, {})
            status = result.get("status", "?")
            icon = "✅" if status == "OK" else "❌"
            message += f"\n{engine_name:17} {icon} {status}"
        
        message += """
```
"""
        
        # Add failures if any
        if report.critical_failures:
            message += f"\n**Failures ({len(report.critical_failures)}):**\n"
            for i, failure in enumerate(report.critical_failures[:5], 1):
                message += f"{i}. {failure}\n"
            if len(report.critical_failures) > 5:
                message += f"... and {len(report.critical_failures) - 5} more\n"
        
        # Add remediation
        if report.remediation_steps:
            message += f"\n**Remediation Steps:**\n"
            for i, step in enumerate(report.remediation_steps[:5], 1):
                message += f"{i}. {step}\n"
        
        # Send via Telegram
        url = f"https://api.telegram.org/bot{AXIOM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": AHMED_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        }
        
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            logger.info("Report sent to Ahmed successfully")
        else:
            logger.error("Failed to send report to Ahmed: HTTP %d", response.status_code)
    
    except Exception as e:
        logger.error("Error sending report to Ahmed: %s", e)
        # Fallback to alert_client
        try:
            send_alert(
                source="preflight_runner",
                level="CRITICAL",
                title=f"SQS Preflight: {report.overall_status}",
                body=f"Confidence: {report.confidence_score*100:.0f}% | Engines: {report.engines_ok}/{report.engines_total}",
                dedup_key="sqs_preflight_daily",
                targets=["ahmed"],
            )
        except Exception as e2:
            logger.error("Alert fallback also failed: %s", e2)


def _store_results_in_chronicle(report: PreflightReport) -> None:
    """Store preflight results in CHRONICLE database."""
    try:
        if not CHRONICLE_DB.exists():
            logger.warning("CHRONICLE database not found at %s", CHRONICLE_DB)
            return
        
        conn = sqlite3.connect(str(CHRONICLE_DB))
        cursor = conn.cursor()
        
        # Create preflight_results table if needed
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS preflight_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                overall_status TEXT NOT NULL,
                confidence_score REAL NOT NULL,
                execution_time_ms REAL NOT NULL,
                engines_ok INTEGER NOT NULL,
                engines_total INTEGER NOT NULL,
                critical_failures TEXT,
                warnings TEXT,
                remediation_steps TEXT,
                engine_results_json TEXT,
                full_report_json TEXT
            )
        """)
        
        # Insert record
        cursor.execute("""
            INSERT INTO preflight_results (
                timestamp, overall_status, confidence_score, execution_time_ms,
                engines_ok, engines_total, critical_failures, warnings,
                remediation_steps, engine_results_json, full_report_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            report.timestamp,
            report.overall_status,
            report.confidence_score,
            report.execution_time_ms,
            report.engines_ok,
            report.engines_total,
            json.dumps(report.critical_failures),
            json.dumps(report.warnings),
            json.dumps(report.remediation_steps),
            json.dumps(report.engine_results),
            json.dumps(asdict(report)),
        ))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        logger.info("Results stored in CHRONICLE database")
    
    except Exception as e:
        logger.error("Error storing results in CHRONICLE: %s", e)


# =====================================================================
# ENTRY POINT
# =====================================================================

def main():
    """Main entry point."""
    try:
        # Run async preflight
        report = asyncio.run(run_preflight())
        
        # Send report to Ahmed
        _send_report_to_ahmed(report)
        
        # Store in CHRONICLE
        _store_results_in_chronicle(report)
        
        # Log final summary
        logger.info("=" * 80)
        logger.info("PREFLIGHT VALIDATION COMPLETE")
        logger.info("  Status: %s", report.overall_status)
        logger.info("  Engines: %d/%d OK", report.engines_ok, report.engines_total)
        logger.info("  Confidence: %.1f%%", report.confidence_score * 100)
        logger.info("=" * 80)
        
        # Exit with appropriate code
        exit_code = 0 if report.overall_status == "READY" else 1
        sys.exit(exit_code)
    
    except Exception as e:
        logger.exception("FATAL: Preflight validation crashed: %s", e)
        
        # Send critical alert
        try:
            send_alert(
                source="preflight_runner",
                level="CRITICAL",
                title="Preflight Validation Crash",
                body=str(e),
                dedup_key="preflight_crash",
                targets=["ahmed"],
            )
        except Exception:
            pass
        
        sys.exit(1)


if __name__ == "__main__":
    main()
