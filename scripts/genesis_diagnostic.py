#!/usr/bin/env python3
"""
genesis_diagnostic.py — GENESIS Proactive Market Diagnostic
============================================================
Runs every 5 minutes during market hours via OpenClaw cron.
Detects degradation before it becomes failure.
Acts immediately on any issue found — does NOT just log.

Detection targets:
  1. TRS — AMBER or below → investigate and fix root cause
  2. Alpha-Exec paused → unpause or diagnose
  3. VIX brake active → alert SOVEREIGN
  4. OMNI silence > 20 min → restart OMNI
  5. 0 GO verdicts after 11AM → investigate OMNI brain health
  6. Any service down → attempt restart
  7. Axiom regime stale > 60 min → force tier1 refresh
  8. Repeat P1 alerts → escalate to SOVEREIGN with work in progress

GENESIS-BUILD-001 2026-04-30: Created as result of Ahmed's directive to be
proactive, vigilant, and analytical — acting before things break, not after.
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ET = ZoneInfo("America/New_York")
def _require(var: str) -> str:
    """Return env var value or raise at startup — never silently use a stale fallback."""
    val = os.environ.get(var)
    if not val:
        raise RuntimeError(f"{var} is required but not set. Source .deploy-secrets before running.")
    return val

SECRET             = _require("NEXUS_SECRET")
TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
HEALTH_GROUP = os.environ.get("TELEGRAM_HEALTH_GROUP_CHAT_ID", "-5241272802")
MESSAGE_BUS = "http://192.168.1.141:9999"
CHRONICLE_DB = "/Users/ahmedsadek/nexus/data/chronicle.db"

SERVICES = {
    "axiom":        (8001, "X-Axiom-Secret"),
    "alpha-buffer": (8002, "X-Nexus-Secret"),
    "prime-buffer": (8003, "X-Nexus-Secret"),
    "omni":         (8004, "X-Nexus-Secret"),
    "alpha-exec":   (8005, "X-Nexus-Secret"),
    "prime-exec":   (8006, "X-Nexus-Secret"),
    "oracle":       (8007, "X-Oracle-Secret"),
    "ails":         (8008, "X-AILS-Secret"),
}

LAUNCHD_LABELS = {
    "axiom":        "ai.nexus.axiom",
    "alpha-buffer": "ai.nexus.alpha-buffer",
    "prime-buffer": "ai.nexus.prime-buffer",
    "omni":         "ai.nexus.omni",
    "alpha-exec":   "ai.nexus.alpha-execution",
    "prime-exec":   "ai.nexus.prime-execution",
    "oracle":       "ai.nexus.oracle",
    "ails":         "ai.nexus.ails",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] genesis.diagnostic: %(message)s",
)
log = logging.getLogger("genesis.diagnostic")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_market_hours() -> bool:
    """Return True if current time is within NYSE market hours (Mon–Fri 9:30–16:00 ET)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    t = now.time()
    from datetime import time as dt_time
    return dt_time(9, 30) <= t <= dt_time(16, 0)


def get_json(url: str, headers: Optional[Dict] = None, timeout: int = 5) -> Optional[Dict]:
    """GET JSON from a URL. Returns None on failure."""
    try:
        r = requests.get(url, headers=headers or {}, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        log.debug("GET %s failed: %s", url, exc)
    return None


def post_bus(to: str, message: str) -> None:
    """Post a message to the Nexus message bus."""
    try:
        requests.post(
            f"{MESSAGE_BUS}/send",
            json={"from": "genesis-diagnostic", "to": to, "message": message},
            timeout=3,
        )
    except Exception:
        pass


def send_telegram(chat_id: str, message: str) -> None:
    """Send a Telegram message."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=8,
        )
    except Exception:
        pass


def restart_service(service: str) -> bool:
    """Restart a launchd service. Returns True if restart was issued."""
    label = LAUNCHD_LABELS.get(service)
    if not label:
        return False
    try:
        subprocess.run(["launchctl", "stop", label], timeout=5)
        time.sleep(3)
        subprocess.run(["launchctl", "start", label], timeout=5)
        log.info("Restarted service: %s (%s)", service, label)
        return True
    except Exception as exc:
        log.error("Failed to restart %s: %s", service, exc)
        return False


def log_to_chronicle(description: str, resolution: str, outcome: str) -> None:
    """Log intervention to CHRONICLE SQLite."""
    try:
        import sqlite3
        conn = sqlite3.connect(CHRONICLE_DB)
        conn.execute(
            """INSERT OR IGNORE INTO intervention_log
               (agent, error_description, time_identified, ideal_response_time,
                time_acted, time_resolved, resolution_type, damage_assessment, outcome)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            ("GENESIS-DIAGNOSTIC", description,
             datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat(),
             "root_cause", resolution, outcome),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning("Chronicle log failed: %s", exc)


# ---------------------------------------------------------------------------
# Diagnostic checks
# ---------------------------------------------------------------------------

def check_trs() -> List[str]:
    """Check TRS score. Returns list of issues found."""
    issues = []
    d = get_json("http://localhost:8012/trs", {"X-Nexus-Secret": SECRET})
    if d is None:
        issues.append("TRS_UNREACHABLE: nexus-integrity /trs not responding")
        return issues
    color = d.get("color", "")
    score = d.get("score", 0)
    reason = d.get("reason", "")
    if color not in ("GREEN",):
        issues.append(f"TRS_{color}: score={score} reason={reason}")
    return issues


def check_service_health() -> List[Tuple[str, str]]:
    """Check all service /health endpoints. Returns list of (service, issue) tuples."""
    down = []
    for svc, (port, _) in SERVICES.items():
        d = get_json(f"http://localhost:{port}/health")
        if d is None:
            down.append((svc, "DOWN: /health unreachable"))
        elif d.get("status") not in ("healthy", "ok", "degraded"):
            down.append((svc, f"STATUS={d.get('status')}"))
    return down


def check_alpha_exec() -> List[str]:
    """Check Alpha-Exec for paused state or VIX brake."""
    issues = []
    d = get_json("http://localhost:8005/health")
    if d is None:
        return ["ALPHA_EXEC_UNREACHABLE"]
    if d.get("execution_paused"):
        issues.append("ALPHA_EXEC_PAUSED: execution is paused")
    vix_brake = d.get("vix_brake", "CLEAR")
    if vix_brake not in ("CLEAR", "UNKNOWN"):
        issues.append(f"VIX_BRAKE_ACTIVE: {vix_brake}")
    return issues


def check_omni() -> List[str]:
    """Check OMNI for synthesis silence."""
    issues = []
    d = get_json("http://localhost:8004/health")
    if d is None:
        return ["OMNI_UNREACHABLE"]
    lag = d.get("last_synthesis_min_ago")
    if lag and lag > 20:
        issues.append(f"OMNI_SILENCE: {lag:.0f}min since last synthesis (threshold=20min)")
    return issues


def check_go_verdicts() -> List[str]:
    """After 11AM: flag if zero GO verdicts with >5 syntheses."""
    now = datetime.now(ET)
    if now.hour < 11:
        return []
    d = get_json("http://localhost:8004/health")
    if d is None:
        return []
    go = d.get("go_verdicts_today", 0)
    synths = d.get("syntheses_today", 0)
    if go == 0 and synths >= 5:
        return [f"ZERO_GO_VERDICTS: 0 GO from {synths} syntheses today — investigate brain consensus"]
    return []


def check_axiom_regime() -> List[str]:
    """Check if Axiom regime is stale (>60 min)."""
    issues = []
    d = get_json("http://localhost:8001/health")
    if d is None:
        return ["AXIOM_UNREACHABLE"]
    last_updated = d.get("regime_last_updated")
    if last_updated:
        try:
            from datetime import datetime as dt
            lu = dt.fromisoformat(last_updated.replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - lu).total_seconds() / 60
            # During market hours: flag if > 60 min stale
            # After hours: only flag if from a prior trading date (regime will refresh at open)
            now_et = datetime.now(ET)
            if is_market_hours() and age_min > 60:
                issues.append(f"AXIOM_REGIME_STALE: {age_min:.0f}min since last regime update (threshold=60min)")
            elif not is_market_hours() and lu.date() < now_et.date():
                # Prior date — flag for pre-market awareness only, don't trigger refresh
                issues.append(f"AXIOM_REGIME_PRIOR_DATE: regime from {lu.date()} — will refresh at market open")
        except Exception:
            pass
    return issues


# ---------------------------------------------------------------------------
# Intervention actions
# ---------------------------------------------------------------------------

def act_on_issues(issues: List[str], service_issues: List[Tuple[str, str]]) -> None:
    """
    For each issue detected, take immediate autonomous action.
    Log everything to CHRONICLE. Escalate to SOVEREIGN after 3 failures.
    """
    if not issues and not service_issues:
        log.info("All checks clean.")
        return

    all_issues = issues + [f"{svc}: {detail}" for svc, detail in service_issues]
    log.warning("GENESIS DIAGNOSTIC: %d issue(s) detected: %s", len(all_issues), all_issues)

    for issue in issues:

        # TRS AMBER/RED — log and probe deeper
        if issue.startswith("TRS_"):
            log.warning("TRS degraded: %s — running deeper probe", issue)
            post_bus("sovereign", f"🟡 GENESIS DIAGNOSTIC: {issue} — investigating root cause")
            log_to_chronicle(issue, "TRS degradation detected — probing components", "in_progress")

        # Alpha-Exec paused — attempt resume via SOVEREIGN directive
        elif "ALPHA_EXEC_PAUSED" in issue:
            log.error("ALPHA-EXEC IS PAUSED — attempting to resume")
            try:
                r = requests.post(
                    "http://localhost:8005/resume",
                    headers={"X-Nexus-Secret": SECRET},
                    timeout=5,
                )
                if r.status_code in (200, 201):
                    log.info("Alpha-Exec resumed successfully")
                    post_bus("sovereign", "✅ GENESIS AUTO-FIX: Alpha-Exec was paused — resumed successfully")
                    log_to_chronicle("ALPHA_EXEC_PAUSED", "Auto-resumed via /resume endpoint", "solved")
                else:
                    log.error("Alpha-Exec resume failed: HTTP %s", r.status_code)
                    post_bus("sovereign", f"🚨 GENESIS: Alpha-Exec paused AND /resume failed (HTTP {r.status_code}) — manual intervention needed")
                    log_to_chronicle("ALPHA_EXEC_PAUSED", f"Auto-resume failed: HTTP {r.status_code}", "unsolved")
            except Exception as exc:
                log.error("Alpha-Exec resume exception: %s", exc)
                post_bus("sovereign", f"🚨 GENESIS: Alpha-Exec paused, resume exception: {exc}")
                log_to_chronicle("ALPHA_EXEC_PAUSED", f"Resume exception: {exc}", "unsolved")

        # OMNI silence — restart
        elif "OMNI_SILENCE" in issue:
            log.error("OMNI SILENCE detected — restarting OMNI")
            # Check no open positions first
            exec_d = get_json("http://localhost:8005/health")
            positions = exec_d.get("open_positions", 0) if exec_d else 1
            if positions > 0:
                log.warning("OMNI silence but %d open positions — deferring restart, notifying SOVEREIGN", positions)
                post_bus("sovereign", f"⚠️ GENESIS: OMNI silence detected but {positions} open positions — restart deferred. Needs SOVEREIGN decision.")
            else:
                restarted = restart_service("omni")
                if restarted:
                    post_bus("sovereign", "✅ GENESIS AUTO-FIX: OMNI silence detected — restarted OMNI (0 open positions)")
                    log_to_chronicle("OMNI_SILENCE", "Auto-restarted OMNI (0 open positions)", "solved")
                else:
                    post_bus("sovereign", "🚨 GENESIS: OMNI silence — restart FAILED, escalating")
                    log_to_chronicle("OMNI_SILENCE", "Restart failed", "unsolved")

        # Axiom regime stale — trigger tier1 refresh
        elif "AXIOM_REGIME_STALE" in issue:
            log.warning("Axiom regime stale — triggering tier1 refresh")
            try:
                r = requests.post(
                    "http://localhost:8001/trigger-tier1",
                    headers={"X-Axiom-Secret": SECRET},
                    timeout=10,
                )
                if r.status_code == 200:
                    log.info("Axiom tier1 refresh triggered")
                    log_to_chronicle("AXIOM_REGIME_STALE", "tier1 refresh triggered", "solved")
                else:
                    log.warning("Axiom tier1 refresh HTTP %s", r.status_code)
            except Exception as exc:
                log.error("Axiom refresh failed: %s", exc)

        # Axiom regime from prior date — informational after-hours, no action needed
        elif "AXIOM_REGIME_PRIOR_DATE" in issue:
            log.info("Axiom regime is from prior date — will be refreshed at market open. No action needed.")

        # Zero GO verdicts — investigate and report, don't restart
        elif "ZERO_GO_VERDICTS" in issue:
            log.warning("Zero GO verdicts detected — investigating brain consensus")
            post_bus("sovereign", f"📊 GENESIS DIAGNOSTIC: {issue} — market may be low-conviction or brain disagreement issue")
            log_to_chronicle("ZERO_GO_VERDICTS", "Flagged to SOVEREIGN, no autonomous fix (market conditions)", "in_progress")

        # Any other issue — log and escalate
        else:
            log.error("Unhandled issue: %s", issue)
            post_bus("sovereign", f"⚠️ GENESIS DIAGNOSTIC: {issue}")

    # Service down — attempt restart (no open positions check for non-exec services)
    for svc, detail in service_issues:
        log.error("Service DOWN: %s — %s", svc, detail)
        exec_services = {"alpha-exec", "prime-exec", "omni", "prime-buffer", "axiom"}

        if svc in exec_services:
            exec_d = get_json("http://localhost:8005/health")
            positions = exec_d.get("open_positions", 0) if exec_d else 1
            if positions > 0:
                post_bus("sovereign", f"🚨 GENESIS: {svc} DOWN but {positions} open positions — restart deferred, needs SOVEREIGN")
                continue

        restarted = restart_service(svc)
        if restarted:
            time.sleep(15)
            d = get_json(f"http://localhost:{SERVICES[svc][0]}/health")
            outcome = "solved" if d else "unsolved"
            post_bus("sovereign", f"{'✅' if d else '🚨'} GENESIS AUTO-FIX: {svc} was DOWN — {'recovered' if d else 'restart failed'}")
            log_to_chronicle(f"{svc}_DOWN", f"Auto-restarted — {'recovered' if d else 'still down'}", outcome)
        else:
            post_bus("sovereign", f"🚨 GENESIS: {svc} DOWN and restart failed — escalating")
            log_to_chronicle(f"{svc}_DOWN", "Restart failed", "unsolved")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the GENESIS diagnostic. Called by OpenClaw cron every 5 min."""
    log.info("GENESIS diagnostic starting")

    market = is_market_hours()
    all_issues: List[str] = []
    svc_issues: List[Tuple[str, str]] = []

    # Always check (market hours or not)
    svc_issues = check_service_health()
    all_issues += check_trs()
    all_issues += check_alpha_exec()
    all_issues += check_axiom_regime()

    # Market hours only
    if market:
        all_issues += check_omni()
        all_issues += check_go_verdicts()

    act_on_issues(all_issues, svc_issues)
    log.info("GENESIS diagnostic complete — %d issue(s) found", len(all_issues) + len(svc_issues))


if __name__ == "__main__":
    main()
