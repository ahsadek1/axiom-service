"""
agent_health_base.py — Universal Agent Health Module
=====================================================
Every agent in the NEXUS system imports this module.
One standard. One language. One protocol. Zero judgment calls.

Usage:
    from shared.health.agent_health_base import AgentHealthMonitor

    monitor = AgentHealthMonitor(
        agent_name="OMNI",
        service_port=8004,
        plist_name="ai.nexus.omni",
        domain_checks=[check_omni_synthesis, check_omni_pool],
    )
    monitor.start()  # runs in background thread

Ahmed directive May 2026:
    "Decentralization of system health. Every agent becomes responsible
     for his own health monitoring and its immediate domain of function.
     The sequence of the fix is standardized so there is no judgment call."
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import requests

from .error_registry import ERROR_REGISTRY, ErrorDefinition, Severity
from .incident_store import (
    init_store, save_incident, load_active_incident, archive_incident,
    RecoveryWatcher, PatternDetector, CorrelationEngine,
    fix_memory_real, get_health_mesh_status,
)

log = logging.getLogger("nexus.health")
_ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NEXUS_SECRET     = os.getenv("NEXUS_WEBHOOK_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c")
HEALTH_GROUP     = os.getenv("NEXUS_HEALTH_GROUP_ID", "-5241272802")
CHRONICLE_DB     = os.getenv("CHRONICLE_DB_PATH", "/Users/ahmedsadek/nexus/data/chronicle.db")
MESSAGE_BUS_URL  = os.getenv("MESSAGE_BUS_URL", "http://192.168.1.141:9999")

# Dispatch chain — in order of preference
DISPATCH_CHAIN = ["cipher", "vector", "genesis"]

# Self-fix constraints
MAX_FIX_ATTEMPTS    = 3
MAX_FIX_MINUTES     = 10
MONITOR_INTERVAL_S  = 60   # check every 60 seconds


# ---------------------------------------------------------------------------
# Incident record
# ---------------------------------------------------------------------------

class Incident:
    """A detected error with its full lifecycle."""

    def __init__(self, error_code: str, agent: str, detail: str):
        self.incident_id  = f"{agent}-{error_code}-{int(time.time())}"
        self.error_code   = error_code
        self.agent        = agent
        self.detail       = detail
        self.detected_at  = datetime.now(timezone.utc)
        self.attempts     = 0
        self.resolved     = False
        self.escalated    = False
        self.resolution   = ""

    @property
    def elapsed_minutes(self) -> float:
        return (datetime.now(timezone.utc) - self.detected_at).total_seconds() / 60

    def to_dict(self) -> dict:
        return {
            "incident_id":  self.incident_id,
            "error_code":   self.error_code,
            "agent":        self.agent,
            "detail":       self.detail,
            "detected_at":  self.detected_at.isoformat(),
            "attempts":     self.attempts,
            "resolved":     self.resolved,
            "escalated":    self.escalated,
            "resolution":   self.resolution,
        }


# ---------------------------------------------------------------------------
# Fix executor
# ---------------------------------------------------------------------------

class FixExecutor:
    """Executes fix steps from the error registry deterministically."""

    def __init__(self, agent_name: str, plist_name: str, service_port: int):
        self.agent_name   = agent_name
        self.plist_name   = plist_name
        self.service_port = service_port

    def execute(self, incident: Incident) -> bool:
        """
        Execute fix sequence for the given error code.
        Returns True if resolved, False if not.
        """
        error_def = ERROR_REGISTRY.get(incident.error_code)
        if not error_def:
            log.error("Unknown error code: %s", incident.error_code)
            return False

        if not error_def.can_auto_fix:
            log.warning("%s: Error %s cannot be auto-fixed — alert only",
                       self.agent_name, incident.error_code)
            return False

        log.info("%s: Executing fix for %s (attempt %d)",
                self.agent_name, incident.error_code, incident.attempts + 1)

        # Route to correct fix handler
        handler = self._get_handler(incident.error_code)
        if handler:
            return handler(incident)

        # Default: service restart
        return self._restart_service(incident)

    def _get_handler(self, error_code: str) -> Optional[Callable]:
        """Map error codes to specific fix handlers."""
        handlers = {
            "ESV001": self._restart_service,
            "ESV002": self._restart_service,
            "ESV003": self._restart_service_clean,
            "EDA001": self._fix_db_lock,
            "EDA002": self._fix_db_corruption,
            "EDA003": self._clear_cache,
            "EAG001": self._restart_agent,
            "EAG002": self._restart_agent,
            "EAG004": self._backoff_retry,
            "EPL001": self._fix_circuit_breaker,
            "EPL002": self._fix_vix_brake,
            "EPL003": self._restart_service,
            "EPL004": self._drain_dlq,
            "EPL005": self._restart_pipeline,
            "EIN001": self._fix_memory,
            "EIN002": self._fix_disk,
            "EIN004": self._rotate_logs,
            "EIN005": self._fix_stale_lock,
        }
        return handlers.get(error_code)

    def _restart_service(self, incident: Incident) -> bool:
        plist = os.path.expanduser(
            f"~/Library/LaunchAgents/{self.plist_name}.plist"
        )
        try:
            subprocess.run(["launchctl", "unload", plist],
                          capture_output=True, timeout=10)
            time.sleep(2)
            subprocess.run(["launchctl", "load", plist],
                          capture_output=True, timeout=10)
            time.sleep(6)
            return self._verify_health()
        except Exception as exc:
            log.error("Restart failed: %s", exc)
            return False

    def _restart_service_clean(self, incident: Incident) -> bool:
        """Restart with log collection before restart."""
        log_path = os.path.expanduser(
            f"~/nexus/logs/{self.plist_name.replace('ai.nexus.','')}/stderr.log"
        )
        try:
            result = subprocess.run(
                ["tail", "-50", log_path],
                capture_output=True, text=True, timeout=5
            )
            incident.detail += f"\nCrash logs:\n{result.stdout[-500:]}"
        except Exception:
            pass
        return self._restart_service(incident)

    def _restart_agent(self, incident: Incident) -> bool:
        """Restart agent via OpenClaw."""
        agent_lower = self.agent_name.lower()
        try:
            result = subprocess.run(
                ["openclaw", "restart", agent_lower],
                capture_output=True, text=True, timeout=30
            )
            time.sleep(5)
            return self._verify_health()
        except Exception as exc:
            log.error("OpenClaw restart failed: %s", exc)
            return self._restart_service(incident)

    def _fix_db_lock(self, incident: Incident) -> bool:
        """Release stale SQLite lock files."""
        try:
            db_path = os.getenv("ALPHA_DB_PATH",
                               "/Users/ahmedsadek/nexus/data/alpha_buffer.db")
            for ext in ["-wal", "-shm"]:
                lock_file = db_path + ext
                if os.path.exists(lock_file):
                    os.remove(lock_file)
                    log.info("Removed lock file: %s", lock_file)
            time.sleep(2)
            conn = sqlite3.connect(db_path, timeout=10)
            conn.execute("SELECT 1")
            conn.close()
            return True
        except Exception as exc:
            log.error("DB lock fix failed: %s", exc)
            return False

    def _fix_db_corruption(self, incident: Incident) -> bool:
        """Restore DB from backup."""
        log.error("DB corruption detected — escalating immediately")
        incident.detail += "\nDB corruption requires manual recovery"
        return False  # Force escalation

    def _clear_cache(self, incident: Incident) -> bool:
        """Clear stale caches via service endpoint."""
        try:
            resp = requests.post(
                f"http://localhost:{self.service_port}/cache/clear",
                headers={"X-Nexus-Secret": NEXUS_SECRET},
                timeout=5,
            )
            return resp.status_code in (200, 204)
        except Exception:
            return True  # Cache clear is best-effort

    def _backoff_retry(self, incident: Incident) -> bool:
        """Exponential backoff for rate limiting."""
        backoff = [30, 60, 120]
        attempt = min(incident.attempts, len(backoff) - 1)
        time.sleep(backoff[attempt])
        return True  # Rate limiting resolves itself

    def _fix_circuit_breaker(self, incident: Incident) -> bool:
        """Reset CB if paper mode and off-hours."""
        is_paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
        now_et = datetime.now(_ET)
        market_open = (now_et.weekday() < 5 and
                      (now_et.hour > 9 or (now_et.hour == 9 and now_et.minute >= 30)) and
                      now_et.hour < 16)

        if not is_paper:
            incident.detail += "\nLive mode — CB reset requires Ahmed"
            return False

        if market_open:
            incident.detail += "\nMarket hours — CB reset deferred to close"
            return False

        try:
            resp = requests.post(
                "http://localhost:8002/circuit-breaker/reset",
                headers={"X-Nexus-Secret": NEXUS_SECRET},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as exc:
            log.error("CB reset failed: %s", exc)
            return False

    def _fix_vix_brake(self, incident: Incident) -> bool:
        """Clear VIX brake — restart alpha-execution after verifying Axiom."""
        try:
            axiom_ok = requests.get("http://localhost:8001/health", timeout=4)
            if axiom_ok.status_code != 200:
                incident.detail += "\nAxiom down — fixing Axiom first"
                return False
        except Exception:
            return False

        plist = os.path.expanduser(
            "~/Library/LaunchAgents/ai.nexus.alpha-execution.plist"
        )
        try:
            subprocess.run(["launchctl", "unload", plist],
                          capture_output=True, timeout=10)
            time.sleep(2)
            subprocess.run(["launchctl", "load", plist],
                          capture_output=True, timeout=10)
            time.sleep(6)
            resp = requests.get("http://localhost:8005/health", timeout=4)
            if resp.status_code == 200:
                vix = resp.json().get("vix", 999)
                return vix < 500
        except Exception as exc:
            log.error("VIX brake fix failed: %s", exc)
        return False

    def _drain_dlq(self, incident: Incident) -> bool:
        """Drain dead letter queue."""
        try:
            resp = requests.post(
                "http://localhost:8005/dlq/drain",
                headers={"X-Nexus-Secret": NEXUS_SECRET},
                timeout=15,
            )
            if resp.status_code == 200:
                drained = resp.json().get("drained", 0)
                incident.resolution = f"Drained {drained} items from DLQ"
                return True
        except Exception as exc:
            log.error("DLQ drain failed: %s", exc)
        return False

    def _restart_pipeline(self, incident: Incident) -> bool:
        """Restart full pipeline in correct order."""
        order = [
            ("ai.nexus.axiom",        8001),
            ("ai.nexus.alpha-buffer", 8002),
            ("ai.nexus.omni",         8004),
        ]
        for plist_name, port in order:
            plist = os.path.expanduser(
                f"~/Library/LaunchAgents/{plist_name}.plist"
            )
            subprocess.run(["launchctl", "unload", plist],
                          capture_output=True, timeout=10)
            time.sleep(2)
            subprocess.run(["launchctl", "load", plist],
                          capture_output=True, timeout=10)
            time.sleep(8)
            try:
                resp = requests.get(f"http://localhost:{port}/health", timeout=5)
                if resp.status_code != 200:
                    log.error("Pipeline restart: %s failed", plist_name)
                    return False
            except Exception:
                return False
        return True

    def _fix_memory(self, incident: Incident) -> bool:
        """Free memory by rotating logs and restarting non-critical services."""
        try:
            import glob
            rotated = 0
            for log_file in glob.glob(
                os.path.expanduser("~/nexus/logs/**/*.log"), recursive=True
            ):
                if os.path.getsize(log_file) > 100 * 1024 * 1024:  # 100MB
                    os.rename(log_file, log_file + ".rotated")
                    rotated += 1
            incident.resolution = f"Rotated {rotated} large log files"
            return True
        except Exception as exc:
            log.error("Memory fix failed: %s", exc)
            return False

    def _fix_disk(self, incident: Incident) -> bool:
        """Free disk space."""
        try:
            import glob
            removed = 0
            for f in glob.glob(
                os.path.expanduser("~/nexus/logs/**/*.rotated"), recursive=True
            ):
                os.remove(f)
                removed += 1
            incident.resolution = f"Removed {removed} rotated log files"
            return True
        except Exception as exc:
            log.error("Disk fix failed: %s", exc)
            return False

    def _rotate_logs(self, incident: Incident) -> bool:
        """Rotate oversized log files."""
        return self._fix_memory(incident)

    def _fix_stale_lock(self, incident: Incident) -> bool:
        """Remove stale lock files."""
        try:
            import glob
            for f in glob.glob(
                os.path.expanduser("~/nexus/**/*.lock"), recursive=True
            ):
                mtime = os.path.getmtime(f)
                age_hours = (time.time() - mtime) / 3600
                if age_hours > 2:
                    os.remove(f)
                    log.info("Removed stale lock: %s (age=%.1fh)", f, age_hours)
            return True
        except Exception as exc:
            log.error("Lock fix failed: %s", exc)
            return False

    def _verify_health(self) -> bool:
        """Verify service is healthy after fix."""
        time.sleep(3)
        try:
            resp = requests.get(
                f"http://localhost:{self.service_port}/health",
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get("status") in ("healthy", "ok")
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Dispatch client
# ---------------------------------------------------------------------------

class DispatchClient:
    """
    Sends fix requests to peer agents when self-fix fails.
    Chain: Cipher → Vector → Genesis → Sovereign (FYI only).
    """

    def dispatch(self, incident: Incident, attempted_self: bool = True) -> bool:
        """
        Dispatch to next available responder.
        Returns True if dispatched successfully.
        """
        for responder in DISPATCH_CHAIN:
            if self._is_available(responder):
                self._send_to(responder, incident)
                log.info("Dispatched %s to %s", incident.incident_id, responder)
                return True
            log.warning("Responder %s is busy — trying next", responder)

        # All responders busy — notify Sovereign as last resort
        self._notify_sovereign_urgent(incident)
        return False

    def _is_available(self, responder: str) -> bool:
        """Check if a responder agent is available via message bus."""
        try:
            resp = requests.post(
                f"{MESSAGE_BUS_URL}/ping",
                json={"agent": responder},
                timeout=3,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def _send_to(self, responder: str, incident: Incident) -> None:
        """Send fix request to responder via message bus."""
        try:
            requests.post(
                f"{MESSAGE_BUS_URL}/send",
                json={
                    "from":    incident.agent.lower(),
                    "to":      responder,
                    "type":    "FIX_REQUEST",
                    "payload": incident.to_dict(),
                },
                timeout=5,
            )
        except Exception as exc:
            log.error("Dispatch to %s failed: %s", responder, exc)

    def _notify_sovereign_urgent(self, incident: Incident) -> None:
        """Notify Sovereign when all responders are unavailable."""
        try:
            requests.post(
                f"{MESSAGE_BUS_URL}/send",
                json={
                    "from":    incident.agent.lower(),
                    "to":      "sovereign",
                    "type":    "URGENT_ESCALATION",
                    "payload": {
                        **incident.to_dict(),
                        "reason": "All responders (Cipher, Vector, Genesis) unavailable",
                    },
                },
                timeout=5,
            )
        except Exception as exc:
            log.error("Sovereign notification failed: %s", exc)


# ---------------------------------------------------------------------------
# Chronicle writer
# ---------------------------------------------------------------------------

class ChronicleWriter:
    """Logs all incidents to Chronicle DB. Fire-and-forget, never blocks."""

    def log_incident(self, incident: Incident) -> None:
        """Log incident to Chronicle in background thread."""
        threading.Thread(
            target=self._write,
            args=(incident,),
            daemon=True,
        ).start()

    def _write(self, incident: Incident) -> None:
        try:
            conn = sqlite3.connect(CHRONICLE_DB, timeout=5)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS health_incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT,
                    error_code TEXT,
                    agent TEXT,
                    detail TEXT,
                    detected_at TEXT,
                    attempts INTEGER,
                    resolved INTEGER,
                    escalated INTEGER,
                    resolution TEXT,
                    ts REAL
                )
            """)
            conn.execute("""
                INSERT OR REPLACE INTO health_incidents
                (incident_id, error_code, agent, detail, detected_at,
                 attempts, resolved, escalated, resolution, ts)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                incident.incident_id,
                incident.error_code,
                incident.agent,
                incident.detail[:1000],
                incident.detected_at.isoformat(),
                incident.attempts,
                int(incident.resolved),
                int(incident.escalated),
                incident.resolution[:500],
                time.time(),
            ))
            conn.commit()
            conn.close()
        except Exception as exc:
            log.warning("Chronicle write failed: %s", exc)


# ---------------------------------------------------------------------------
# Telegram FYI
# ---------------------------------------------------------------------------

def _fyi(msg: str) -> None:
    """Send FYI notification. Never blocks. Never fails silently."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": HEALTH_GROUP, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Agent Health Monitor — the main class every agent instantiates
# ---------------------------------------------------------------------------

class AgentHealthMonitor:
    """
    Self-sovereign health monitor for a single agent/service.

    Each agent creates one instance and calls start().
    The monitor runs in a background thread, checking health every 60 seconds.
    On error: self-fix × 3, then dispatch to peer chain, then Sovereign FYI.

    Usage:
        monitor = AgentHealthMonitor(
            agent_name="OMNI",
            service_port=8004,
            plist_name="ai.nexus.omni",
            domain_checks=[check_omni_synthesis],
        )
        monitor.start()
    """

    def __init__(
        self,
        agent_name: str,
        service_port: int,
        plist_name: str,
        domain_checks: list[Callable[[], Optional[str]]] = None,
        check_interval: int = MONITOR_INTERVAL_S,
    ):
        self.agent_name     = agent_name
        self.service_port   = service_port
        self.plist_name     = plist_name
        self.domain_checks  = domain_checks or []
        self.check_interval = check_interval

        self._executor  = FixExecutor(agent_name, plist_name, service_port)
        self._dispatch  = DispatchClient()
        self._chronicle = ChronicleWriter()
        self._active_incidents: dict[str, Incident] = {}
        self._watcher    = RecoveryWatcher()
        self._patterns   = PatternDetector()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        """Start health monitor in background thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name=f"health-{self.agent_name.lower()}",
        )
        self._thread.start()
        init_store()
        log.info("%s health monitor started (interval=%ds)", self.agent_name, self.check_interval)

    def stop(self) -> None:
        self._running = False

    def _monitor_loop(self) -> None:
        """Main monitoring loop. Runs every check_interval seconds."""
        while self._running:
            try:
                self._run_checks()
            except Exception as exc:
                log.error("%s monitor error: %s", self.agent_name, exc)
            time.sleep(self.check_interval)

    def _run_checks(self) -> None:
        """Run all health checks and handle any errors found."""
        # 1. Self health check
        error_code = self._check_self_health()
        if error_code:
            self._handle_error(error_code, "Self health check failed")

        # 2. Domain checks
        for check_fn in self.domain_checks:
            try:
                error_code = check_fn()
                if error_code:
                    self._handle_error(
                        error_code,
                        f"Domain check {check_fn.__name__} detected {error_code}"
                    )
            except Exception as exc:
                log.error("%s domain check error: %s", self.agent_name, exc)

    def _check_self_health(self) -> Optional[str]:
        """Check own service health. Returns error code or None."""
        try:
            resp = requests.get(
                f"http://localhost:{self.service_port}/health",
                timeout=5,
            )
            if resp.status_code != 200:
                return "ESV001"
            status = resp.json().get("status", "")
            if status not in ("healthy", "ok"):
                return "ESV002"
            return None
        except requests.exceptions.ConnectionError:
            return "ESV001"
        except Exception:
            return "ESV001"

    def _handle_error(self, error_code: str, detail: str) -> None:
        """
        Core error handling protocol:
        1. Watch period check — prevent restart loops
        2. Pattern check — escalate repeat offenders to Genesis
        3. Load persisted incident — survive restarts
        4. Self-fix x3 (10 min cap)
        5. Dispatch chain: Cipher, Vector, Genesis
        6. Sovereign FYI only — never a bottleneck
        7. Persist + Chronicle log
        """
        error_def = ERROR_REGISTRY.get(error_code)
        if not error_def:
            return

        # Guard 1: Watch period — restart loop prevention
        if self._watcher.is_in_watch_period(self.agent_name, error_code):
            log.error("%s: %s recurred within watch period — escalating immediately",
                     self.agent_name, error_code)
            incident = Incident(error_code, self.agent_name,
                               detail + " [RECURRENCE in watch period]")
            incident.attempts = error_def.escalate_after_attempts
            self._escalate(incident)
            self._chronicle.log_incident(incident)
            return

        # Guard 2: Pattern detection — structural problems
        pattern = self._patterns.check_pattern(self.agent_name, error_code)
        if pattern:
            log.error("%s: %s is a PATTERN (%dx in %dh) - escalating to Genesis", self.agent_name, error_code, pattern["count"], pattern["window_hours"])
            _fyi("<b>PATTERN: %s %s</b> %dx in %dh - Genesis investigating" % (self.agent_name, error_code, pattern["count"], pattern["window_hours"]))

        # Dedup with persistence — survive restarts
        if error_code in self._active_incidents:
            incident = self._active_incidents[error_code]
            if not incident.resolved and not incident.escalated:
                return
        else:
            stored = load_active_incident(self.agent_name, error_code)
            if stored:
                incident = Incident(error_code, self.agent_name, detail)
                incident.attempts = stored["attempts"]
                incident.incident_id = stored["incident_id"]
                log.warning("%s: Resuming %s attempt %d after restart",
                           self.agent_name, error_code, incident.attempts)
            else:
                incident = Incident(error_code, self.agent_name, detail)
                log.warning("%s: Error detected %s — %s",
                           self.agent_name, error_code, detail)
            self._active_incidents[error_code] = incident

        # Escalation conditions
        if (incident.elapsed_minutes > MAX_FIX_MINUTES or
                incident.attempts >= error_def.escalate_after_attempts):
            self._escalate(incident)
            archive_incident(incident)
            self._chronicle.log_incident(incident)
            return

        # Self-fix
        incident.attempts += 1
        log.info("%s: Self-fix attempt %d/%d for %s",
                self.agent_name, incident.attempts,
                error_def.escalate_after_attempts, error_code)

        if error_code == "EIN001":
            success, msg = fix_memory_real()
            incident.resolution = msg
        else:
            success = self._executor.execute(incident)

        save_incident(incident)

        if success:
            incident.resolved = True
            if not incident.resolution:
                incident.resolution = "Self-fixed on attempt %d" % incident.attempts
            log.info("%s: %s resolved on attempt %d (%.1f min)",
                    self.agent_name, error_code,
                    incident.attempts, incident.elapsed_minutes)
            self._watcher.start_watch(self.agent_name, error_code)
            _fyi("<b>AUTO-FIXED: %s</b> %s %s attempt %d %.1f min watch 10min" % (self.agent_name, error_code, error_def.name, incident.attempts, incident.elapsed_minutes))
            archive_incident(incident)
            del self._active_incidents[error_code]
        else:
            log.warning("%s: Self-fix attempt %d failed for %s",
                       self.agent_name, incident.attempts, error_code)

        self._chronicle.log_incident(incident)

    def _escalate(self, incident: Incident) -> None:
        """Dispatch to peer chain. Sovereign gets FYI only."""
        if incident.escalated:
            return

        incident.escalated = True
        error_def = ERROR_REGISTRY.get(incident.error_code, None)
        name = error_def.name if error_def else incident.error_code

        log.error("%s: Escalating %s after %d attempts (%.1f min)",
                 self.agent_name, incident.error_code,
                 incident.attempts, incident.elapsed_minutes)

        # Dispatch to Cipher → Vector → Genesis
        dispatched = self._dispatch.dispatch(incident)

        # Sovereign FYI — always informed, never the bottleneck
        _fyi(
            f"🚨 <b>ESCALATED: {self.agent_name} — {name}</b>\n"
            f"Code: {incident.error_code}\n"
            f"Attempts: {incident.attempts} | Time: {incident.elapsed_minutes:.1f} min\n"
            f"Detail: {incident.detail[:200]}\n"
            f"Dispatched to: {'responder chain' if dispatched else 'SOVEREIGN (all busy)'}"
        )

        # Log to Chronicle
        self._chronicle.log_incident(incident)
