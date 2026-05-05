#!/usr/bin/env python3
"""
post_deploy_check.py — Deploy-Without-Restart Gate (P2-C)

After every git push/merge, compares each service's live code_hash
(from /health endpoint) against the MD5 of its current main.py on disk.

If they diverge → launchctl unload + load the service plist.
Then fires an OpenClaw system event notifying VECTOR.

Idempotent: safe to run multiple times. Only restarts services that are stale.

Author: GENESIS | Tasked by VECTOR | 2026-05-01
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_PATH = "/Users/ahmedsadek/nexus/logs/post-deploy/post_deploy_check.log"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [post-deploy] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("post_deploy_check")

# ── Constants ─────────────────────────────────────────────────────────────────
NEXUS_SECRET = os.environ.get(
    "NEXUS_SECRET",
    "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2",
)

NEXUS_ROOT = "/Users/ahmedsadek/nexus"

# The 7 target services per spec (ports 8001-8007)
TARGET_SERVICES = {
    "axiom":           {"port": 8001, "plist": "ai.nexus.axiom"},
    "alpha-buffer":    {"port": 8002, "plist": "ai.nexus.alpha-buffer"},
    "prime-buffer":    {"port": 8003, "plist": "ai.nexus.prime-buffer"},
    "omni":            {"port": 8004, "plist": "ai.nexus.omni"},
    "alpha-execution": {"port": 8005, "plist": "ai.nexus.alpha-execution"},
    "prime-execution": {"port": 8006, "plist": "ai.nexus.prime-execution"},
    "oracle":          {"port": 8007, "plist": "ai.nexus.oracle"},
}

# Plist directory (LaunchAgents for user-level services)
PLIST_DIR = os.path.expanduser("~/Library/LaunchAgents")

# Health check timeout (seconds)
HEALTH_TIMEOUT = 5

# How long to wait for a service to come back after restart
RESTART_VERIFY_WAIT = 8

# OpenClaw message bus for VECTOR notification
OPENCLAW_BUS_URL = "http://192.168.1.141:9999/send"


# ── Data ──────────────────────────────────────────────────────────────────────
@dataclass
class ServiceCheckResult:
    """Result of checking a single service."""
    name: str
    port: int
    disk_hash: Optional[str]       # MD5 of main.py on disk (first 8 hex chars)
    live_hash: Optional[str]       # code_hash from /health
    stale: bool                    # True if hashes diverge or service unreachable with stale main.py
    restarted: bool                # True if we fired a restart
    restart_ok: Optional[bool]     # True/False/None (None = not restarted)
    error: Optional[str]           # Any error message


# ── Hash ──────────────────────────────────────────────────────────────────────
def compute_disk_hash(service_name: str) -> Optional[str]:
    """
    Compute MD5 of ALL *.py files in the service directory (excluding __pycache__).
    Uses the same algorithm services themselves use to compute _CODE_HASH at startup.
    Returns first 8 hex chars, or None if directory missing.

    IMPORTANT: Must match the _compute_module_hash() logic in each service's main.py.
    Services hash all *.py files sorted — not just main.py.
    """
    import glob
    svc_dir = os.path.join(NEXUS_ROOT, service_name)
    if not os.path.isdir(svc_dir):
        log.warning(f"[{service_name}] service directory not found: {svc_dir}")
        return None
    py_files = sorted(
        f for f in glob.glob(os.path.join(svc_dir, "*.py"))
        if "__pycache__" not in f
    )
    if not py_files:
        log.warning(f"[{service_name}] no .py files found in {svc_dir}")
        return None
    try:
        h = hashlib.md5()
        for py_file in py_files:
            with open(py_file, "rb") as f:
                h.update(f.read())
        return h.hexdigest()[:8]
    except OSError as e:
        log.error(f"[{service_name}] Failed to read .py files: {e}")
        return None


# ── Health ────────────────────────────────────────────────────────────────────
def fetch_live_hash(service_name: str, port: int) -> Optional[str]:
    """
    Fetch code_hash from /health endpoint.
    Returns the hash string or None if unreachable/missing.
    """
    url = f"http://localhost:{port}/health"
    headers = {"X-Nexus-Secret": NEXUS_SECRET}
    try:
        resp = requests.get(url, headers=headers, timeout=HEALTH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("code_hash") or data.get("_CODE_HASH")
    except requests.exceptions.ConnectionError:
        log.warning(f"[{service_name}] /health unreachable (port {port})")
        return None
    except requests.exceptions.Timeout:
        log.warning(f"[{service_name}] /health timeout after {HEALTH_TIMEOUT}s")
        return None
    except Exception as e:
        log.error(f"[{service_name}] /health error: {e}")
        return None


# ── Restart ───────────────────────────────────────────────────────────────────
def restart_service(service_name: str, plist_label: str) -> bool:
    """
    Restart a launchd service via launchctl unload + load.
    Returns True if reload succeeded (no subprocess error).
    """
    plist_path = os.path.join(PLIST_DIR, f"{plist_label}.plist")
    if not os.path.isfile(plist_path):
        log.error(f"[{service_name}] Plist not found: {plist_path}")
        return False

    log.info(f"[{service_name}] Unloading plist: {plist_label}")
    unload = subprocess.run(
        ["launchctl", "unload", plist_path],
        capture_output=True, text=True
    )
    if unload.returncode != 0:
        log.warning(f"[{service_name}] launchctl unload warning: {unload.stderr.strip()}")
    # Small gap to let the port release
    time.sleep(1)

    log.info(f"[{service_name}] Loading plist: {plist_label}")
    load = subprocess.run(
        ["launchctl", "load", plist_path],
        capture_output=True, text=True
    )
    if load.returncode != 0:
        log.error(f"[{service_name}] launchctl load failed: {load.stderr.strip()}")
        return False

    log.info(f"[{service_name}] Restart issued. Waiting {RESTART_VERIFY_WAIT}s for service to come up…")
    return True


def verify_restart(service_name: str, port: int, expected_hash: str) -> bool:
    """
    After restart, wait then check /health. Confirm code_hash matches disk_hash.
    Returns True if service is up and hash matches.
    """
    time.sleep(RESTART_VERIFY_WAIT)
    live = fetch_live_hash(service_name, port)
    if live is None:
        log.error(f"[{service_name}] Still unreachable after restart")
        return False
    if live == expected_hash:
        log.info(f"[{service_name}] ✅ Hash confirmed post-restart: {live}")
        return True
    log.warning(f"[{service_name}] Hash mismatch post-restart: live={live} expected={expected_hash}")
    return False


# ── Notify VECTOR ─────────────────────────────────────────────────────────────
def notify_vector(results: list[ServiceCheckResult]) -> None:
    """
    Fire OpenClaw system event to VECTOR via message bus for each restarted service.
    Also fires a summary even if nothing was restarted.
    """
    restarted = [r for r in results if r.restarted]
    clean = [r for r in results if not r.restarted and not r.error]
    errors = [r for r in results if r.error]

    lines = ["🌱 **GENESIS — Post-Deploy Gate Report**"]

    if restarted:
        lines.append(f"\n🔄 **Restarted ({len(restarted)}):**")
        for r in restarted:
            status = "✅ ok" if r.restart_ok else "❌ failed"
            lines.append(
                f"  • `{r.name}` — old_hash=`{r.live_hash}` → new_hash=`{r.disk_hash}` | restart {status}"
            )
    else:
        lines.append("\n✅ All services in sync — no restarts needed.")

    if errors:
        lines.append(f"\n⚠️ **Errors ({len(errors)}):**")
        for r in errors:
            lines.append(f"  • `{r.name}`: {r.error}")

    lines.append(f"\n_Checked {len(results)} services | clean={len(clean)} | restarted={len(restarted)} | errors={len(errors)}_")

    message = "\n".join(lines)
    log.info(f"VECTOR notification:\n{message}")

    # Fire to message bus
    payload = {
        "from": "genesis",
        "to": "vector",
        "message": message,
    }
    try:
        resp = requests.post(
            OPENCLAW_BUS_URL,
            json=payload,
            timeout=5,
        )
        if resp.status_code == 200:
            log.info("VECTOR notified via message bus ✅")
        else:
            log.warning(f"Message bus returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log.warning(f"Message bus unreachable: {e} — notification logged above")

    # Also send via openclaw sessions_send as fallback (will be caught by VECTOR session)
    try:
        openclaw_payload = {
            "sessionKey": "agent:vector:main",
            "message": message,
        }
        # Use openclaw CLI if available
        subprocess.run(
            ["openclaw", "message", "--session", "agent:vector:main", "--text", message],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        pass  # Bus notification above is primary; this is best-effort


# ── Core check ────────────────────────────────────────────────────────────────
def check_service(name: str, config: dict) -> ServiceCheckResult:
    """
    Check a single service. Returns a ServiceCheckResult.
    """
    port = config["port"]
    plist = config["plist"]

    disk_hash = compute_disk_hash(name)
    live_hash = fetch_live_hash(name, port)

    # Determine if stale
    stale = False
    error = None

    if disk_hash is None:
        error = "main.py not found on disk"
        return ServiceCheckResult(
            name=name, port=port,
            disk_hash=None, live_hash=live_hash,
            stale=False, restarted=False, restart_ok=None,
            error=error,
        )

    if live_hash is None:
        # Service is down — attempt restart if we have a disk hash
        log.warning(f"[{name}] Service unreachable — attempting restart to load current code")
        stale = True
    elif live_hash != disk_hash:
        log.info(f"[{name}] STALE: live={live_hash} disk={disk_hash} → will restart")
        stale = True
    else:
        log.info(f"[{name}] IN SYNC: hash={live_hash}")

    if not stale:
        return ServiceCheckResult(
            name=name, port=port,
            disk_hash=disk_hash, live_hash=live_hash,
            stale=False, restarted=False, restart_ok=None,
            error=None,
        )

    # Restart
    restart_issued = restart_service(name, plist)
    if not restart_issued:
        return ServiceCheckResult(
            name=name, port=port,
            disk_hash=disk_hash, live_hash=live_hash,
            stale=True, restarted=True, restart_ok=False,
            error="launchctl reload failed",
        )

    restart_ok = verify_restart(name, port, disk_hash)
    return ServiceCheckResult(
        name=name, port=port,
        disk_hash=disk_hash, live_hash=live_hash,
        stale=True, restarted=True, restart_ok=restart_ok,
        error=None if restart_ok else "service still stale after restart",
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    """
    Run post-deploy hash check for all 7 target services.
    Returns exit code: 0 = all good (or synced), 1 = one or more failures.
    """
    log.info("=" * 60)
    log.info("POST-DEPLOY GATE — starting hash check for 7 services")
    log.info("=" * 60)

    results: list[ServiceCheckResult] = []
    for name, config in TARGET_SERVICES.items():
        result = check_service(name, config)
        results.append(result)

    # Summary
    log.info("-" * 60)
    restarted = [r for r in results if r.restarted]
    failed = [r for r in results if r.restart_ok is False]
    errors = [r for r in results if r.error and not r.restarted]

    log.info(f"Summary: {len(results)} checked | {len(restarted)} restarted | {len(failed)} restart-failed | {len(errors)} errors")

    # Notify VECTOR regardless of outcome (clean runs also reported)
    notify_vector(results)

    if failed or errors:
        log.error("POST-DEPLOY GATE: one or more services failed — see above")
        return 1

    log.info("POST-DEPLOY GATE: complete ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
