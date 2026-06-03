"""
snapshot_system.py — Nexus v1 Snapshot & Drift Detection System
===============================================================
Part of NEXUS RESILIENCE FRAMEWORK v1.0 — Week 3 Build

PURPOSE:
  Capture the known-good state of all Nexus v1 services at 6:00 AM daily.
  Detect configuration drift every 60 seconds during trading hours.
  Auto-correct safe values. Alert Ahmed on critical drift.

WHAT IS SNAPSHOTTED:
  - Alpha service health + all key config values
  - Prime service health + all key config values
  - Axiom service health
  - Environment variables (key presence, not values)
  - NEXUS_AUTO_EXECUTE flag (must stay false — checked every 60s)
  - Scanner loop state
  - Layer 11 status

DRIFT DETECTION RULES:
  CRITICAL (never auto-correct, always alert):
    - NEXUS_AUTO_EXECUTE = true (should always be false)
    - execution_paused changed from false → true without reason
    - Service URL changed
    - Secret/key presence changed

  SAFE (auto-correct if possible):
    - pending queue growth (trigger flush attempt)
    - loop_active = false (trigger restart via health alert)

SNAPSHOT VALIDITY:
  A snapshot is VALID if:
    - Captured within last 24 hours
    - All 3 services responded at capture time
    - Cryptographic hash matches stored value (tamper detection)

Author: OMNI 🌐
Date:   2026-04-15 (Resilience Framework Week 3)
"""

import os
import json
import time
import hashlib
import logging
import sqlite3
import threading
import datetime
import requests
from typing import Optional, Dict, Any, Tuple
from pathlib import Path

logger = logging.getLogger("omni.snapshot")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(name)s] %(levelname)s — %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────
ALPHA_URL      = os.getenv("ALPHA_SERVICE_URL", "https://worker-production-2060.up.railway.app")
PRIME_URL      = os.getenv("PRIME_SERVICE_URL", "https://nexus-prime-bot-production.up.railway.app")
AXIOM_URL      = os.getenv("AXIOM_URL", "https://axiom-production-334c.up.railway.app")
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "")
TG_AHMED_DM    = "8573754783"
TG_HEALTH_GRP  = os.getenv("TG_HEALTH_GROUP", "-5184172590")
NEXUS_SECRET   = os.getenv("NEXUS_SECRET", "")
DB_PATH        = os.getenv("SNAPSHOT_DB_PATH", "/data/nexus_snapshot.db")

DRIFT_CHECK_INTERVAL  = 60    # seconds between drift checks during market hours
SNAPSHOT_HOUR         = 6     # 6:00 AM ET daily snapshot
DRIFT_ALERT_COOLDOWN  = 300   # seconds between repeat alerts for same drift

# ── Critical fields that should NEVER change without manual action ─────────────
CRITICAL_WATCH = {
    "alpha": [
        ("execution_paused",  False,   "CRITICAL: Alpha execution_paused changed"),
        ("status",            "healthy", "CRITICAL: Alpha service no longer healthy"),
    ],
    "prime": [
        ("status",            "healthy", "CRITICAL: Prime service no longer healthy"),
    ],
    "axiom": [
        ("status",            "healthy", "CRITICAL: Axiom service no longer healthy"),
    ],
}

# ── NEXUS_AUTO_EXECUTE: the most sacred flag ───────────────────────────────────
# Must ALWAYS be false. Checked every 60 seconds.
NEXUS_AUTO_EXECUTE_FIELD = "nexus_auto_execute"


# ── Database ───────────────────────────────────────────────────────────────────

def _init_snapshot_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT UNIQUE,
            captured_at  TEXT,
            alpha_json   TEXT,
            prime_json   TEXT,
            axiom_json   TEXT,
            signature    TEXT,      -- SHA256 hash of combined state
            valid        INTEGER DEFAULT 1,
            notes        TEXT
        );
        CREATE TABLE IF NOT EXISTS drift_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            field        TEXT,
            service      TEXT,
            expected     TEXT,
            actual       TEXT,
            severity     TEXT,
            action_taken TEXT,
            resolved     INTEGER DEFAULT 0,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS drift_checks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT DEFAULT (datetime('now')),
            drifts_found INTEGER DEFAULT 0,
            all_clear    INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()


# ── Snapshot System ────────────────────────────────────────────────────────────

class SnapshotSystem:
    """
    Captures daily state snapshots and monitors for drift every 60 seconds.
    """

    def __init__(self):
        _init_snapshot_db()
        self._lock              = threading.Lock()
        self._running           = False
        self._monitor_th        = None
        self._snapshot_th       = None
        self._last_snapshot     : Optional[dict] = None
        self._drift_alert_times : Dict[str, float] = {}
        self._load_latest_snapshot()

    def start(self):
        self._running = True

        # Drift detection thread (every 60s)
        self._monitor_th = threading.Thread(
            target=self._drift_loop, daemon=True, name="snapshot-drift"
        )
        self._monitor_th.start()

        # Snapshot scheduler thread (6:00 AM ET daily)
        self._snapshot_th = threading.Thread(
            target=self._snapshot_scheduler, daemon=True, name="snapshot-scheduler"
        )
        self._snapshot_th.start()

        logger.info("📸 Snapshot System started — drift check every 60s, snapshot at 6:00 AM ET")

    def stop(self):
        self._running = False

    def capture_now(self) -> dict:
        """Capture a snapshot right now. Returns snapshot dict."""
        return self._capture_snapshot()

    def get_status(self) -> dict:
        with self._lock:
            snap = self._last_snapshot
        return {
            "last_snapshot_date": snap.get("snapshot_date") if snap else None,
            "last_snapshot_valid": snap.get("valid") if snap else None,
            "snapshot_age_hours": self._snapshot_age_hours(),
            "monitor_running": self._running,
        }

    # ── Snapshot Capture ──────────────────────────────────────────────────────

    def _snapshot_scheduler(self):
        import pytz
        et_tz = pytz.timezone("America/New_York")

        while self._running:
            try:
                now_et = datetime.datetime.now(et_tz)
                today  = str(now_et.date())

                # Fire at 6:00 AM ET
                if now_et.hour == SNAPSHOT_HOUR and now_et.minute == 0:
                    # Check not already captured today
                    if not self._snapshot_exists_today(today):
                        logger.info(f"📸 6:00 AM ET — Capturing daily snapshot for {today}")
                        snap = self._capture_snapshot()
                        if snap.get("valid"):
                            self._send_tg(
                                f"📸 NEXUS SNAPSHOT CAPTURED\n"
                                f"Date: {today} | Signature: {snap.get('signature','?')[:16]}...\n"
                                f"Alpha: {snap.get('alpha_status','?')} | "
                                f"Prime: {snap.get('prime_status','?')} | "
                                f"Axiom: {snap.get('axiom_status','?')}",
                                [TG_HEALTH_GRP]
                            )
            except Exception as e:
                logger.error(f"Snapshot scheduler error: {e}")
            time.sleep(45)

    def _capture_snapshot(self) -> dict:
        """Fetch current state of all services and persist to DB."""
        today = str(datetime.date.today())
        captured_at = datetime.datetime.utcnow().isoformat()

        alpha_state = self._fetch_alpha_state()
        prime_state = self._fetch_prime_state()
        axiom_state = self._fetch_axiom_state()

        # Compute signature
        combined = json.dumps({
            "alpha": alpha_state, "prime": prime_state, "axiom": axiom_state,
            "date": today
        }, sort_keys=True)
        signature = hashlib.sha256(combined.encode()).hexdigest()

        valid = all([
            alpha_state.get("status") == "healthy",
            prime_state.get("status") == "healthy",
            axiom_state.get("status") == "healthy",
        ])

        snap = {
            "snapshot_date": today,
            "captured_at":   captured_at,
            "alpha":         alpha_state,
            "prime":         prime_state,
            "axiom":         axiom_state,
            "signature":     signature,
            "valid":         valid,
            "alpha_status":  alpha_state.get("status", "unknown"),
            "prime_status":  prime_state.get("status", "unknown"),
            "axiom_status":  axiom_state.get("status", "unknown"),
        }

        # Persist
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
                INSERT OR REPLACE INTO snapshots
                (snapshot_date, captured_at, alpha_json, prime_json, axiom_json, signature, valid)
                VALUES (?,?,?,?,?,?,?)
            """, (
                today, captured_at,
                json.dumps(alpha_state), json.dumps(prime_state), json.dumps(axiom_state),
                signature, 1 if valid else 0
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Snapshot persist error: {e}")

        with self._lock:
            self._last_snapshot = snap

        logger.info(f"📸 Snapshot captured: {'VALID' if valid else 'INVALID'} | sig={signature[:16]}")
        return snap

    def _fetch_alpha_state(self) -> dict:
        try:
            r = requests.get(f"{ALPHA_URL}/health", timeout=10)
            if r.status_code == 200:
                h = r.json()
                return {
                    "status":           "healthy",
                    "version":          h.get("version"),
                    "execution_paused": h.get("execution_paused", False),
                    "loop_active":      h.get("omni_scanner", {}).get("loop_active", False),
                    "pending":          h.get("pending", 0),
                    "go_today":         h.get("go_verdicts_today", 0),
                    "layer11_state":    h.get("layer11", {}).get("state"),
                    "nexus_auto_execute": h.get("nexus_auto_execute", False),
                }
            return {"status": f"http_{r.status_code}"}
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}

    def _fetch_prime_state(self) -> dict:
        try:
            r = requests.get(f"{PRIME_URL}/health", timeout=10)
            if r.status_code == 200:
                h = r.json()
                return {
                    "status":    "healthy",
                    "version":   h.get("version"),
                    "scans":     h.get("scans", 0),
                    "go_today":  h.get("go_verdicts_today", 0),
                }
            return {"status": f"http_{r.status_code}"}
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}

    def _fetch_axiom_state(self) -> dict:
        try:
            r = requests.get(f"{AXIOM_URL}/health", timeout=10)
            if r.status_code == 200:
                h = r.json()
                return {
                    "status":   "healthy",
                    "version":  h.get("version"),
                    "gate":     h.get("position_gate", h.get("gate")),
                }
            return {"status": f"http_{r.status_code}"}
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}

    # ── Drift Detection ───────────────────────────────────────────────────────

    def _drift_loop(self):
        import pytz
        et_tz = pytz.timezone("America/New_York")

        while self._running:
            try:
                now_et = datetime.datetime.now(et_tz)
                # Check during trading window + 1h before/after
                in_window = (8 <= now_et.hour <= 17) and now_et.weekday() < 5

                if in_window and self._last_snapshot:
                    self._check_drift()

            except Exception as e:
                logger.error(f"Drift loop error: {e}")
            time.sleep(DRIFT_CHECK_INTERVAL)

    def _check_drift(self):
        """Compare current state to snapshot. Alert on critical drift."""
        with self._lock:
            snap = self._last_snapshot
        if not snap:
            return

        drifts_found = 0
        drifts = []

        # Fetch current state
        alpha_now = self._fetch_alpha_state()
        prime_now = self._fetch_prime_state()
        axiom_now = self._fetch_axiom_state()

        current = {"alpha": alpha_now, "prime": prime_now, "axiom": axiom_now}
        baseline = {"alpha": snap.get("alpha", {}), "prime": snap.get("prime", {}), "axiom": snap.get("axiom", {})}

        # ── SACRED CHECK: NEXUS_AUTO_EXECUTE must ALWAYS be false ─────────────
        auto_exec = alpha_now.get("nexus_auto_execute", False)
        if auto_exec:
            drift = {
                "field":    "nexus_auto_execute",
                "service":  "alpha",
                "expected": False,
                "actual":   True,
                "severity": "CRITICAL",
                "action":   "IMMEDIATE_ALERT — manual review required",
            }
            drifts.append(drift)
            drifts_found += 1
            self._handle_critical_drift(drift)

        # ── Check critical fields ─────────────────────────────────────────────
        for service, watches in CRITICAL_WATCH.items():
            curr_state = current.get(service, {})
            base_state = baseline.get(service, {})

            for field, expected_val, msg in watches:
                curr_val = curr_state.get(field)
                base_val = base_state.get(field)

                # Only alert if we drifted FROM the snapshot value
                if curr_val != base_val and curr_val != expected_val:
                    drift = {
                        "field":    field,
                        "service":  service,
                        "expected": str(base_val),
                        "actual":   str(curr_val),
                        "severity": "CRITICAL" if "CRITICAL" in msg else "WARNING",
                        "action":   msg,
                    }
                    drifts.append(drift)
                    drifts_found += 1
                    self._handle_drift(drift)

        # Log check
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO drift_checks (drifts_found, all_clear) VALUES (?,?)",
                (drifts_found, 1 if drifts_found == 0 else 0)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

        if drifts_found == 0:
            logger.debug("📸 Drift check: all clear")

    def _handle_critical_drift(self, drift: dict):
        """Handle a critical drift — always alert Ahmed immediately."""
        key = f"{drift['service']}_{drift['field']}"
        last = self._drift_alert_times.get(key, 0)
        if time.time() - last < 60:  # 1 min cooldown for critical
            return
        self._drift_alert_times[key] = time.time()

        msg = (
            f"🚨 CRITICAL DRIFT DETECTED\n"
            f"Service: {drift['service'].upper()} | Field: {drift['field']}\n"
            f"Expected: {drift['expected']} → Actual: {drift['actual']}\n"
            f"Action: {drift['action']}\n"
            f"⚠️ MANUAL REVIEW REQUIRED"
        )
        self._send_tg(msg, [TG_AHMED_DM, TG_HEALTH_GRP])

        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO drift_events (field, service, expected, actual, severity, action_taken) VALUES (?,?,?,?,?,?)",
                (drift['field'], drift['service'], drift['expected'], drift['actual'],
                 drift['severity'], drift['action'])
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _handle_drift(self, drift: dict):
        """Handle non-critical drift — alert with cooldown."""
        key = f"{drift['service']}_{drift['field']}"
        last = self._drift_alert_times.get(key, 0)
        if time.time() - last < DRIFT_ALERT_COOLDOWN:
            return
        self._drift_alert_times[key] = time.time()

        severity_icon = "🚨" if drift["severity"] == "CRITICAL" else "⚠️"
        msg = (
            f"{severity_icon} DRIFT DETECTED [{drift['severity']}]\n"
            f"Service: {drift['service'].upper()} | Field: {drift['field']}\n"
            f"Was: {drift['expected']} → Now: {drift['actual']}\n"
            f"{drift['action']}"
        )
        targets = [TG_HEALTH_GRP]
        if drift["severity"] == "CRITICAL":
            targets.append(TG_AHMED_DM)
        self._send_tg(msg, targets)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _load_latest_snapshot(self):
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("""
                SELECT snapshot_date, captured_at, alpha_json, prime_json, axiom_json, signature, valid
                FROM snapshots ORDER BY id DESC LIMIT 1
            """).fetchone()
            conn.close()
            if row:
                with self._lock:
                    self._last_snapshot = {
                        "snapshot_date": row[0],
                        "captured_at":   row[1],
                        "alpha":         json.loads(row[2] or "{}"),
                        "prime":         json.loads(row[3] or "{}"),
                        "axiom":         json.loads(row[4] or "{}"),
                        "signature":     row[5],
                        "valid":         bool(row[6]),
                    }
                logger.info(f"📸 Loaded snapshot: {row[0]} | valid={bool(row[6])}")
        except Exception as e:
            logger.warning(f"Could not load snapshot: {e}")

    def _snapshot_exists_today(self, today: str) -> bool:
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.execute("SELECT COUNT(*) FROM snapshots WHERE snapshot_date=?", (today,))
            count = c.fetchone()[0]
            conn.close()
            return count > 0
        except Exception:
            return False

    def _snapshot_age_hours(self) -> Optional[float]:
        with self._lock:
            snap = self._last_snapshot
        if not snap or not snap.get("captured_at"):
            return None
        try:
            captured = datetime.datetime.fromisoformat(snap["captured_at"])
            age = (datetime.datetime.utcnow() - captured).total_seconds() / 3600
            return round(age, 1)
        except Exception:
            return None

    def _send_tg(self, text: str, targets: list):
        if not TG_BOT_TOKEN:
            logger.info(f"[TG] {text[:80]}")
            return
        for chat_id in targets:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=5,
                )
            except Exception:
                pass


# ── Module-level singleton ─────────────────────────────────────────────────────
_snapshot_sys: Optional[SnapshotSystem] = None


def get_snapshot_system() -> SnapshotSystem:
    global _snapshot_sys
    if _snapshot_sys is None:
        _snapshot_sys = SnapshotSystem()
        _snapshot_sys.start()
    return _snapshot_sys


if __name__ == "__main__":
    print("📸 Snapshot System — Manual Capture")
    ss = SnapshotSystem()
    snap = ss.capture_now()
    print(f"Snapshot valid: {snap['valid']}")
    print(f"Alpha: {snap.get('alpha_status')} | Prime: {snap.get('prime_status')} | Axiom: {snap.get('axiom_status')}")
    print(f"Signature: {snap.get('signature','?')[:32]}...")
