#!/usr/bin/env python3
"""
atomic_deploy.py — Atomic Two-Phase Deploy Gate
================================================
RESILIENCE_SPEC_v2 Block 1 — Prevention-First

Implements a two-phase atomic deploy:
  Phase 1 (Validate): Stage code, run smoke tests. Abort on failure.
  Phase 2 (Swap):     Write lock, rsync, launchctl reload, health-gate.
                      If unhealthy after 15s → auto-rollback.

Usage:
    python scripts/atomic_deploy.py <service_name> <source_path>

Environment:
    NEXUS_SECRET          — Used to authenticate /health probes.
    TELEGRAM_BOT_TOKEN    — Telegram bot token for Ahmed notifications.
    TELEGRAM_AHMED_CHAT_ID — Ahmed's Telegram chat ID.
    NEXUS_ROOT            — Root of Nexus repo (default: /Users/ahmedsadek/nexus).

Exit codes:
    0 — Deploy succeeded.
    1 — Deploy failed or rolled back.
"""

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import logging
import pytz
from datetime import datetime, timezone, time as dtime
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s atomic_deploy: %(message)s",
)
log = logging.getLogger("atomic_deploy")

# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------

NEXUS_ROOT: str = os.environ.get("NEXUS_ROOT", "/Users/ahmedsadek/nexus")
NEXUS_SECRET: str = os.environ.get("NEXUS_SECRET", "")
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_AHMED_CHAT_ID: str = os.environ.get("TELEGRAM_AHMED_CHAT_ID", "")
CHRONICLE_DB: str = os.environ.get(
    "CHRONICLE_DB", f"{NEXUS_ROOT}/data/chronicle.db"
)

EXECUTION_SERVICES: frozenset = frozenset({"alpha-execution", "prime-execution", "alpha-buffer", "prime-buffer"})


class DeployFrozen(Exception):
    pass


def _in_market_hours() -> bool:
    """True if current ET time is within 09:29-16:01 Mon-Fri (1-min buffer each side)."""
    import datetime as _dt
    et = pytz.timezone("America/New_York")
    now = _dt.datetime.now(et)
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return False
    t = now.time()
    return dtime(9, 29) <= t <= dtime(16, 1)


# Port map: service_name → (live_port, test_port)
SERVICE_PORTS: dict[str, tuple[int, int]] = {
    "axiom":           (8001, 18001),
    "alpha-buffer":    (8002, 18002),
    "prime-buffer":    (8003, 18003),
    "omni":            (8004, 18004),
    "alpha-execution": (8005, 18005),
    "prime-execution": (8006, 18006),
    "oracle":          (8007, 18007),
    "ails":            (8008, 18008),
    "cipher":          (9001, 19001),
    "atlas":           (9002, 19002),
    "sage":            (9003, 19003),
}

HEALTH_PATH_MAP: dict[str, str] = {
    "oracle": "/ping",
}


# ---------------------------------------------------------------------------
# Telegram alert
# ---------------------------------------------------------------------------

def _telegram_alert(message: str) -> None:
    """Send a Telegram message to Ahmed. Best-effort — never raises.

    Args:
        message: Plain text message body.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_AHMED_CHAT_ID:
        log.warning("Telegram not configured — skipping alert")
        return
    try:
        import requests as _req
        _req.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_AHMED_CHAT_ID, "text": f"🚀 DEPLOY GATE\n\n{message}"},
            timeout=8,
        )
    except Exception as exc:
        log.warning("Telegram alert failed: %s", exc)


# ---------------------------------------------------------------------------
# CHRONICLE log
# ---------------------------------------------------------------------------

def _chronicle_log(service: str, phase: str, outcome: str, detail: str = "") -> None:
    """Write deploy record to CHRONICLE intervention_log table.

    Creates the table if it doesn't exist. Best-effort — never raises.

    Args:
        service: Service name being deployed.
        phase:   'phase1', 'phase2', 'rollback', etc.
        outcome: 'success', 'failure', 'rollback'.
        detail:  Optional detail string.
    """
    try:
        Path(CHRONICLE_DB).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(CHRONICLE_DB, timeout=5)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS intervention_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts TEXT NOT NULL, "
            "service TEXT NOT NULL, "
            "phase TEXT NOT NULL, "
            "outcome TEXT NOT NULL, "
            "detail TEXT, "
            "agent TEXT NOT NULL DEFAULT 'atomic_deploy'"
            ")"
        )
        conn.execute(
            "INSERT INTO intervention_log (ts, service, phase, outcome, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                service,
                phase,
                outcome,
                detail[:500] if detail else "",
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning("CHRONICLE write failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------

def _health_probe(port: int, health_path: str = "/health", timeout: float = 5.0) -> bool:
    """Probe the service health endpoint on the given port.

    Args:
        port:        Port to probe.
        health_path: HTTP path.
        timeout:     Request timeout in seconds.

    Returns:
        True if HTTP 200 received.
    """
    try:
        import requests as _req
        headers = {"X-Nexus-Secret": NEXUS_SECRET} if NEXUS_SECRET else {}
        resp = _req.get(
            f"http://localhost:{port}{health_path}",
            headers=headers,
            timeout=timeout,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _wait_for_health(
    port: int,
    health_path: str = "/health",
    timeout_s: int = 15,
    interval_s: float = 1.0,
) -> bool:
    """Poll the health endpoint until healthy or timeout.

    Args:
        port:       Port to probe.
        health_path: HTTP path.
        timeout_s:  Maximum seconds to wait.
        interval_s: Polling interval.

    Returns:
        True if service became healthy within timeout.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _health_probe(port, health_path):
            return True
        time.sleep(interval_s)
    return False


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test_import(staging_path: str) -> tuple[bool, str]:
    """Run 'python -c "import main"' in the staging directory.

    Args:
        staging_path: Path to the staging directory.

    Returns:
        Tuple of (success: bool, error_message: str).
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, '.'); import main"],
            cwd=staging_path,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PYTHONPATH": staging_path},
        )
        if result.returncode != 0:
            return False, (result.stderr or result.stdout)[:500]
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Import smoke test timed out after 30s"
    except Exception as exc:
        return False, str(exc)[:300]


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def _backup_service(service_path: str, backup_dir: str) -> Optional[str]:
    """Copy live service directory to a timestamped backup.

    Args:
        service_path: Absolute path to the live service directory.
        backup_dir:   Destination directory for the backup.

    Returns:
        Path to the backup directory, or None on failure.
    """
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        svc_name = Path(service_path).name
        dest = Path(backup_dir) / f"{svc_name}_{ts}"
        shutil.copytree(service_path, str(dest), ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "staging"))
        log.info("Backup created: %s", dest)
        return str(dest)
    except Exception as exc:
        log.error("Backup failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# launchctl helpers
# ---------------------------------------------------------------------------

def _launchctl_label(service_name: str) -> str:
    """Return the launchd label for a Nexus service.

    Args:
        service_name: Short service name (e.g. 'alpha-execution').

    Returns:
        launchd label string.
    """
    return f"ai.nexus.{service_name}"


def _launchctl_unload(service_name: str) -> bool:
    """Unload a launchd service.

    Args:
        service_name: Short service name.

    Returns:
        True if unload succeeded.
    """
    label = _launchctl_label(service_name)
    uid = os.getuid()
    try:
        result = subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{label}"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception as exc:
        log.warning("launchctl bootout failed for %s: %s", service_name, exc)
        return False


def _launchctl_load(service_name: str) -> bool:
    """Load a launchd service from its plist.

    Args:
        service_name: Short service name.

    Returns:
        True if load succeeded.
    """
    label = _launchctl_label(service_name)
    uid = os.getuid()
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if not plist_path.exists():
        log.error("Plist not found: %s", plist_path)
        return False
    try:
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception as exc:
        log.warning("launchctl bootstrap failed for %s: %s", service_name, exc)
        return False


# ---------------------------------------------------------------------------
# Core deploy logic
# ---------------------------------------------------------------------------

def deploy(service_name: str, source_path: str, force_market_hours: bool = False) -> bool:
    """Execute the two-phase atomic deploy for a service.

    Phase 1 — Validate:
        1. Copies source_path → service/staging/.
        2. Runs smoke test (python -c "import main") on staging.
        3. On failure → abort, delete staging, alert Ahmed.

    Phase 2 — Swap:
        1. Market hours freeze gate: rejects deploys of EXECUTION_SERVICES
           during 09:30–16:00 ET weekdays unless force_market_hours=True.
        2. Takes a backup of the live service directory.
        3. Writes .deploy_lock sentinel file.
        4. rsync staging → live.
        5. launchctl unload + load.
        6. Waits up to 15s for /health 200.
        7. Success: delete .deploy_lock, write CHRONICLE.
        8. Failure: rollback from backup, reload, alert Ahmed.

    Args:
        service_name:        Short service name (e.g. 'alpha-execution').
        source_path:         Path to the new code directory to deploy.
        force_market_hours:  If True, bypass market hours freeze (logs CRITICAL + Telegram alert).

    Returns:
        True if deploy succeeded, False if failed/rolled back.
    """
    source_path = str(Path(source_path).resolve())
    service_dir = Path(NEXUS_ROOT) / service_name
    staging_dir = service_dir / "staging"
    lock_file   = service_dir / ".deploy_lock"
    backup_dir  = Path(NEXUS_ROOT) / "data" / "deploy_backups"

    live_port   = SERVICE_PORTS.get(service_name, (8000,))[0]
    health_path = HEALTH_PATH_MAP.get(service_name, "/health")

    log.info("=== ATOMIC DEPLOY START: %s ===", service_name)
    log.info("Source: %s → %s", source_path, service_dir)

    # ── Phase 1: Validate ─────────────────────────────────────────────────────

    log.info("Phase 1: Staging and smoke test...")

    # Clean any previous staging dir
    if staging_dir.exists():
        shutil.rmtree(str(staging_dir))
    staging_dir.mkdir(parents=True, exist_ok=True)

    # Copy source → staging
    try:
        # rsync source into staging (handles large trees)
        result = subprocess.run(
            ["rsync", "-a", "--exclude=__pycache__", "--exclude=*.pyc",
             f"{source_path.rstrip('/')}/", f"{staging_dir}/"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout)[:300]
            log.error("Phase 1 FAILED: rsync to staging: %s", err)
            _telegram_alert(
                f"Deploy REJECTED — {service_name}\n"
                f"Staging rsync failed: {err}\n"
                f"Current version still running."
            )
            _chronicle_log(service_name, "phase1", "failure", f"rsync failed: {err}")
            shutil.rmtree(str(staging_dir), ignore_errors=True)
            return False
    except Exception as exc:
        log.error("Phase 1 FAILED: staging copy exception: %s", exc)
        _telegram_alert(
            f"Deploy REJECTED — {service_name}\n"
            f"Staging failed: {exc}\n"
            f"Current version still running."
        )
        shutil.rmtree(str(staging_dir), ignore_errors=True)
        return False

    # Smoke test: python -c "import main"
    import_ok, import_err = _smoke_test_import(str(staging_dir))
    if not import_ok:
        log.error("Phase 1 FAILED: smoke test failed: %s", import_err)
        _telegram_alert(
            f"Deploy REJECTED — {service_name}\n"
            f"Staging smoke test failed:\n{import_err[:300]}\n"
            f"Current version still running."
        )
        _chronicle_log(service_name, "phase1", "failure", f"smoke_test_failed: {import_err[:200]}")
        shutil.rmtree(str(staging_dir), ignore_errors=True)
        return False

    log.info("Phase 1 PASSED: smoke test OK")

    # ── Market Hours Freeze Gate ───────────────────────────────────────────────
    # Execution services (alpha/prime execution + buffers) must NOT be restarted
    # during live market hours (09:30–16:00 ET, Mon–Fri). A mid-market restart
    # wipes in-memory position state and can cause phantom P&L / ghost closes.
    if service_name in EXECUTION_SERVICES and _in_market_hours():
        et_tz = pytz.timezone("America/New_York")
        et_time = datetime.now(et_tz).strftime("%H:%M:%S ET")
        if not force_market_hours:
            log.critical(
                "DEPLOY REJECTED: %s is a live execution service — market hours freeze active at %s",
                service_name, et_time,
            )
            _chronicle_log(service_name, "phase2", "frozen",
                           f"market_hours_freeze at {et_time}")
            raise DeployFrozen(
                f"DEPLOY REJECTED: {service_name} is a live execution service. "
                f"Market hours freeze active 09:30–16:00 ET. "
                f"Current time: {et_time}. Deploy after market close."
            )
        else:
            log.critical(
                "MARKET HOURS DEPLOY OVERRIDE: deploying %s during market hours at %s "
                "(--force-market-hours / NEXUS_FORCE_MARKET_DEPLOY). PROCEED WITH EXTREME CAUTION.",
                service_name, et_time,
            )
            _telegram_alert(
                f"⚠️ MARKET HOURS DEPLOY OVERRIDE\n"
                f"Service: {service_name}\n"
                f"Time: {et_time}\n"
                f"Market hours freeze bypassed via --force-market-hours.\n"
                f"MANUAL OVERRIDE — ensure this is intentional!"
            )

    # ── Phase 2: Swap ─────────────────────────────────────────────────────────

    log.info("Phase 2: Backup → lock → rsync → reload → health-gate...")

    # Backup current live code
    backup_path = _backup_service(str(service_dir), str(backup_dir))
    if not backup_path:
        log.error("Phase 2 FAILED: could not create backup — aborting")
        _telegram_alert(
            f"Deploy ABORTED — {service_name}\n"
            f"Backup creation failed. Deploy cancelled for safety."
        )
        shutil.rmtree(str(staging_dir), ignore_errors=True)
        return False

    # Write deploy lock
    try:
        lock_file.write_text(datetime.now(timezone.utc).isoformat())
        log.info("Deploy lock written: %s", lock_file)
    except Exception as exc:
        log.warning("Could not write deploy lock (non-fatal): %s", exc)

    # rsync staging → live
    try:
        result = subprocess.run(
            ["rsync", "-a", "--exclude=staging", "--exclude=__pycache__",
             "--exclude=*.pyc", "--delete",
             f"{staging_dir}/", f"{service_dir}/"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout)[:300])
        log.info("Live code updated via rsync")
    except Exception as exc:
        log.error("Phase 2 FAILED: live rsync: %s", exc)
        _rollback(service_name, service_dir, backup_path, live_port, health_path)
        lock_file.unlink(missing_ok=True)
        shutil.rmtree(str(staging_dir), ignore_errors=True)
        return False

    # launchctl unload → load
    log.info("Reloading service via launchctl...")
    _launchctl_unload(service_name)
    time.sleep(1)
    _launchctl_load(service_name)
    log.info("Service reloaded — waiting up to 15s for /health 200...")

    # Wait up to 15s for health
    healthy = _wait_for_health(live_port, health_path, timeout_s=15)

    if healthy:
        # Success path
        lock_file.unlink(missing_ok=True)
        shutil.rmtree(str(staging_dir), ignore_errors=True)
        log.info("=== DEPLOY SUCCESS: %s ===", service_name)
        _chronicle_log(service_name, "phase2", "success",
                       f"deployed_from={source_path} backup={backup_path}")
        _telegram_alert(
            f"✅ Deploy SUCCESS — {service_name}\n"
            f"Service is healthy on port {live_port}."
        )
        return True
    else:
        # Health gate failed → rollback
        log.error("Health gate FAILED after 15s — initiating rollback")
        _telegram_alert(
            f"❌ Deploy FAILED — {service_name}\n"
            f"Service unhealthy after 15s. AUTO-ROLLBACK in progress."
        )
        rolled_back = _rollback(service_name, service_dir, backup_path, live_port, health_path)
        lock_file.unlink(missing_ok=True)
        shutil.rmtree(str(staging_dir), ignore_errors=True)
        _chronicle_log(service_name, "phase2", "rollback",
                       f"health_gate_failed rollback={'ok' if rolled_back else 'failed'}")
        return False


def _rollback(
    service_name: str,
    service_dir: Path,
    backup_path: str,
    live_port: int,
    health_path: str,
) -> bool:
    """Restore service from backup and reload.

    Args:
        service_name: Short service name.
        service_dir:  Path to live service directory.
        backup_path:  Path to backup directory created in Phase 1.
        live_port:    Service's live port.
        health_path:  Health check path.

    Returns:
        True if service came back healthy after rollback.
    """
    log.warning("ROLLBACK: restoring %s from %s", service_name, backup_path)
    try:
        # Remove current (bad) code, restore from backup
        # Preserve .env and data dirs — only replace *.py
        result = subprocess.run(
            ["rsync", "-a", "--exclude=.env", "--exclude=*.db",
             "--exclude=staging", "--exclude=__pycache__",
             f"{backup_path.rstrip('/')}/", f"{service_dir}/"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            log.error("ROLLBACK rsync failed: %s", (result.stderr or result.stdout)[:200])
    except Exception as exc:
        log.error("ROLLBACK exception: %s", exc)

    _launchctl_unload(service_name)
    time.sleep(1)
    _launchctl_load(service_name)

    healthy = _wait_for_health(live_port, health_path, timeout_s=20)
    if healthy:
        log.info("ROLLBACK SUCCESS: %s is healthy again", service_name)
        _telegram_alert(
            f"↩️ ROLLBACK SUCCESS — {service_name}\n"
            f"Previous version restored and healthy."
        )
    else:
        log.error("ROLLBACK FAILED: %s still unhealthy — MANUAL INTERVENTION REQUIRED", service_name)
        _telegram_alert(
            f"🆘 ROLLBACK FAILED — {service_name}\n"
            f"Service still unhealthy after rollback.\n"
            f"MANUAL INTERVENTION REQUIRED immediately."
        )
    return healthy


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for atomic_deploy.py.

    Usage: python scripts/atomic_deploy.py <service_name> <source_path>
    """
    parser = argparse.ArgumentParser(
        description="Atomic two-phase deploy gate for Nexus services.",
    )
    parser.add_argument("service_name", help="Service name (e.g. alpha-execution)")
    parser.add_argument("source_path", help="Path to new code directory to deploy")
    parser.add_argument(
        "--force-market-hours",
        action="store_true",
        default=False,
        help="Override market hours freeze gate for execution services (USE WITH EXTREME CAUTION)",
    )
    args = parser.parse_args()

    if args.service_name not in SERVICE_PORTS:
        log.warning(
            "Service '%s' not in SERVICE_PORTS map — live port unknown. "
            "Health gate will use port 8000.",
            args.service_name,
        )

    force = args.force_market_hours or os.environ.get("NEXUS_FORCE_MARKET_DEPLOY", "").lower() == "true"
    success = deploy(args.service_name, args.source_path, force_market_hours=force)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
