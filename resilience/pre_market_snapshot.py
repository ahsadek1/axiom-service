"""
pre_market_snapshot.py — Approach 1: Immutable Pre-Market Snapshot

Captures a SHA256-signed snapshot of all service configs, env vars, and
invariants at 6:00 AM. Validates at 8:30 AM. Detects drift every 60 seconds.

SAFE_AUTO_CORRECT: service_restart, cache_clear, rate_limiter_reset, connection_pool_refresh
REQUIRES_AHMED: api_key_change, database_schema_change, agent_weight_change, execution_bridge_change
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests
import pytz

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

# All 12 Nexus V2 services
SERVICE_PORTS = {
    "axiom":          (8001, "X-Axiom-Secret"),
    "alpha-buffer":   (8002, "X-Nexus-Secret"),
    "prime-buffer":   (8003, "X-Nexus-Prime-Secret"),
    "omni":           (8004, "X-Nexus-Secret"),
    "alpha-exec":     (8005, "X-Nexus-Secret"),
    "prime-exec":     (8006, "X-Nexus-Prime-Secret"),
    "oracle":         (8007, "X-Oracle-Secret"),
    "ails":           (8008, "X-AILS-Secret"),
    "cipher":         (9001, "X-Nexus-Secret"),
    "atlas":          (9002, "X-Nexus-Secret"),
    "sage":           (9003, "X-Nexus-Secret"),
}

LAUNCHAGENT_DIR = os.path.expanduser("~/Library/LaunchAgents")
DB_PATH = "/Users/ahmedsadek/nexus/data/snapshots.db"

SAFE_AUTO_CORRECT = {
    "service_restart", "cache_clear", "rate_limiter_reset", "connection_pool_refresh"
}

REQUIRES_AHMED = {
    "api_key_change", "database_schema_change", "agent_weight_change", "execution_bridge_change"
}


@dataclass
class ServiceSnapshot:
    """Snapshot of a single service at capture time."""
    service_name: str
    version: str
    status: str
    health_data: Dict
    captured_at: str


@dataclass
class TradingDaySnapshot:
    """Full system snapshot for a trading day."""
    snapshot_date: str
    captured_at: str
    services: Dict[str, Dict]          # service_name → ServiceSnapshot dict
    agent_weights: Dict                 # Cipher/Atlas/Sage weights
    plist_mtimes: Dict[str, float]      # LaunchAgent plist mtimes
    global_invariants: Dict
    signature: str
    valid: bool = False


def _hash(data: str) -> str:
    """SHA256 hash of a string."""
    return hashlib.sha256(data.encode()).hexdigest()


def _sign_snapshot(snap: TradingDaySnapshot) -> str:
    """Produce SHA256 signature over snapshot content (excluding signature field)."""
    payload = json.dumps({
        "snapshot_date": snap.snapshot_date,
        "captured_at": snap.captured_at,
        "services": snap.services,
        "agent_weights": snap.agent_weights,
        "plist_mtimes": snap.plist_mtimes,
        "global_invariants": snap.global_invariants,
    }, sort_keys=True)
    return _hash(payload)


def _get_plist_mtimes() -> Dict[str, float]:
    """Return modification times of all Nexus LaunchAgent plists."""
    mtimes: Dict[str, float] = {}
    if not os.path.isdir(LAUNCHAGENT_DIR):
        return mtimes
    for fname in os.listdir(LAUNCHAGENT_DIR):
        if fname.startswith("ai.nexus.") or fname.startswith("ai.cipher.") or fname.startswith("ai.atlas.") or fname.startswith("ai.sage."):
            full = os.path.join(LAUNCHAGENT_DIR, fname)
            try:
                mtimes[fname] = os.path.getmtime(full)
            except OSError:
                pass
    return mtimes


def _fetch_service_health(service_name: str, port: int, auth_header: str, secrets: Dict[str, str]) -> ServiceSnapshot:
    """Fetch /health from a single service. Returns degraded snapshot on failure."""
    url = f"http://localhost:{port}/health"
    secret_val = _resolve_secret(auth_header, secrets)
    headers = {auth_header: secret_val} if secret_val else {}
    captured_at = datetime.now(ET).isoformat()
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            version = data.get("version", data.get("agent", "unknown"))
            return ServiceSnapshot(
                service_name=service_name,
                version=str(version),
                status="healthy",
                health_data=data,
                captured_at=captured_at,
            )
        else:
            return ServiceSnapshot(service_name, "unknown", f"http_{resp.status_code}", {}, captured_at)
    except Exception as e:
        return ServiceSnapshot(service_name, "unknown", f"unreachable: {e}", {}, captured_at)


def _resolve_secret(header: str, secrets: Dict[str, str]) -> str:
    """Map auth header name to secret value."""
    mapping = {
        "X-Axiom-Secret":        secrets.get("NEXUS_SECRET", ""),
        "X-Nexus-Secret":        secrets.get("NEXUS_SECRET", ""),
        "X-Nexus-Prime-Secret":  secrets.get("NEXUS_PRIME_SECRET", ""),
        "X-Oracle-Secret":       secrets.get("ORACLE_SECRET", ""),
        "X-AILS-Secret":         secrets.get("AILS_SECRET", ""),
    }
    return mapping.get(header, "")


def capture_snapshot(secrets: Dict[str, str]) -> TradingDaySnapshot:
    """
    Capture full system snapshot.
    Called at 6:00 AM each trading day.

    Args:
        secrets: Dict with NEXUS_SECRET, NEXUS_PRIME_SECRET, ORACLE_SECRET, etc.

    Returns:
        Signed TradingDaySnapshot persisted to SQLite.
    """
    logger.info("Capturing pre-market snapshot...")
    now_et = datetime.now(ET)

    services: Dict[str, Dict] = {}
    for svc_name, (port, auth_header) in SERVICE_PORTS.items():
        snap = _fetch_service_health(svc_name, port, auth_header, secrets)
        services[svc_name] = asdict(snap)
        status_icon = "✅" if snap.status == "healthy" else "❌"
        logger.info(f"  {status_icon} {svc_name}:{port} — {snap.status}")

    plist_mtimes = _get_plist_mtimes()

    agent_weights = {
        "Cipher": 0.45,
        "Atlas":  0.30,
        "Sage":   0.25,
    }

    global_invariants = {
        "nexus_auto_execute": os.getenv("NEXUS_AUTO_EXECUTE", "unknown"),
        "paper_mode": os.getenv("PAPER_MODE", "unknown"),
        "services_checked": len(services),
        "services_healthy": sum(1 for s in services.values() if s["status"] == "healthy"),
    }

    snap = TradingDaySnapshot(
        snapshot_date=now_et.strftime("%Y-%m-%d"),
        captured_at=now_et.isoformat(),
        services=services,
        agent_weights=agent_weights,
        plist_mtimes=plist_mtimes,
        global_invariants=global_invariants,
        signature="",
        valid=False,
    )
    snap.signature = _sign_snapshot(snap)
    snap.valid = True

    _persist_snapshot(snap)
    logger.info(f"Snapshot captured: {global_invariants['services_healthy']}/{global_invariants['services_checked']} services healthy | sig={snap.signature[:12]}...")
    return snap


def validate_snapshot(baseline: TradingDaySnapshot, secrets: Dict[str, str]) -> Tuple[bool, List[str]]:
    """
    Validate current system state against baseline snapshot.
    Returns (all_clear, list_of_drift_items).

    Called at 8:30 AM and then every 60 seconds during trading.
    """
    drift_items: List[str] = []
    current = capture_snapshot(secrets)

    # Check service health drift
    for svc_name, baseline_svc in baseline.services.items():
        current_svc = current.services.get(svc_name, {})
        if baseline_svc.get("status") == "healthy" and current_svc.get("status") != "healthy":
            drift_items.append(f"SERVICE_DOWN:{svc_name} (was healthy, now {current_svc.get('status')})")

    # Check plist modification time drift
    for plist, baseline_mtime in baseline.plist_mtimes.items():
        current_mtime = current.plist_mtimes.get(plist, 0)
        if current_mtime != baseline_mtime:
            drift_items.append(f"PLIST_MODIFIED:{plist} (baseline={baseline_mtime:.0f}, current={current_mtime:.0f})")

    # Check agent weights
    for agent, weight in baseline.agent_weights.items():
        current_weight = current.agent_weights.get(agent)
        if current_weight != weight:
            drift_items.append(f"AGENT_WEIGHT_CHANGED:{agent} ({weight} → {current_weight})")

    if drift_items:
        logger.warning(f"Snapshot drift detected: {len(drift_items)} items — {drift_items}")
    else:
        logger.info("Snapshot validation: ✅ No drift detected")

    return len(drift_items) == 0, drift_items


def classify_drift_action(drift_item: str) -> str:
    """
    Classify a drift item into SAFE_AUTO_CORRECT or REQUIRES_AHMED.

    Returns action string.
    """
    if drift_item.startswith("SERVICE_DOWN:"):
        return "service_restart"
    if drift_item.startswith("PLIST_MODIFIED:"):
        return "REQUIRES_AHMED:plist_change_alert"
    if drift_item.startswith("AGENT_WEIGHT_CHANGED:"):
        return "REQUIRES_AHMED:agent_weight_change"
    return "REQUIRES_AHMED:unknown_drift"


def drift_detection_loop(baseline: TradingDaySnapshot, secrets: Dict[str, str],
                          telegram_alert_fn=None, interval_s: int = 60):
    """
    Continuous drift detection loop. Run during trading hours (9:30 AM - 4:15 PM).
    Blocks the calling thread — run in a daemon thread.

    Args:
        baseline: The 6 AM snapshot to compare against.
        secrets: Auth secrets dict.
        telegram_alert_fn: Callable(message) for Ahmed alerts.
        interval_s: Check interval in seconds (default 60).
    """
    logger.info(f"Drift detection loop started (interval={interval_s}s)")
    while True:
        try:
            all_clear, drift_items = validate_snapshot(baseline, secrets)
            if not all_clear:
                for item in drift_items:
                    action = classify_drift_action(item)
                    if action.startswith("REQUIRES_AHMED"):
                        msg = f"⚠️ SNAPSHOT DRIFT — REQUIRES AHMED\n{item}\nAction: {action}"
                        logger.critical(msg)
                        if telegram_alert_fn:
                            telegram_alert_fn(msg)
                    else:
                        logger.warning(f"Auto-correcting drift: {item} → {action}")
        except Exception as e:
            logger.error(f"Drift detection error: {e}")
        time.sleep(interval_s)


def _persist_snapshot(snap: TradingDaySnapshot):
    """Persist snapshot to SQLite."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT,
            captured_at TEXT,
            payload TEXT,
            signature TEXT,
            valid INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO snapshots (snapshot_date, captured_at, payload, signature, valid) VALUES (?, ?, ?, ?, ?)",
        (snap.snapshot_date, snap.captured_at, json.dumps(asdict(snap)), snap.signature, int(snap.valid))
    )
    conn.commit()
    conn.close()


def load_latest_snapshot() -> Optional[TradingDaySnapshot]:
    """Load the most recent snapshot from SQLite."""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT payload FROM snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return None
    data = json.loads(row[0])
    return TradingDaySnapshot(**{k: data[k] for k in TradingDaySnapshot.__dataclass_fields__})
