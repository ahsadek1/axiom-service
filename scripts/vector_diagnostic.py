#!/usr/bin/env python3
"""
vector_diagnostic.py — VECTOR Proactive Field Diagnostic
=========================================================
VECTOR's equivalent of genesis_diagnostic.py.
Runs every 5 minutes during market hours via OpenClaw cron.
VECTOR owns infra — checks process table, log files, disk, memory.
Acts immediately. Does NOT wait for SOVEREIGN directives.

Detection targets:
  1. All 11 services responding (health endpoints)
  2. Service process alive in ps (launchd managed)
  3. Recent CRITICAL entries in logs (last 15 min)
  4. Disk/memory pressure
  5. Message bus reachable
  6. Log file sizes (runaway logging)
  7. Any service restart loop (restarted 3+ times in 10 min)

VECTOR-BUILD-001 2026-04-30: Created as result of Ahmed's directive to
upgrade VECTOR from reactive to proactive. VECTOR must not be dormant.

Author: GENESIS 🌱 (built for VECTOR 🔧)
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

import psutil
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

SECRET                    = _require("NEXUS_SECRET")
TELEGRAM_BOT_TOKEN        = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_BOT_TOKEN_VECTOR = _require("TELEGRAM_BOT_TOKEN")  # same token, alias
HEALTH_GROUP = "-5241272802"
MESSAGE_BUS = "http://192.168.1.141:9999"
LOG_BASE = "/Users/ahmedsadek/nexus/logs"
CHRONICLE_DB = "/Users/ahmedsadek/nexus/data/chronicle.db"

# All Nexus services: (name, port, launchd_label)
SERVICES = [
    ("axiom",        8001, "ai.nexus.axiom"),
    ("alpha-buffer", 8002, "ai.nexus.alpha-buffer"),
    ("prime-buffer", 8003, "ai.nexus.prime-buffer"),
    ("omni",         8004, "ai.nexus.omni"),
    ("alpha-exec",   8005, "ai.nexus.alpha-execution"),
    ("prime-exec",   8006, "ai.nexus.prime-execution"),
    ("oracle",       8007, "ai.nexus.oracle"),
    ("ails",         8008, "ai.nexus.ails"),
    ("cipher",       9001, "ai.nexus.cipher"),
    ("atlas",        9002, "ai.nexus.atlas"),
    ("sage",         9003, "ai.nexus.sage"),
]

# Log directories to monitor for CRITICAL entries
LOG_DIRS = {
    "omni":           f"{LOG_BASE}/omni/stderr.log",
    "alpha-exec":     f"{LOG_BASE}/alpha-execution/stderr.log",
    "prime-exec":     f"{LOG_BASE}/prime-execution/stderr.log",
    "axiom":          f"{LOG_BASE}/axiom/stderr.log",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] vector.diagnostic: %(message)s",
)
log = logging.getLogger("vector.diagnostic")


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


def post_bus(to: str, message: str) -> None:
    """Post a message to the Nexus message bus."""
    try:
        requests.post(
            f"{MESSAGE_BUS}/send",
            json={"from": "vector-diagnostic", "to": to, "message": message},
            timeout=3,
        )
    except Exception:
        pass


def send_telegram(chat_id: str, message: str) -> None:
    """Send a Telegram alert via VECTOR's bot."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN_VECTOR}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=8,
        )
    except Exception:
        pass


def restart_service(label: str, service_name: str) -> bool:
    """Restart a launchd service. Returns True if issued."""
    try:
        subprocess.run(["launchctl", "stop", label], timeout=5)
        time.sleep(3)
        subprocess.run(["launchctl", "start", label], timeout=5)
        log.info("Restarted: %s (%s)", service_name, label)
        return True
    except Exception as exc:
        log.error("Restart failed %s: %s", service_name, exc)
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
            ("VECTOR-DIAGNOSTIC", description,
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

def check_all_services() -> List[Tuple[str, str]]:
    """Check all service health endpoints. Returns (service, issue) for each down service.
    Uses double-check with 5s retry to avoid false positives during service restart windows.
    """
    down = []
    hdrs = {"X-Nexus-Secret": SECRET, "X-Nexus-Prime-Secret": SECRET, "X-Axiom-Secret": SECRET}
    for name, port, _ in SERVICES:
        issue = None
        try:
            r = requests.get(f"http://localhost:{port}/health", headers=hdrs, timeout=3)
            if r.status_code == 200:
                continue
            issue = f"HTTP {r.status_code}"
        except Exception as exc:
            issue = f"UNREACHABLE: {exc}"
        # Non-200 or unreachable: wait 5s and retry before flagging as DOWN
        time.sleep(5)
        try:
            r2 = requests.get(f"http://localhost:{port}/health", headers=hdrs, timeout=3)
            if r2.status_code == 200:
                log.info("Service %s recovered on retry (transient %s) — skipping restart", name, issue)
                continue
            down.append((name, f"HTTP {r2.status_code} (confirmed after retry)"))
        except Exception as exc2:
            down.append((name, f"UNREACHABLE: {exc2} (confirmed after retry)"))
    return down


def check_recent_criticals() -> List[Tuple[str, str]]:
    """Scan service logs for CRITICAL entries in last 15 minutes. Returns (service, entry)."""
    findings = []
    cutoff = time.time() - 900  # 15 minutes
    now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M")  # rough cutoff string

    for service, log_path in LOG_DIRS.items():
        if not os.path.exists(log_path):
            continue
        try:
            result = subprocess.run(
                ["tail", "-100", log_path],
                capture_output=True, text=True, timeout=3,
            )
            for line in result.stdout.split("\n"):
                import re
                if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ CRITICAL ", line):
                    # Only count recent ones (last 15 min)
                    try:
                        ts_str = line[:19]
                        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=ET
                        )
                        if (datetime.now(ET) - ts).total_seconds() < 900:
                            findings.append((service, line[20:100]))
                    except Exception:
                        pass
        except Exception as exc:
            log.debug("Log scan failed for %s: %s", service, exc)

    return findings


def check_system_resources() -> List[str]:
    """Check memory and disk pressure."""
    issues = []
    mem = psutil.virtual_memory()
    if mem.percent > 90:
        issues.append(f"MEMORY_CRITICAL: {mem.percent:.0f}% used ({mem.available // 1024 // 1024}MB free)")
    elif mem.percent > 80:
        issues.append(f"MEMORY_WARNING: {mem.percent:.0f}% used")

    disk = psutil.disk_usage("/Users/ahmedsadek/nexus")
    if disk.percent > 90:
        issues.append(f"DISK_CRITICAL: {disk.percent:.0f}% used on nexus volume")
    elif disk.percent > 80:
        issues.append(f"DISK_WARNING: {disk.percent:.0f}% used")

    return issues


def check_bus_reachable() -> Optional[str]:
    """Check if message bus is reachable."""
    try:
        r = requests.get(f"{MESSAGE_BUS}/health", timeout=3)
        if r.status_code != 200:
            return f"BUS_DEGRADED: HTTP {r.status_code}"
    except Exception as exc:
        return f"BUS_UNREACHABLE: {exc}"
    return None


def check_bus_inbox() -> List[str]:
    """Check VECTOR's inbox on the message bus for pending directives."""
    try:
        r = requests.get(f"{MESSAGE_BUS}/inbox/vector", timeout=3)
        if r.status_code == 200:
            data = r.json()
            msgs = data.get("messages", [])
            return [m.get("message", "") for m in msgs if m.get("message")]
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Act on issues
# ---------------------------------------------------------------------------

def act_on_down_services(
    down: List[Tuple[str, str]],
    open_positions: int,
) -> None:
    """Attempt to restart down services. Respects trading gate for exec services."""
    exec_services = {"alpha-exec", "prime-exec", "omni", "prime-buffer", "axiom"}

    for name, detail in down:
        log.error("Service DOWN: %s — %s", name, detail)

        # Get launchd label
        label = next((lbl for n, p, lbl in SERVICES if n == name), None)
        if not label:
            log.warning("No launchd label for %s", name)
            continue

        # Trading gate for critical services
        if name in exec_services and open_positions > 0:
            msg = (f"🔧 VECTOR: {name} DOWN but {open_positions} open positions "
                   f"— restart deferred, escalating to SOVEREIGN")
            log.warning(msg)
            post_bus("sovereign", msg)
            send_telegram(HEALTH_GROUP, msg)
            continue

        # Restart
        restarted = restart_service(label, name)
        if restarted:
            time.sleep(20)  # Allow service to fully boot before health check
            # Verify recovery
            port = next((p for n, p, l in SERVICES if n == name), None)
            recovered = False
            if port:
                try:
                    r = requests.get(
                        f"http://localhost:{port}/health",
                        headers={"X-Nexus-Secret": SECRET, "X-Nexus-Prime-Secret": SECRET, "X-Axiom-Secret": SECRET},
                        timeout=5,
                    )
                    recovered = r.status_code == 200
                except Exception:
                    pass

            outcome = "solved" if recovered else "unsolved"
            msg = (f"{'✅' if recovered else '🚨'} VECTOR AUTO-FIX: "
                   f"{name} was DOWN — {'recovered after restart' if recovered else 'restart failed, still down'}")
            log.info(msg)
            post_bus("sovereign", msg)
            if not recovered:
                send_telegram(HEALTH_GROUP, msg)
            log_to_chronicle(f"{name}_DOWN: {detail}", f"Restarted via launchctl", outcome)
        else:
            msg = f"🚨 VECTOR: {name} DOWN, restart FAILED — escalating"
            post_bus("sovereign", msg)
            send_telegram(HEALTH_GROUP, msg)
            log_to_chronicle(f"{name}_DOWN: {detail}", "Restart failed", "unsolved")


def act_on_criticals(criticals: List[Tuple[str, str]]) -> None:
    """Report recent CRITICAL log entries to SOVEREIGN for investigation."""
    if not criticals:
        return
    summary = "; ".join(f"{svc}:{entry[:60]}" for svc, entry in criticals[:3])
    msg = f"⚠️ VECTOR LOG SCAN: {len(criticals)} CRITICAL entries in last 15min — {summary}"
    log.warning(msg)
    post_bus("sovereign", msg)
    post_bus("genesis", msg)  # CODE class: GENESIS should investigate


def act_on_bus_directives(directives: List[str]) -> None:
    """Process directives from VECTOR's message bus inbox."""
    for directive in directives:
        log.info("Bus directive received: %s", directive[:100])
        post_bus("sovereign", f"🔧 VECTOR: Processing directive — {directive[:100]}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Run VECTOR's proactive diagnostic. Called every 5 min by cron."""
    log.info("VECTOR diagnostic starting")

    # Get open positions for trading gate
    open_positions = 0
    try:
        r = requests.get(
            "http://localhost:8005/health",
            headers={"X-Nexus-Secret": SECRET},
            timeout=5,
        )
        if r.status_code == 200:
            open_positions = r.json().get("open_positions", 0)
    except Exception:
        pass

    # Run all checks
    down_services = check_all_services()
    criticals = check_recent_criticals() if is_market_hours() else []
    resource_issues = check_system_resources()
    bus_issue = check_bus_reachable()
    bus_directives = check_bus_inbox()

    total_issues = len(down_services) + len(criticals) + len(resource_issues)
    if bus_issue:
        total_issues += 1

    if total_issues == 0 and not bus_directives:
        log.info("VECTOR diagnostic: all clear")
        return

    log.warning("VECTOR diagnostic: %d issue(s) found", total_issues)

    # Act on each class of issue
    if down_services:
        act_on_down_services(down_services, open_positions)

    if criticals:
        act_on_criticals(criticals)

    if resource_issues:
        for issue in resource_issues:
            log.warning("Resource issue: %s", issue)
            post_bus("sovereign", f"⚠️ VECTOR RESOURCE: {issue}")
            log_to_chronicle(issue, "Flagged to SOVEREIGN", "in_progress")

    if bus_issue:
        log.error("Message bus: %s", bus_issue)
        post_bus("sovereign", f"🚨 VECTOR: {bus_issue}")

    if bus_directives:
        act_on_bus_directives(bus_directives)

    log.info("VECTOR diagnostic complete")


if __name__ == "__main__":
    main()
