"""
incident_store.py — Persistent Incident Store
==============================================
Persists active incidents to SQLite so restarts don't reset attempt counters.
Prevents duplicate dispatch and restart loops across service restarts.

Also provides:
- Recovery watch period (10 min post-fix monitoring)
- Pattern detection (repeat offenders)
- Cross-agent correlation (simultaneous failures)
"""
from __future__ import annotations

import sqlite3
import time
import logging
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("nexus.health.store")

STORE_DB = os.getenv(
    "HEALTH_STORE_DB",
    "/Users/ahmedsadek/nexus/data/health_incidents.db"
)
WATCH_PERIOD_MINUTES = 10   # after fix, watch for recurrence
PATTERN_THRESHOLD    = 3    # same error N times = pattern
PATTERN_WINDOW_HOURS = 24   # pattern window
CORRELATION_WINDOW_S = 300  # 5 min — simultaneous failure window


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(STORE_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_store() -> None:
    """Initialize incident store tables."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS active_incidents (
            incident_id     TEXT PRIMARY KEY,
            error_code      TEXT NOT NULL,
            agent           TEXT NOT NULL,
            detail          TEXT,
            detected_at     TEXT NOT NULL,
            attempts        INTEGER DEFAULT 0,
            resolved        INTEGER DEFAULT 0,
            escalated       INTEGER DEFAULT 0,
            resolution      TEXT,
            watch_until     TEXT,
            ts              REAL
        );

        CREATE TABLE IF NOT EXISTS incident_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id     TEXT,
            error_code      TEXT,
            agent           TEXT,
            detail          TEXT,
            detected_at     TEXT,
            resolved_at     TEXT,
            attempts        INTEGER,
            escalated       INTEGER,
            resolution      TEXT,
            ts              REAL
        );

        CREATE TABLE IF NOT EXISTS dispatch_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id     TEXT,
            dispatched_to   TEXT,
            dispatched_at   TEXT,
            acknowledged    INTEGER DEFAULT 0,
            resolved        INTEGER DEFAULT 0,
            ts              REAL
        );

        CREATE INDEX IF NOT EXISTS idx_active_agent
            ON active_incidents(agent, error_code);
        CREATE INDEX IF NOT EXISTS idx_history_error
            ON incident_history(error_code, agent, ts);
    """)
    conn.commit()
    conn.close()
    log.info("Incident store initialized: %s", STORE_DB)


# ---------------------------------------------------------------------------
# Active incident persistence
# ---------------------------------------------------------------------------

def save_incident(incident) -> None:
    """Persist incident state to store."""
    try:
        conn = _get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO active_incidents
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
            incident.resolution[:500] if incident.resolution else "",
            time.time(),
        ))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning("Failed to save incident: %s", exc)


def load_active_incident(
    agent: str, error_code: str
) -> Optional[dict]:
    """Load active incident from store if it exists."""
    try:
        conn = _get_conn()
        row = conn.execute("""
            SELECT * FROM active_incidents
            WHERE agent=? AND error_code=?
            AND resolved=0 AND escalated=0
        """, (agent, error_code)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as exc:
        log.warning("Failed to load incident: %s", exc)
        return None


def archive_incident(incident) -> None:
    """Move resolved incident to history."""
    try:
        conn = _get_conn()
        conn.execute("""
            INSERT INTO incident_history
            (incident_id, error_code, agent, detail, detected_at,
             resolved_at, attempts, escalated, resolution, ts)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            incident.incident_id,
            incident.error_code,
            incident.agent,
            incident.detail[:1000],
            incident.detected_at.isoformat(),
            datetime.now(timezone.utc).isoformat(),
            incident.attempts,
            int(incident.escalated),
            incident.resolution[:500] if incident.resolution else "",
            time.time(),
        ))
        conn.execute(
            "DELETE FROM active_incidents WHERE incident_id=?",
            (incident.incident_id,)
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning("Failed to archive incident: %s", exc)


# ---------------------------------------------------------------------------
# Recovery watch period
# ---------------------------------------------------------------------------

class RecoveryWatcher:
    """
    After a fix, watches for recurrence for WATCH_PERIOD_MINUTES.
    If same error recurs within watch period → skip self-fix → escalate immediately.
    Prevents restart loops.
    """

    def __init__(self):
        self._watching: dict[str, datetime] = {}  # key: f"{agent}:{error_code}"
        self._lock = threading.Lock()

    def start_watch(self, agent: str, error_code: str) -> None:
        """Start watch period after successful fix."""
        key = f"{agent}:{error_code}"
        watch_until = datetime.now(timezone.utc) + timedelta(
            minutes=WATCH_PERIOD_MINUTES
        )
        with self._lock:
            self._watching[key] = watch_until
        log.debug("Watch started: %s until %s", key,
                 watch_until.strftime("%H:%M:%S"))

    def is_in_watch_period(self, agent: str, error_code: str) -> bool:
        """True if this error is in its post-fix watch period."""
        key = f"{agent}:{error_code}"
        with self._lock:
            watch_until = self._watching.get(key)
            if not watch_until:
                return False
            if datetime.now(timezone.utc) > watch_until:
                del self._watching[key]
                return False
            return True

    def clear_watch(self, agent: str, error_code: str) -> None:
        """Clear watch period."""
        key = f"{agent}:{error_code}"
        with self._lock:
            self._watching.pop(key, None)


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------

class PatternDetector:
    """
    Reads incident history to identify repeat offenders.
    Same error N+ times in 24 hours = pattern → escalate to Genesis.
    """

    def check_pattern(
        self, agent: str, error_code: str
    ) -> Optional[dict]:
        """
        Returns pattern info if error is recurring, else None.
        Pattern = same error PATTERN_THRESHOLD+ times in PATTERN_WINDOW_HOURS.
        """
        try:
            cutoff = time.time() - (PATTERN_WINDOW_HOURS * 3600)
            conn = _get_conn()
            rows = conn.execute("""
                SELECT COUNT(*) as count,
                       MIN(detected_at) as first_seen,
                       MAX(detected_at) as last_seen
                FROM incident_history
                WHERE agent=? AND error_code=? AND ts > ?
            """, (agent, error_code, cutoff)).fetchone()
            conn.close()

            if rows and rows["count"] >= PATTERN_THRESHOLD:
                return {
                    "agent":      agent,
                    "error_code": error_code,
                    "count":      rows["count"],
                    "first_seen": rows["first_seen"],
                    "last_seen":  rows["last_seen"],
                    "window_hours": PATTERN_WINDOW_HOURS,
                }
        except Exception as exc:
            log.warning("Pattern check failed: %s", exc)
        return None

    def get_all_patterns(self) -> list[dict]:
        """Get all active patterns across all agents."""
        try:
            cutoff = time.time() - (PATTERN_WINDOW_HOURS * 3600)
            conn = _get_conn()
            rows = conn.execute("""
                SELECT agent, error_code,
                       COUNT(*) as count,
                       MIN(detected_at) as first_seen,
                       MAX(detected_at) as last_seen
                FROM incident_history
                WHERE ts > ?
                GROUP BY agent, error_code
                HAVING COUNT(*) >= ?
                ORDER BY count DESC
            """, (cutoff, PATTERN_THRESHOLD)).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("Pattern scan failed: %s", exc)
            return []


# ---------------------------------------------------------------------------
# Cross-agent correlation
# ---------------------------------------------------------------------------

class CorrelationEngine:
    """
    Detects when 2+ agents fail within CORRELATION_WINDOW_S seconds.
    Correlated failures = system-level event, not individual failures.
    Routes to Sovereign immediately with correlation context.
    """

    def __init__(self):
        self._recent: list[dict] = []  # recent incidents
        self._lock = threading.Lock()
        self._alerted: set[str] = set()  # prevent duplicate alerts

    def record(self, agent: str, error_code: str) -> Optional[dict]:
        """
        Record a new incident and check for correlation.
        Returns correlation info if 2+ agents failed simultaneously.
        """
        now = time.time()
        entry = {
            "agent":      agent,
            "error_code": error_code,
            "ts":         now,
        }

        with self._lock:
            # Add new incident
            self._recent.append(entry)

            # Clean up old entries outside window
            cutoff = now - CORRELATION_WINDOW_S
            self._recent = [e for e in self._recent if e["ts"] > cutoff]

            # Check for correlation — 2+ different agents in window
            agents_in_window = {e["agent"] for e in self._recent}
            if len(agents_in_window) >= 2:
                # Build correlation key to avoid duplicate alerts
                corr_key = ":".join(sorted(agents_in_window))
                if corr_key not in self._alerted:
                    self._alerted.add(corr_key)
                    # Clear old alert keys after 10 min
                    threading.Timer(
                        600, lambda k=corr_key: self._alerted.discard(k)
                    ).start()
                    return {
                        "agents":       list(agents_in_window),
                        "incidents":    list(self._recent),
                        "window_s":     CORRELATION_WINDOW_S,
                        "likely_cause": self._diagnose(agents_in_window),
                    }
        return None

    def _diagnose(self, agents: set) -> str:
        """Infer likely shared cause from set of failing agents."""
        if len(agents) >= 4:
            return "System-wide failure — possible Mac Mini crash or network partition"
        if "OMNI" in agents and "alpha-buffer" in agents:
            return "Pipeline failure — concordance chain broken"
        if "Axiom" in agents and "alpha-execution" in agents:
            return "Risk layer failure — VIX or regime data unavailable"
        if "ORACLE" in agents and "OMNI" in agents:
            return "Data layer failure — market context unavailable"
        return "Shared dependency failure — check message bus and network"


# ---------------------------------------------------------------------------
# Real memory fix
# ---------------------------------------------------------------------------

def fix_memory_real() -> tuple[bool, str]:
    """
    Real memory fix: identify top nexus service consuming RAM and restart it.
    Not log rotation — actual memory pressure relief.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=10
        )

        nexus_processes = []
        for line in result.stdout.split("\n"):
            if "/nexus/" in line or "uvicorn" in line or "openclaw" not in line.lower():
                parts = line.split()
                if len(parts) > 5:
                    try:
                        mem_pct = float(parts[3])
                        pid = parts[1]
                        cmd = " ".join(parts[10:])[:80]
                        if mem_pct > 5.0 and "/nexus/" in cmd:
                            nexus_processes.append({
                                "pid": pid,
                                "mem_pct": mem_pct,
                                "cmd": cmd,
                            })
                    except (ValueError, IndexError):
                        pass

        if not nexus_processes:
            return False, "No high-memory nexus processes found"

        # Sort by memory descending
        nexus_processes.sort(key=lambda x: x["mem_pct"], reverse=True)
        top = nexus_processes[0]

        log.warning("High memory process: PID=%s MEM=%.1f%% CMD=%s",
                   top["pid"], top["mem_pct"], top["cmd"])

        # Determine which service and restart via launchctl
        service_map = {
            "omni":            "ai.nexus.omni",
            "alpha-buffer":    "ai.nexus.alpha-buffer",
            "alpha-execution": "ai.nexus.alpha-execution",
            "axiom":           "ai.nexus.axiom",
            "oracle":          "ai.nexus.oracle",
            "ails":            "ai.nexus.ails",
        }

        target_plist = None
        for key, plist in service_map.items():
            if key in top["cmd"].lower():
                target_plist = plist
                break

        if not target_plist:
            return False, f"Cannot identify service for PID {top['pid']}"

        plist_path = os.path.expanduser(
            f"~/Library/LaunchAgents/{target_plist}.plist"
        )
        subprocess.run(["launchctl", "unload", plist_path],
                      capture_output=True, timeout=10)
        import time as _time
        _time.sleep(3)
        subprocess.run(["launchctl", "load", plist_path],
                      capture_output=True, timeout=10)

        return True, (
            f"Restarted {target_plist} "
            f"(was using {top['mem_pct']:.1f}% RAM)"
        )

    except Exception as exc:
        return False, f"Memory fix failed: {exc}"


# ---------------------------------------------------------------------------
# Health mesh status
# ---------------------------------------------------------------------------

def get_health_mesh_status(monitors: list) -> dict:
    """
    Returns current status of all health monitors.
    Exposed via /health-mesh endpoint.
    """
    now = time.time()
    return {
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "monitor_count": len(monitors),
        "monitors": [
            {
                "agent":          m.agent_name,
                "port":           m.service_port,
                "running":        m._running,
                "active_incidents": len(m._active_incidents),
                "incident_codes": list(m._active_incidents.keys()),
            }
            for m in monitors
        ],
        "patterns":      PatternDetector().get_all_patterns(),
    }
