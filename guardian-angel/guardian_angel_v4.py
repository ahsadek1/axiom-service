"""
Guardian Angel v4 — Full Coverage Rebuild
==========================================
V3 covered 43% of real failure points. V4 covers 100% — infrastructure AND
trading pipeline — with structured escalation, OMNI+Cipher redundancy, and
self-monitoring.

Adds on top of V3:
  1. AILS (port 8008) in SERVICES
  2. Full DEPENDENCY_GRAPH (agents + external APIs)
  3. PipelineTelemetry — 8 pipeline-level checks (4a-4h)
  4. ExternalAPIProbe — 5 API health probes every 15 min
  5. Self-heartbeat file write to /tmp/nexus_guardian_heartbeat
  6. EscalationEngine — structured 3-tier ladder replacing ad-hoc alerts
  7. MultiAIBrain — Claude + OMNI + Cipher adversarial diagnosis
  8. GENESIS notification path on Tier 2+

Ahmed's Law: "Everything we build, we aim at IDEAL and nothing less."
"""

import os
import sys
import time
import json
import gzip
import math
import signal
import hashlib
import logging
import sqlite3
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import psutil
import requests
import sys as _sys
_sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
from alert_client import send_alert as _send_alert

# Serializes backup writes vs TelemetryWriter to prevent SQLite deadlock.
# Acquired by _backup_dbs; TelemetryWriter yields when this is held.
_BACKUP_LOCK = threading.Lock()
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env", override=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _require(key: str) -> str:
    """Load required env var; raise loudly on missing."""
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Required env var '{key}' is not set.")
    return val


TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_AHMED_CHAT_ID: str = _require("TELEGRAM_AHMED_CHAT_ID")
ALPACA_API_KEY: str = _require("ALPACA_API_KEY")
# Support both env var names — ALPACA_API_SECRET is canonical, ALPACA_SECRET_KEY is legacy
ALPACA_API_SECRET: str = os.environ.get("ALPACA_API_SECRET", os.environ.get("ALPACA_SECRET_KEY", ""))
ALPACA_BASE_URL: str = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
NEXUS_ROOT: str = os.environ.get("NEXUS_ROOT", "/Users/ahmedsadek/nexus")
DATA_DIR: str = os.environ.get("DATA_DIR", f"{NEXUS_ROOT}/data")
LOGS_DIR: str = os.environ.get("LOGS_DIR", f"{NEXUS_ROOT}/logs")
BACKUP_DIR: str = os.environ.get("BACKUP_DIR", f"{DATA_DIR}/backups")
HEALING_DB_PATH: str = os.environ.get("HEALING_DB_PATH", f"{DATA_DIR}/healing.db")
TELEMETRY_DB_PATH: str = os.environ.get("TELEMETRY_DB_PATH", f"{DATA_DIR}/telemetry.db")
NEXUS_SECRET: str = _require("NEXUS_SECRET")
# GAP-B fix (2026-04-27): Prime uses X-Nexus-Prime-Secret, not X-Nexus-Secret.
# Guardian was POSTing /resume with the wrong header → silent 403 → success=0.
NEXUS_PRIME_SECRET: str = os.environ.get("NEXUS_PRIME_SECRET", "") or NEXUS_SECRET
# AXIOM_SECRET: used to authenticate against Axiom's /trigger-tier1 endpoint.
# Falls back to NEXUS_SECRET since both share the same value — prevents 403 if
# AXIOM_SECRET is absent from the environment (the original heal auth bug).
AXIOM_SECRET: str = os.environ.get("AXIOM_SECRET", "") or NEXUS_SECRET
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
DEEPSEEK_API_KEY: str = os.environ.get("DEEPSEEK_API_KEY", "")
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
ORATS_API_KEY: str = os.environ.get("ORATS_API_KEY", "")
OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")

MONITOR_INTERVAL_S: int = int(os.environ.get("MONITOR_INTERVAL_S", "60"))
TELEMETRY_INTERVAL_S: float = float(os.environ.get("TELEMETRY_INTERVAL_S", "1.0"))
FORECAST_HORIZON_MIN: int = int(os.environ.get("FORECAST_HORIZON_MIN", "15"))
BASELINE_WINDOW_DAYS: int = int(os.environ.get("BASELINE_WINDOW_DAYS", "30"))
ANOMALY_SIGMA: float = float(os.environ.get("ANOMALY_SIGMA", "3.0"))
AI_CONFIDENCE_THRESHOLD: float = float(os.environ.get("AI_CONFIDENCE_THRESHOLD", "0.80"))

# P1 fix: moved from /tmp (cleared on reboot) to persistent path.
# /tmp wipe on reboot meant GA crash post-reboot left no stale heartbeat to detect.
HEARTBEAT_PATH: str = "/Users/ahmedsadek/nexus/data/guardian_heartbeat"

# ── Block 9: CHRONICLE cron log ───────────────────────────────────────────────
CHRONICLE_DB_PATH: str = "/Users/ahmedsadek/nexus/data/chronicle.db"

def _ensure_cron_log_table() -> None:
    """Block 9: Create cron_log table in CHRONICLE if it doesn't exist."""
    try:
        import sqlite3 as _sq
        conn = _sq.connect(CHRONICLE_DB_PATH, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cron_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                cron_id    TEXT    NOT NULL,
                started_at REAL    NOT NULL,
                duration_s REAL    NOT NULL,
                outcome    TEXT    NOT NULL,  -- success / error / timeout
                error_msg  TEXT,
                created_at TEXT    NOT NULL
            )
        """)
        conn.commit()
        conn.close()
    except Exception as _e:
        log.warning("Block 9: cron_log table init failed: %s", _e)


def _log_cron(cron_id: str, started_at: float, outcome: str, error_msg: str = "") -> None:
    """Block 9: Write cron execution record to CHRONICLE cron_log.

    Args:
        cron_id:    Identifier for the cron task (e.g. "pipeline_telemetry", "backup").
        started_at: time.time() at task start.
        outcome:    "success", "error", or "timeout".
        error_msg:  Error detail if outcome != "success".
    """
    try:
        import sqlite3 as _sq
        from datetime import datetime as _dt, timezone as _tz
        duration_s = time.time() - started_at
        conn = _sq.connect(CHRONICLE_DB_PATH, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO cron_log (cron_id, started_at, duration_s, outcome, error_msg, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cron_id, started_at, round(duration_s, 3), outcome,
             error_msg[:500] if error_msg else None,
             _dt.now(_tz.utc).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as _e:
        log.warning("Block 9: _log_cron failed for %s: %s", cron_id, _e)

# ---------------------------------------------------------------------------
# Service registry + dependency graph
# ---------------------------------------------------------------------------

SERVICES: List[Dict[str, Any]] = [
    {"name": "axiom",           "port": 8001, "health_path": "/health", "launchd": "ai.nexus.axiom",           "db": "axiom.db"},
    {"name": "alpha-buffer",    "port": 8002, "health_path": "/health", "launchd": "ai.nexus.alpha-buffer",    "db": "alpha_buffer.db"},
    {"name": "prime-buffer",    "port": 8003, "health_path": "/health", "launchd": "ai.nexus.prime-buffer",    "db": "prime_buffer.db"},  # S3 audit: no unauthenticated /status calls found in GA V4
    {"name": "omni",            "port": 8004, "health_path": "/health", "launchd": "ai.nexus.omni",            "db": "omni.db"},
    {"name": "alpha-execution", "port": 8005, "health_path": "/health", "launchd": "ai.nexus.alpha-execution", "db": "alpha_execution.db"},
    {"name": "prime-execution", "port": 8006, "health_path": "/health", "launchd": "ai.nexus.prime-execution", "db": "prime_execution.db"},
    # Block 2/6: Oracle uses /health (not /ping) so GA can see service_mode, warm_tickers, and status.
    # GA suppresses Oracle heals when status='warming' — that is normal post-restart state.
    {"name": "oracle",          "port": 8007, "health_path": "/health", "launchd": "ai.nexus.oracle",          "db": "oracle.db"},
    {"name": "ails",            "port": 8008, "health_path": "/health", "launchd": "ai.nexus.ails",            "db": "ails.db"},
    # Agent scanning services — critical for concordance formation
    {"name": "cipher",          "port": 9001, "health_path": "/health", "launchd": "ai.nexus.cipher",          "db": "cipher.db"},
    {"name": "atlas",           "port": 9002, "health_path": "/health", "launchd": "ai.nexus.atlas",           "db": "atlas.db"},
    {"name": "sage",            "port": 9003, "health_path": "/health", "launchd": "ai.nexus.sage",           "db": "sage.db"},
    # Governance + audit DBs — critical for Chronicle history and mandate compliance
    {"name": "chronicle",       "port": None,  "health_path": None, "launchd": None, "db": "chronicle.db"},
    {"name": "pipeline-sentinel","port": None,  "health_path": None, "launchd": None, "db": "pipeline_sentinel.db"},
    # SQS databases — trade history and capital deployment state
    {"name": "sqs-atm-multiweek","port": 9004,  "health_path": "/health", "launchd": "com.sqs.multiweek",     "db": None, "ext_db": "/Users/ahmedsadek/sqs/atm-multi-week/data/atm-multi-week.db"},
    {"name": "sqs-atm-0dte",     "port": 9005,  "health_path": "/health", "launchd": "com.sqs.0dte",         "db": None, "ext_db": "/Users/ahmedsadek/sqs/atm-0dte/data/atm.db"},
    {"name": "sqs-atg-swing",    "port": 9006,  "health_path": "/health", "launchd": "com.sqs.swing",        "db": None, "ext_db": "/Users/ahmedsadek/sqs/atg-swing/atg_swing.db"},
    {"name": "sqs-atg-intraday", "port": 9007,  "health_path": "/health", "launchd": "com.sqs.intraday",     "db": None, "ext_db": "/Users/ahmedsadek/sqs/atg-intraday/atg_intraday.db"},
    {"name": "sqs-capital-router","port": 8000, "health_path": "/health", "launchd": "com.sqs.capital-router","db": None, "ext_db": "/Users/ahmedsadek/sqs/capital-router/capital_router.db"},
]

# Full V4 dependency graph — includes external API dependencies
DEPENDENCY_GRAPH: Dict[str, List[str]] = {
    "oracle":           ["orats_api", "polygon_api"],
    "axiom":            ["oracle"],
    "alpha-buffer":     ["axiom", "oracle"],
    "prime-buffer":     ["axiom", "oracle"],
    "omni":             ["alpha-buffer", "prime-buffer", "oracle",
                         "anthropic_api", "gemini_api", "deepseek_api", "openai_api"],
    "alpha-execution":  ["omni", "alpaca_api"],
    "prime-execution":  ["omni", "alpaca_api"],
    "cipher":           ["oracle", "alpha-buffer", "prime-buffer", "anthropic_api"],
    "atlas":            ["oracle", "alpha-buffer", "prime-buffer", "gemini_api"],
    "sage":             ["oracle", "alpha-buffer", "prime-buffer", "deepseek_api"],
    "ails":             [],
}

ENV_FILES: List[str] = [f"{NEXUS_ROOT}/{s['name']}/.env" for s in SERVICES]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_dir = Path(LOGS_DIR) / "guardian-angel"
_log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(_log_dir / "guardian_v4.log"), mode="a"),
    ],
)
log = logging.getLogger("guardian.v4")

# ---------------------------------------------------------------------------
# Telemetry + Baseline DB
# ---------------------------------------------------------------------------

def _init_telemetry_db(path: str) -> sqlite3.Connection:
    """Initialize telemetry.db — time series + baselines."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)  # Cipher fix: busy_timeout
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")  # Cipher fix: 30s busy timeout for lock contention
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            service     TEXT    NOT NULL,
            signal_name TEXT    NOT NULL,
            value       REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_signals_lookup
            ON signals (service, signal_name, ts);

        CREATE TABLE IF NOT EXISTS baselines (
            service      TEXT NOT NULL,
            signal_name  TEXT NOT NULL,
            hour_of_day  INTEGER NOT NULL,
            day_of_week  INTEGER NOT NULL,
            mean         REAL NOT NULL DEFAULT 0,
            std          REAL NOT NULL DEFAULT 1,
            n            INTEGER NOT NULL DEFAULT 0,
            last_updated REAL NOT NULL,
            PRIMARY KEY (service, signal_name, hour_of_day, day_of_week)
        );

        CREATE TABLE IF NOT EXISTS anomalies_v3 (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at  REAL    NOT NULL,
            service      TEXT    NOT NULL,
            signal_name  TEXT    NOT NULL,
            observed     REAL    NOT NULL,
            expected     REAL    NOT NULL,
            sigma        REAL    NOT NULL,
            root_cause   TEXT,
            prevented    INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS heal_outcomes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL    NOT NULL,
            root_cause   TEXT    NOT NULL,
            patch_type   TEXT    NOT NULL,
            success      INTEGER NOT NULL,
            details      TEXT
        );

        CREATE TABLE IF NOT EXISTS coherence_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL    NOT NULL,
            check_name   TEXT    NOT NULL,
            passed       INTEGER NOT NULL,
            details      TEXT
        );
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Healing DB
# ---------------------------------------------------------------------------

def _init_healing_db(path: str) -> sqlite3.Connection:
    """Initialize healing.db with all v2 tables + v3/v4 extensions."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)  # Cipher fix: busy_timeout
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")  # Cipher fix: 30s busy timeout for lock contention
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS anomalies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL,
            service TEXT NOT NULL,
            anomaly_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            details TEXT,
            resolved INTEGER DEFAULT 0,
            resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS healing_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anomaly_id INTEGER,
            action_at TEXT NOT NULL,
            service TEXT NOT NULL,
            heal_type TEXT NOT NULL,
            success INTEGER NOT NULL,
            time_to_heal_s REAL,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS patterns (
            service TEXT NOT NULL,
            anomaly_type TEXT NOT NULL,
            occurrence_count INTEGER DEFAULT 1,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            UNIQUE(service, anomaly_type)
        );
        CREATE TABLE IF NOT EXISTS flags (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            set_at TEXT NOT NULL,
            set_by TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prevented_crashes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            service TEXT NOT NULL,
            signal_name TEXT NOT NULL,
            minutes_before TEXT NOT NULL,
            action_taken TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _send_telegram(message: str, tier: int = 2, dedup_key: Optional[str] = None) -> None:
    """Route alert through Alert Broker. tier=1 → silent.
    
    Args:
        message: Alert text (first 200 chars = title, remainder = body).
        tier: Alert tier (1=silent, 2=warning, 3=critical, 4=critical).
        dedup_key: Deduplication key for cooldown. If None, defaults to derived from title.
                   IMPORTANT: Use condition-based keys (no timestamps) for proper dedup.
    """
    if tier < 2:
        return
    level_map = {2: "WARNING", 3: "CRITICAL", 4: "CRITICAL"}
    level = level_map.get(tier, "WARNING")
    targets = ["ahmed", "nexus_health_group"] if tier >= 3 else ["nexus_health_group"]
    
    title = message[:200]
    body = message[200:] if len(message) > 200 else ""
    
    # Use provided dedup_key, or auto-derive from title (alert_client will use title if None)
    # Condition-based keys are essential for dedup to work across repeated alerts
    _send_alert(
        source="guardian-angel-v4",
        level=level,
        title=title,
        body=body,
        dedup_key=dedup_key,
        targets=targets,
    )


# ---------------------------------------------------------------------------
# PRIMUS & CIPHER SQS Alert Routing (Ahmed Directive 2026-05-17)
# ---------------------------------------------------------------------------

def _notify_primus_sqs(component: str, failure_detail: str, log_tail: str = "") -> None:
    """Route SQS service alerts to PRIMUS (primary), fallback to CIPHER.
    
    SQS alerts MUST go to Primus first. If Primus unresponsive after 30s,
    escalate to Cipher. This ensures SQS issues are owned by the SQS engineer.
    """
    text = f"🚨 [GUARDIAN] SQS ALERT: {component}. {failure_detail}"
    if log_tail:
        text += f" | Log: {log_tail[:150]}"
    
    try:
        # Primary: route to PRIMUS main session
        subprocess.run(
            ["openclaw", "sessions", "send", 
             "--session-key", "agent:primus:main",
             "--message", text],
            capture_output=True, timeout=10,
        )
        log.info("SQS alert routed to PRIMUS: %s", component)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.warning("PRIMUS notify failed, escalating to CIPHER: %s", exc)
        try:
            # Fallback: route to CIPHER
            subprocess.run(
                ["openclaw", "sessions", "send",
                 "--session-key", "agent:cipher:main",
                 "--message", f"PRIMUS UNRESPONSIVE - {text}"],
                capture_output=True, timeout=10,
            )
            log.info("SQS alert escalated to CIPHER (PRIMUS unresponsive)")
        except Exception as cipher_exc:
            log.error("Both PRIMUS and CIPHER notify failed: %s", cipher_exc)


# ---------------------------------------------------------------------------
# GENESIS notification (non-SQS services)
# ---------------------------------------------------------------------------

def _notify_genesis(component: str, failure_detail: str, log_tail: str = "") -> None:
    """Fire a direct message to GENESIS agent via OpenClaw sessions_send.
    
    This is CRITICAL — orphan positions and other Tier 2+ failures MUST reach
    the GENESIS agent immediately so it can act. No fallback, no suppression.
    """
    text = f"🚨 [GUARDIAN ESCALATION] {component}\n\n{failure_detail}"
    if log_tail:
        text += f"\n\nContext: {log_tail[:200]}"
    
    try:
        result = subprocess.run(
            ["openclaw", "sessions", "send",
             "--session-key", "agent:genesis:main",
             "--message", text],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            log.info("GENESIS notify succeeded for %s", component)
        else:
            log.error("GENESIS notify failed (exit %d): %s", result.returncode, result.stderr.decode('utf-8', errors='ignore'))
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.error("GENESIS notify exception: %s — this is CRITICAL, Guardian Angel escalation failed", exc)


# ---------------------------------------------------------------------------
# launchctl helpers
# ---------------------------------------------------------------------------

def _launchctl_restart(service_name: str) -> bool:
    """Restart a launchd service."""
    uid = os.getuid()
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/ai.nexus.{service_name}"],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.error("launchctl restart failed for %s: %s", service_name, exc)
        return False


def _get_service_pid(service_name: str) -> Optional[int]:
    """Get PID of a running launchd service."""
    try:
        result = subprocess.run(
            ["launchctl", "list", f"ai.nexus.{service_name}"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.split("\n"):
            if '"PID"' in line:
                try:
                    return int(line.split("=")[-1].strip().rstrip(";").strip())
                except (ValueError, IndexError):
                    pass
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _read_stderr_tail(service_name: str, lines: int = 100) -> str:
    """Read last N lines of a service's stderr log."""
    log_path = Path(LOGS_DIR) / service_name / "stderr.log"
    if not log_path.exists():
        return ""
    try:
        result = subprocess.run(
            ["tail", f"-{lines}", str(log_path)],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


# ---------------------------------------------------------------------------
# Alpaca helper
# ---------------------------------------------------------------------------

def _alpaca_get(path: str) -> Optional[Any]:
    """GET from Alpaca paper API."""
    try:
        resp = requests.get(
            f"{ALPACA_BASE_URL}{path}",
            headers={
                "APCA-API-KEY-ID": ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
            },
            timeout=10,
        )
        return resp.json() if resp.ok else None
    except requests.RequestException:
        return None


# ---------------------------------------------------------------------------
# 1. Telemetry Collector
# ---------------------------------------------------------------------------

class TelemetryCollector:
    """
    Samples signals per service every 1 second.
    Stores to telemetry.db for baseline computation and anomaly detection.
    """

    SIGNALS = [
        "memory_pct", "cpu_pct", "num_threads", "num_fds",
        "response_time_ms", "http_ok",
    ]

    def __init__(self, telemetry_conn: sqlite3.Connection) -> None:
        self._conn = telemetry_conn
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Callback: called with new conn after prune_old() recreates the DB.
        self._on_conn_replaced: Optional[callable] = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()
        log.info("Telemetry collector started (interval=%.1fs)", TELEMETRY_INTERVAL_S)

    def stop(self) -> None:
        self._running = False

    def _collect_loop(self) -> None:
        while self._running:
            # Yield if backup is running — prevents SQLite deadlock on WAL checkpoint.
            if _BACKUP_LOCK.locked():
                time.sleep(0.1)
                continue
            t0 = time.time()
            for svc in SERVICES:
                self._sample_service(svc)
            elapsed = time.time() - t0
            time.sleep(max(0.0, TELEMETRY_INTERVAL_S - elapsed))

    def _sample_service(self, svc: Dict[str, Any]) -> None:
        name = svc["name"]
        now = time.time()
        readings: Dict[str, float] = {}

        url = f"http://localhost:{svc['port']}{svc['health_path']}"
        t_http = time.time()
        try:
            resp = requests.get(url, timeout=3, headers={"X-Nexus-Secret": NEXUS_SECRET})
            readings["response_time_ms"] = (time.time() - t_http) * 1000
            readings["http_ok"] = 1.0 if resp.ok else 0.0
        except requests.RequestException:
            readings["response_time_ms"] = 3000.0
            readings["http_ok"] = 0.0

        pid = _get_service_pid(name)
        if pid:
            try:
                proc = psutil.Process(pid)
                readings["memory_pct"] = proc.memory_percent()
                readings["cpu_pct"] = proc.cpu_percent(interval=None)
                readings["num_threads"] = proc.num_threads()
                try:
                    readings["num_fds"] = proc.num_fds()
                except (AttributeError, psutil.AccessDenied):
                    readings["num_fds"] = 0.0
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        rows = [(now, name, sig, val) for sig, val in readings.items()]
        try:
            with self._lock:
                self._conn.executemany(
                    "INSERT INTO signals (ts, service, signal_name, value) VALUES (?,?,?,?)",
                    rows,
                )
                self._conn.commit()
        except (MemoryError, sqlite3.DatabaseError) as exc:
            log.error("_sample_service DB write failed for %s (non-fatal): %s", name, exc)

    def get_recent(self, service: str, signal: str, window_s: int = 300) -> List[float]:
        cutoff = time.time() - window_s
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT value FROM signals WHERE service=? AND signal_name=? AND ts>=? ORDER BY ts",
                    (service, signal, cutoff),
                ).fetchall()
            return [r[0] for r in rows]
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
            # Cipher fix: recover from locking protocol / file corruption errors.
            # Reconnect and retry once; return empty list if retry also fails.
            log.error("get_recent DB error (%s/%s): %s — attempting reconnect", service, signal, exc)
            try:
                db_path = os.environ.get("TELEMETRY_DB_PATH", TELEMETRY_DB_PATH)
                with self._lock:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
                    self._conn.execute("PRAGMA journal_mode=WAL")
                    self._conn.execute("PRAGMA busy_timeout=30000")
                    rows = self._conn.execute(
                        "SELECT value FROM signals WHERE service=? AND signal_name=? AND ts>=? ORDER BY ts",
                        (service, signal, cutoff),
                    ).fetchall()
                log.info("get_recent reconnect succeeded for %s/%s", service, signal)
                return [r[0] for r in rows]
            except Exception as retry_exc:
                log.error("get_recent retry failed for %s/%s: %s — returning []", service, signal, retry_exc)
                return []

    def prune_old(self, keep_days: int = 30) -> None:
        """Delete telemetry older than keep_days. Self-heals on DB corruption."""
        cutoff = time.time() - keep_days * 86400
        try:
            with self._lock:
                self._conn.execute("DELETE FROM signals WHERE ts<?", (cutoff,))
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.commit()
        except sqlite3.DatabaseError as exc:
            log.error("prune_old — DB corruption detected: %s. Recreating telemetry.db.", exc)
            try:
                with self._lock:
                    self._conn.close()
                db_path = os.environ.get(
                    "TELEMETRY_DB_PATH",
                    os.path.join(os.environ.get("DATA_DIR", "/Users/ahmedsadek/nexus/data"), "telemetry.db"),
                )
                for suffix in ("", "-shm", "-wal"):
                    p = db_path + suffix
                    if os.path.exists(p):
                        os.unlink(p)
                        log.info("Deleted corrupted file: %s", p)
                self._conn = _init_telemetry_db(db_path)
                log.info("Telemetry DB recreated successfully at %s", db_path)
                if self._on_conn_replaced:
                    try:
                        self._on_conn_replaced(self._conn)
                        log.info("Telemetry conn propagated to all dependents after DB recreation.")
                    except Exception as cb_exc:
                        log.error("on_conn_replaced callback failed: %s", cb_exc)
            except Exception as heal_exc:
                log.error("prune_old self-heal failed: %s", heal_exc)


# ---------------------------------------------------------------------------
# 2. Baseline Engine
# ---------------------------------------------------------------------------

class BaselineEngine:
    """Statistical baselines per (service, signal, hour_of_day, day_of_week)."""

    def __init__(self, telemetry_conn: sqlite3.Connection,
                 collector: TelemetryCollector) -> None:
        self._conn = telemetry_conn
        self._collector = collector
        self._lock = threading.Lock()

    def update_baselines(self) -> None:
        """Recompute baselines from last 30 days of telemetry. Run every hour."""
        try:
            cutoff = time.time() - BASELINE_WINDOW_DAYS * 86400
            for svc in SERVICES:
                for signal in TelemetryCollector.SIGNALS:
                    rows = self._conn.execute(
                        "SELECT ts, value FROM signals "
                        "WHERE service=? AND signal_name=? AND ts>=? ORDER BY ts",
                        (svc["name"], signal, cutoff),
                    ).fetchall()
                    if len(rows) < 10:
                        continue
                    buckets: Dict[Tuple[int, int], List[float]] = {}
                    for ts, val in rows:
                        dt = datetime.fromtimestamp(ts)
                        key = (dt.hour, dt.weekday())
                        buckets.setdefault(key, []).append(val)

                    for (hour, dow), vals in buckets.items():
                        n = len(vals)
                        mean = sum(vals) / n
                        variance = sum((v - mean) ** 2 for v in vals) / n
                        std = max(math.sqrt(variance), 0.001)
                        with self._lock:
                            self._conn.execute(
                                "INSERT INTO baselines "
                                "(service, signal_name, hour_of_day, day_of_week, mean, std, n, last_updated) "
                                "VALUES (?,?,?,?,?,?,?,?) "
                                "ON CONFLICT(service, signal_name, hour_of_day, day_of_week) "
                                "DO UPDATE SET mean=excluded.mean, std=excluded.std, "
                                "n=excluded.n, last_updated=excluded.last_updated",
                                (svc["name"], signal, hour, dow, mean, std, n, time.time()),
                            )
                    with self._lock:
                        self._conn.commit()
        except sqlite3.ProgrammingError:
            log.warning("BaselineEngine.update_baselines: connection closed mid-run, skipping")
        except sqlite3.Error as exc:
            log.warning("BaselineEngine.update_baselines DB error: %s", exc)

    def get_baseline(self, service: str, signal: str,
                     hour: int, dow: int) -> Optional[Tuple[float, float, int]]:
        """Return (mean, std, n) for a service/signal/time bucket, or None."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT mean, std, n FROM baselines "
                    "WHERE service=? AND signal_name=? AND hour_of_day=? AND day_of_week=?",
                    (service, signal, hour, dow),
                ).fetchone()
            return (row[0], row[1], row[2]) if row else None
        except sqlite3.ProgrammingError:
            return None
        except sqlite3.Error as exc:
            log.warning("BaselineEngine.get_baseline DB error: %s", exc)
            return None

    def check_anomaly(self, service: str, signal: str, value: float) -> Optional[float]:
        now = datetime.now()
        baseline = self.get_baseline(service, signal, now.hour, now.weekday())
        if baseline is None or baseline[2] < 30:
            return None
        mean, std, _ = baseline
        sigma = abs(value - mean) / std
        return sigma if sigma >= ANOMALY_SIGMA else None

    def flag_anomaly(self, service: str, signal: str, value: float,
                     sigma: float, conn: sqlite3.Connection) -> None:
        now = datetime.now()
        baseline = self.get_baseline(service, signal, now.hour, now.weekday())
        expected = baseline[0] if baseline else 0.0
        conn.execute(
            "INSERT INTO anomalies_v3 (detected_at, service, signal_name, observed, expected, sigma) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), service, signal, value, expected, sigma),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 3. Causal Graph
# ---------------------------------------------------------------------------

class CausalGraph:
    """Service dependency map + trace-back algorithm."""

    def __init__(self, telemetry_conn: sqlite3.Connection,
                 collector: TelemetryCollector) -> None:
        self._conn = telemetry_conn
        self._collector = collector

    def find_root_cause(self, failing_service: str,
                        known_failing: Optional[List[str]] = None) -> str:
        if known_failing is None:
            known_failing = []

        upstream = DEPENDENCY_GRAPH.get(failing_service, [])
        # Filter to only internal services (not external API strings like "orats_api")
        upstream_services = [s for s in upstream if not s.endswith("_api")]
        if not upstream_services:
            return failing_service

        unhealthy_upstream: List[Tuple[str, float]] = []
        for dep in upstream_services:
            recent = self._collector.get_recent(dep, "http_ok", window_s=120)
            if recent:
                health_rate = sum(recent) / len(recent)
                if health_rate < 0.8:
                    unhealthy_upstream.append((dep, health_rate))

        if not unhealthy_upstream:
            return failing_service

        unhealthy_upstream.sort(key=lambda x: x[1])
        most_unhealthy = unhealthy_upstream[0][0]

        if most_unhealthy in known_failing:
            return failing_service

        return self.find_root_cause(most_unhealthy, known_failing + [failing_service])

    def explain_chain(self, failing_service: str) -> str:
        root = self.find_root_cause(failing_service)
        if root == failing_service:
            return f"`{failing_service}` is the root cause (no upstream issues)"
        return f"`{root}` → `{failing_service}`\nRoot cause: `{root}`"


# ---------------------------------------------------------------------------
# 4. Forecast Engine
# ---------------------------------------------------------------------------

class ForecastEngine:
    """Linear regression on critical signals — predicts crashes before they happen."""

    CRITICAL_THRESHOLDS: Dict[str, float] = {
        "memory_pct":       85.0,
        "cpu_pct":          90.0,
        "response_time_ms": 5000.0,
        "num_fds":          900.0,
    }

    def __init__(self, collector: TelemetryCollector,
                 telemetry_conn: sqlite3.Connection) -> None:
        self._collector = collector
        self._conn = telemetry_conn

    def _linear_regression(self, xs: List[float],
                            ys: List[float]) -> Tuple[float, float]:
        n = len(xs)
        if n < 2:
            return 0.0, 0.0
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
        den = sum((xs[i] - mean_x) ** 2 for i in range(n))
        slope = num / den if den != 0 else 0.0
        intercept = mean_y - slope * mean_x
        return slope, intercept

    def predict_minutes_to_threshold(self, service: str, signal: str) -> Optional[float]:
        threshold = self.CRITICAL_THRESHOLDS.get(signal)
        if threshold is None:
            return None
        values = self._collector.get_recent(service, signal, window_s=600)
        if len(values) < 10:
            return None
        now = time.time()
        n = len(values)
        xs = [now - (n - i) * TELEMETRY_INTERVAL_S for i in range(n)]
        slope, intercept = self._linear_regression(xs, values)
        if slope <= 0:
            return None
        t_threshold = (threshold - intercept) / slope
        return (t_threshold - now) / 60.0

    def check_all_forecasts(self, healing_conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        impending: List[Dict[str, Any]] = []
        for svc in SERVICES:
            for signal in self.CRITICAL_THRESHOLDS:
                minutes = self.predict_minutes_to_threshold(svc["name"], signal)
                if minutes is None:
                    continue
                if 0 < minutes <= FORECAST_HORIZON_MIN:
                    impending.append({
                        "service": svc["name"],
                        "signal": signal,
                        "minutes": round(minutes, 1),
                        "threshold": self.CRITICAL_THRESHOLDS[signal],
                    })
                    now_iso = datetime.now(timezone.utc).isoformat()
                    healing_conn.execute(
                        "INSERT INTO prevented_crashes "
                        "(ts, service, signal_name, minutes_before, action_taken) "
                        "VALUES (?,?,?,?,?)",
                        (now_iso, svc["name"], signal, str(round(minutes, 1)),
                         "PREEMPTIVE_RESTART"),
                    )
                    healing_conn.commit()
                    log.warning(
                        "FORECAST: %s/%s will hit threshold in %.1f min — intervening now",
                        svc["name"], signal, minutes,
                    )
        return impending


# ---------------------------------------------------------------------------
# 5. AI Diagnostic Brain (V3 — preserved for backward compat)
# ---------------------------------------------------------------------------

class AIBrain:
    """Uses Claude to diagnose novel failures the catalog has never seen."""

    def __init__(self) -> None:
        self._available = bool(ANTHROPIC_API_KEY)
        if not self._available:
            log.warning("AI brain disabled — ANTHROPIC_API_KEY not set")

    def diagnose(self, service: str, failure_description: str,
                 stderr_tail: str, health_history: str) -> Optional[Dict[str, Any]]:
        if not self._available:
            return None

        prompt = f"""You are diagnosing a failure in a Python FastAPI microservice called '{service}'.

FAILURE DESCRIPTION:
{failure_description}

LAST 100 LINES OF STDERR:
{stderr_tail}

HEALTH CHECK HISTORY (last 10 min):
{health_history}

SERVICE DEPENDENCY GRAPH:
{json.dumps(DEPENDENCY_GRAPH, indent=2)}

Analyze this failure and respond in EXACTLY this JSON format (no other text):
{{
  "root_cause": "one sentence description of root cause",
  "root_cause_category": "MEMORY_LEAK|DB_CORRUPTION|DEPENDENCY_FAILURE|CONFIG_ERROR|INFINITE_LOOP|RESOURCE_EXHAUSTION|UNKNOWN",
  "fix_type": "RESTART|CHECKPOINT_WAL|CLEAR_CACHE|CONFIG_RESTORE|NONE",
  "fix_rationale": "one sentence explaining why this fix addresses the root cause",
  "confidence": 0.0,
  "rollback": "how to undo the fix if it makes things worse",
  "estimated_fix_time_s": 30
}}

CRITICAL: confidence must be a float 0.0-1.0. Be honest — return low confidence for unknown failure modes."""

        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 600,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            if not resp.ok:
                log.error("AI brain API error: %s", resp.status_code)
                return None

            content = resp.json().get("content", [])
            if not content:
                return None

            text = content[0].get("text", "")
            start = text.find("{")
            end = text.rfind("}") + 1
            if start == -1 or end == 0:
                return None

            return json.loads(text[start:end])

        except (requests.RequestException, json.JSONDecodeError, KeyError) as exc:
            log.error("AI brain diagnosis failed: %s", exc)
            return None

    def execute_diagnosis(self, service: str, diagnosis: Dict[str, Any],
                          healing_conn: sqlite3.Connection) -> bool:
        confidence = diagnosis.get("confidence", 0.0)
        fix_type = diagnosis.get("fix_type", "NONE")
        root_cause = diagnosis.get("root_cause", "Unknown")

        log.info("AI diagnosis for %s: %s (confidence=%.2f, fix=%s)",
                 service, root_cause, confidence, fix_type)

        if confidence < AI_CONFIDENCE_THRESHOLD:
            _send_telegram(
                f"*AI DIAGNOSIS* — `{service}`\n\n"
                f"Root cause: {root_cause}\n"
                f"Proposed fix: {fix_type}\n"
                f"Confidence: {confidence:.0%} (below threshold)\n\n"
                f"*Cannot act autonomously. Your decision required.*",
                tier=4,
            )
            return False

        success = False
        if fix_type == "RESTART":
            success = _launchctl_restart(service)
        elif fix_type == "CHECKPOINT_WAL":
            db_path = str(Path(DATA_DIR) / next(
                (s["db"] for s in SERVICES if s["name"] == service), f"{service}.db"
            ))
            try:
                conn = sqlite3.connect(db_path, timeout=10)
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
                conn.close()
                success = True
            except sqlite3.Error as exc:
                log.error("WAL checkpoint failed: %s", exc)
        elif fix_type == "NONE":
            success = True
        else:
            success = _launchctl_restart(service)

        healing_conn.execute(
            "INSERT INTO heal_outcomes (ts, root_cause, patch_type, success, details) "
            "VALUES (?,?,?,?,?)",
            (time.time(), root_cause, fix_type, int(success),
             f"AI confidence={confidence:.2f}: {diagnosis.get('fix_rationale','')}"),
        )
        healing_conn.commit()

        tier = 2 if success else 3
        _send_telegram(
            f"*AI FIX {'APPLIED' if success else 'FAILED'}* — `{service}`\n\n"
            f"Root cause: {root_cause}\n"
            f"Action: {fix_type}\n"
            f"Confidence: {confidence:.0%}\n"
            f"Result: {'✅ Success' if success else '❌ Failed'}",
            tier=tier,
        )
        return success


# ---------------------------------------------------------------------------
# 6. Self-Learning Heal Catalog
# ---------------------------------------------------------------------------

class HealCatalog:
    """Tracks P(success) per (root_cause, patch_type) — improves over time."""

    CATALOG: Dict[str, List[str]] = {
        "CONNECTION_REFUSED": ["RESTART"],
        "TIMEOUT":            ["RESTART"],
        "HTTP_500":           ["RESTART"],
        "HTTP_503":           ["WAIT_RETRY", "RESTART"],
        "MEMORY_LEAK":        ["RESTART"],
        "DB_CORRUPTION":      ["RESTORE_BACKUP"],
        "WAL_STUCK":          ["CHECKPOINT_WAL"],
        "DISK_CRITICAL":      ["LOG_ROTATION"],
        "CONFIG_TAMPERED":    ["ALERT_ONLY"],
        "EXECUTION_PAUSED":   ["AUTO_RESUME"],
        "AGENT_SILENT":       ["RESTART"],
        "SCHEDULER_DRIFT":    ["RESTART"],
        "UNKNOWN":            ["RESTART", "ALERT_ONLY"],
    }

    def __init__(self, telemetry_conn: sqlite3.Connection) -> None:
        self._conn = telemetry_conn
        self._lock = threading.Lock()

    def best_patch(self, root_cause: str) -> str:
        candidates = self.CATALOG.get(root_cause, ["RESTART"])
        best_patch = candidates[0]
        best_prob = -1.0

        for patch in candidates:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT success FROM heal_outcomes WHERE root_cause=? AND patch_type=?",
                    (root_cause, patch),
                ).fetchall()

            if not rows:
                return candidates[0]

            n = len(rows)
            successes = sum(r[0] for r in rows)
            prob = successes / n

            if prob > best_prob:
                best_prob = prob
                best_patch = patch

        return best_patch

    def record_outcome(self, root_cause: str, patch: str, success: bool,
                       details: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO heal_outcomes (ts, root_cause, patch_type, success, details) "
                "VALUES (?,?,?,?,?)",
                (time.time(), root_cause, patch, int(success), details),
            )
            self._conn.commit()

    def success_rate(self, root_cause: str, patch: str) -> Optional[float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT success FROM heal_outcomes WHERE root_cause=? AND patch_type=?",
                (root_cause, patch),
            ).fetchall()
        if not rows:
            return None
        return sum(r[0] for r in rows) / len(rows)


# ---------------------------------------------------------------------------
# 7. Coherence Verifier
# ---------------------------------------------------------------------------

class CoherenceVerifier:
    """Cross-service state agreement checks — catches silent failures."""

    def __init__(self, healing_conn: sqlite3.Connection,
                 telemetry_conn: sqlite3.Connection,
                 telemetry_db_path: str = TELEMETRY_DB_PATH) -> None:
        self._healing = healing_conn
        self._telemetry = telemetry_conn
        self._telemetry_db_path = telemetry_db_path
        self._last_orphan_alert_ts: float = 0.0

    def _log_check(self, name: str, passed: bool, details: str) -> None:
        """
        Log a coherence check result using a fresh per-call connection.

        Uses its own connection (not self._telemetry) to avoid the race condition
        where TelemetryCollector.prune_old() closes the shared connection between
        the check and the write. A fresh connect/write/close is ~0.5ms and safe.
        """
        try:
            _conn = sqlite3.connect(self._telemetry_db_path, timeout=5)
            try:
                _conn.execute(
                    "INSERT OR IGNORE INTO coherence_log "
                    "(ts, check_name, passed, details) VALUES (?,?,?,?)",
                    (time.time(), name, int(passed), details),
                )
                _conn.commit()
            finally:
                _conn.close()
        except Exception as _e:
            log.warning("_log_check failed for %s: %s", name, _e)

    def check_axiom_pool_alive(self) -> bool:
        if not _is_market_hours():
            return True
        try:
            resp = requests.get(
                "http://localhost:8001/health",
                headers={"X-Nexus-Secret": NEXUS_SECRET},
                timeout=5,
            )
            if not resp.ok:
                return True
            data = resp.json()
            pool_size = data.get("pool_size", data.get("pool_count", -1))
            if isinstance(pool_size, int) and pool_size == 0:
                self._log_check("axiom_pool_alive", False,
                    "Pool empty during market hours — triggering Tier 1 rebuild")
                try:
                    heal_resp = requests.post(
                        "http://localhost:8001/trigger-tier1",
                        headers={"X-Axiom-Secret": AXIOM_SECRET,
                                 "Content-Type": "application/json"},
                        timeout=5,
                    )
                    if heal_resp.status_code == 200:
                        log.warning("AUTO-HEAL: Triggered Tier 1 rebuild (pool was empty)")
                        _send_telegram(
                            "*AUTO-HEAL* — Axiom pool was empty\n"
                            "Tier 1 rebuild triggered. Pool repopulating (~2 min).",
                            tier=3,
                            dedup_key="axiom-pool-empty-healed",
                        )
                    else:
                        _send_telegram(
                            "*AXIOM POOL EMPTY — AUTO-HEAL FAILED*\n"
                            f"HTTP {heal_resp.status_code}. Manual intervention required.",
                            tier=4,
                            dedup_key="axiom-pool-empty-heal-failed",
                        )
                except Exception as exc:
                    _send_telegram(
                        "*AXIOM POOL EMPTY — AXIOM UNREACHABLE*\n"
                        f"{str(exc)[:100]}",
                        tier=4,
                        dedup_key="axiom-unreachable",
                    )
                return False
            self._log_check("axiom_pool_alive", True, f"pool_size={pool_size}")
            return True
        except requests.RequestException:
            return True

    def check_oracle_cache_fresh(self) -> bool:
        try:
            resp = requests.get(
                "http://localhost:8007/ping",
                headers={"X-Nexus-Secret": NEXUS_SECRET},
                timeout=5,
            )
            if not resp.ok:
                return True
            data = resp.json()
            cache_age_s = data.get("cache_age_s", data.get("last_warm_s", 0))
            if isinstance(cache_age_s, (int, float)) and cache_age_s > 1800:
                self._log_check("oracle_cache_fresh", False,
                    f"Cache stale: {cache_age_s/60:.0f}min old")
                _send_telegram(
                    f"*COHERENCE* — ORACLE cache stale ({cache_age_s/60:.0f} min)\n"
                    "Agents getting stale market data.",
                    tier=2,
                    dedup_key="oracle-cache-stale",
                )
                return False
            self._log_check("oracle_cache_fresh", True, f"cache_age={cache_age_s}s")
            return True
        except requests.RequestException:
            return True

    def check_alpaca_db_agreement(self) -> bool:
        alpaca_data = _alpaca_get("/v2/positions")
        if alpaca_data is None:
            return True

        alpaca_syms = {p["symbol"] for p in alpaca_data}
        db_positions: List[str] = []

        for db_name in ["alpha_execution.db", "prime_execution.db"]:
            db_path = Path(DATA_DIR) / db_name
            if not db_path.exists():
                continue
            try:
                conn = sqlite3.connect(str(db_path), timeout=5)
                rows = conn.execute(
                    "SELECT symbol FROM positions WHERE status='open'"
                ).fetchall()
                conn.close()
                db_positions.extend(r[0] for r in rows)
            except sqlite3.Error:
                pass

        db_syms = set(db_positions)
        orphans = alpaca_syms - db_syms

        if orphans:
            self._log_check("alpaca_db_agreement", False,
                f"Orphan positions in Alpaca: {orphans}")
            self._healing.execute(
                "INSERT INTO flags (key, value, set_at, set_by) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "set_at=excluded.set_at, set_by=excluded.set_by",
                ("healing_active", "true",
                 datetime.now(timezone.utc).isoformat(), "coherence_verifier"),
            )
            self._healing.commit()
            closed = []
            failed_close = []
            for sym in orphans:
                try:
                    resp = requests.delete(
                        f"{ALPACA_BASE_URL}/v2/positions/{sym}",
                        headers={
                            "APCA-API-KEY-ID":     ALPACA_API_KEY,
                            "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
                        },
                        timeout=10,
                    )
                    if resp.status_code in (200, 204):
                        closed.append(sym)
                        log.warning("AUTO-CLOSED orphan position: %s", sym)
                    else:
                        failed_close.append(sym)
                        log.error("Failed to close orphan %s: HTTP %d", sym, resp.status_code)
                except Exception as exc:
                    failed_close.append(sym)
                    log.error("Exception closing orphan %s: %s", sym, exc)

            now_ts = time.time()
            if now_ts - self._last_orphan_alert_ts >= 3600:
                self._last_orphan_alert_ts = now_ts
                if closed and not failed_close:
                    _send_telegram(
                        f"*ORPHANS AUTO-CLOSED* — {closed}\n"
                        f"Positions in Alpaca but not DB. Auto-liquidated.",
                        tier=3,
                        dedup_key="orphan-positions-closed",
                    )
                else:
                    _send_telegram(
                        f"*ORPHAN POSITIONS — ACTION REQUIRED*\n"
                        f"Closed: {closed} | Failed: {failed_close}\n"
                        f"Manually close {failed_close} in Alpaca immediately.",
                        tier=4,
                        dedup_key="orphan-positions-alert",
                    )
                    # CRITICAL: Also notify GENESIS immediately so it can act
                    if failed_close:
                        _notify_genesis(
                            "orphan-positions",
                            f"🚨 ORPHANED POSITIONS REQUIRE MANUAL CLOSE IN ALPACA: {failed_close}\n"
                            f"Auto-close attempt FAILED. These must be manually closed immediately.",
                            ""
                        )
            return len(failed_close) == 0

        self._log_check("alpaca_db_agreement", True, "all positions reconciled")
        return True

    def run_all(self) -> int:
        failures = 0
        for check in [
            self.check_axiom_pool_alive,
            self.check_oracle_cache_fresh,
            self.check_alpaca_db_agreement,
        ]:
            try:
                if not check():
                    failures += 1
            except Exception as exc:
                log.error("Coherence check %s raised: %s", check.__name__, exc)
        return failures


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def _is_market_hours() -> bool:
    """Return True if ET time is between 9:25 and 16:05 on a weekday."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("America/New_York")
    except ImportError:
        return False
    now = datetime.now(tz)
    if now.weekday() >= 5:
        return False
    return now.replace(hour=9, minute=25, second=0) <= now <= now.replace(hour=16, minute=5, second=0)


def _is_pipeline_hours() -> bool:
    """Return True if ET time is between 9:15 and 16:15 on a weekday."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("America/New_York")
    except ImportError:
        return False
    now = datetime.now(tz)
    if now.weekday() >= 5:
        return False
    return now.replace(hour=9, minute=15, second=0) <= now <= now.replace(hour=16, minute=15, second=0)


def _get_et_now() -> Optional[datetime]:
    """Return current time in ET timezone, or None if zoneinfo unavailable."""
    try:
        import zoneinfo
        return datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    except ImportError:
        return None


def _is_on_the_hour() -> bool:
    now = datetime.now()
    return now.minute == 0 and now.second < MONITOR_INTERVAL_S


def _disk_usage_pct(path: str) -> float:
    usage = shutil.disk_usage(path)
    return (usage.used / usage.total) * 100.0


def _rotate_logs(logs_dir: str) -> int:
    freed = 0
    for log_file in Path(logs_dir).rglob("*.log"):
        if log_file.stat().st_mtime < time.time() - 7 * 86400:
            gz = log_file.with_suffix(".log.gz")
            with open(log_file, "rb") as fin, gzip.open(gz, "wb") as fout:
                shutil.copyfileobj(fin, fout)
            freed += log_file.stat().st_size
            log_file.unlink()
    for gz_file in Path(logs_dir).rglob("*.log.gz"):
        if gz_file.stat().st_mtime < time.time() - 30 * 86400:
            freed += gz_file.stat().st_size
            gz_file.unlink()
    return freed


def _hash_env_files() -> Dict[str, str]:
    return {
        path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if Path(path).exists() else "MISSING"
        for path in ENV_FILES
    }


def _backup_dbs(healing_conn: sqlite3.Connection) -> None:
    """Hourly backup of all service DBs using sqlite3.backup() (WAL-safe).

    Handles two path conventions:
      - Standard services: svc["db"] relative to DATA_DIR
      - External path services (SQS etc.): svc["ext_db"] absolute path

    Acquires _BACKUP_LOCK to serialize vs TelemetryWriter and prevent deadlock.
    Retains 24 hourly snapshots per service (rolling 24h window).
    Updated 2026-04-28: added chronicle, pipeline-sentinel, and all SQS databases.
    """
    with _BACKUP_LOCK:  # serialize vs TelemetryWriter to prevent SQLite deadlock
        Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d_%H")
        for svc in SERVICES:
            # Resolve source path: ext_db (absolute) takes precedence over db (relative)
            ext_db = svc.get("ext_db")
            rel_db = svc.get("db")
            if ext_db:
                src = Path(ext_db)
            elif rel_db:
                src = Path(DATA_DIR) / rel_db
            else:
                continue  # no DB configured for this service entry
            if not src.exists():
                continue
            dst = Path(BACKUP_DIR) / f"{svc['name']}_{tag}.db"
            try:
                src_conn = sqlite3.connect(str(src), timeout=10)
                dst_conn = sqlite3.connect(str(dst))
                with dst_conn:
                    src_conn.backup(dst_conn, pages=100)
                src_conn.close()
                dst_conn.close()
            except sqlite3.Error as exc:
                log.error("SQLite backup failed for %s: %s", svc["name"], exc)
            except OSError as exc:
                log.error("Backup I/O failed for %s: %s", svc["name"], exc)
        # Prune stale backups — keep 24 hourly snapshots per service
        for svc in SERVICES:
            backups = sorted(Path(BACKUP_DIR).glob(f"{svc['name']}_*.db"), reverse=True)
            for stale in backups[24:]:
                stale.unlink(missing_ok=True)


def _launchctl_list_pid(label: str) -> Optional[int]:
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.split("\n"):
            if '"PID"' in line:
                try:
                    return int(line.split("=")[-1].strip().rstrip(";"))
                except (ValueError, IndexError):
                    pass
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# EOD Report
# ---------------------------------------------------------------------------

def _send_eod_report(healing_conn: sqlite3.Connection,
                     telemetry_conn: sqlite3.Connection) -> None:
    today = datetime.now().strftime("%Y-%m-%d")

    anomalies_today = healing_conn.execute(
        "SELECT COUNT(*) FROM anomalies WHERE detected_at LIKE ?", (f"{today}%",)
    ).fetchone()[0]

    heals_today = healing_conn.execute(
        "SELECT COUNT(*) FROM healing_actions WHERE action_at LIKE ? AND success=1",
        (f"{today}%",)
    ).fetchone()[0]

    prevented = healing_conn.execute(
        "SELECT COUNT(*) FROM prevented_crashes WHERE ts LIKE ?", (f"{today}%",)
    ).fetchone()[0]

    outcomes = telemetry_conn.execute(
        "SELECT success FROM heal_outcomes WHERE ts >= ?",
        (time.time() - 86400,)
    ).fetchall()
    heal_rate = sum(r[0] for r in outcomes) / len(outcomes) if outcomes else 1.0

    coherence_fails = telemetry_conn.execute(
        "SELECT COUNT(*) FROM coherence_log WHERE passed=0 AND ts >= ?",
        (time.time() - 86400,)
    ).fetchone()[0]

    stat_anomalies = telemetry_conn.execute(
        "SELECT COUNT(*) FROM anomalies_v3 WHERE detected_at >= ?",
        (time.time() - 86400,)
    ).fetchone()[0]

    _send_telegram(
        f"*NEXUS EOD HEALTH REPORT — {today}*\n\n"
        f"Guardian Angel v4\n"
        f"Anomalies detected: {anomalies_today}\n"
        f"Autonomous heals: {heals_today} ({heal_rate:.0%} success rate)\n"
        f"Crashes PREVENTED: *{prevented}*\n"
        f"Statistical anomalies: {stat_anomalies}\n"
        f"Coherence failures: {coherence_fails}\n\n"
        f"System integrity: {'✅ Clean' if not coherence_fails else '⚠️ Issues found'}",
        tier=2,
    )


# ---------------------------------------------------------------------------
# V4 NEW: EscalationEngine — structured 3-tier ladder
# ---------------------------------------------------------------------------

class EscalationEngine:
    """
    Structured failure escalation ladder.

    Tier 1: Auto-heal attempt (immediate, silent log only)
    Tier 2: Alert Ahmed + notify GENESIS (after 20s unresolved)
    Tier 3: Full MultiAIBrain diagnosis (after 3min at Tier 2)
    Tier 4: Immediate critical alert — no ladder, requires human

    Usage:
      report_failure(component, detail)  — called each loop while failing
      report_healed(component)           — called on recovery
      tick()                             — called each loop to advance ladder
      alert_tier4(component, detail)     — immediate critical alert
    """

    TIER2_DELAY_S: float = 20.0
    TIER3_DELAY_S: float = 180.0  # 3 minutes at Tier 2

    def __init__(self) -> None:
        self._active: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._multi_ai: Optional["MultiAIBrain"] = None

    def set_ai_brain(self, brain: "MultiAIBrain") -> None:
        self._multi_ai = brain

    def report_failure(self, component: str, detail: str, log_tail: str = "") -> None:
        """Register a new failure or update an existing one."""
        with self._lock:
            if component not in self._active:
                self._active[component] = {
                    "tier": 1,
                    "first_seen": time.time(),
                    "last_escalated": time.time(),
                    "detail": detail,
                    "log_tail": log_tail,
                }
                log.info("EscalationEngine: Tier 1 registered for %s", component)
            else:
                self._active[component]["detail"] = detail
                if log_tail:
                    self._active[component]["log_tail"] = log_tail

    def report_healed(self, component: str) -> None:
        """Remove component from active ladder on recovery."""
        with self._lock:
            if component in self._active:
                tier = self._active.pop(component)["tier"]
                log.info("EscalationEngine: %s healed (was Tier %d)", component, tier)

    def alert_tier4(self, component: str, detail: str) -> None:
        """Fire Tier 4 immediately — critical, requires manual intervention."""
        dedup_key = f"critical-{component}"
        _send_telegram(
            f"*CRITICAL — {component}*\n\n{detail}\n\n"
            f"Requires immediate manual intervention.",
            tier=4,
            dedup_key=dedup_key,
        )
        # Route SQS alerts to PRIMUS, others to GENESIS (Ahmed Directive 2026-05-17)
        is_sqs_service = any(sqs in component.lower() for sqs in 
                            ["capital_router", "capital-router", "atm", "atg", 
                             "multiweek", "0dte", "swing", "intraday"])
        if is_sqs_service:
            _notify_primus_sqs(component, detail)
        else:
            _notify_genesis(component, detail)

    def tick(self) -> None:
        """Advance escalation ladder for any unresolved failures. Call each main loop."""
        now = time.time()
        to_escalate: List[Tuple[str, int, Dict]] = []

        with self._lock:
            for component, state in self._active.items():
                tier = state["tier"]
                elapsed = now - state["last_escalated"]
                if tier == 1 and elapsed >= self.TIER2_DELAY_S:
                    to_escalate.append((component, 2, dict(state)))
                    state["tier"] = 2
                    state["last_escalated"] = now
                elif tier == 2 and elapsed >= self.TIER3_DELAY_S:
                    to_escalate.append((component, 3, dict(state)))
                    state["tier"] = 3
                    state["last_escalated"] = now

        for component, new_tier, state in to_escalate:
            detail = state["detail"]
            log_tail = state.get("log_tail", "")
            elapsed_min = (now - state["first_seen"]) / 60.0
            
            # Determine if this is an SQS service (Ahmed Directive 2026-05-17)
            is_sqs_service = any(sqs in component.lower() for sqs in 
                                ["capital_router", "capital-router", "atm", "atg", 
                                 "multiweek", "0dte", "swing", "intraday"])

            if new_tier == 2:
                log.warning("EscalationEngine: → Tier 2 for %s (unresolved %.0fs)",
                            component, now - state["first_seen"])
                dedup_key = f"unresolved-{component}"
                _send_telegram(
                    f"*UNRESOLVED — {component}*\n\n"
                    f"{detail}\n\n"
                    f"Duration: {elapsed_min:.1f} min — auto-heal attempted, not recovered.",
                    tier=2,
                    dedup_key=dedup_key,
                )
                # Route SQS alerts to PRIMUS, others to GENESIS (Ahmed Directive)
                if is_sqs_service:
                    _notify_primus_sqs(component, detail, log_tail)
                else:
                    _notify_genesis(component, detail, log_tail)

            elif new_tier == 3:
                log.error("EscalationEngine: → Tier 3 for %s (unresolved %.0f min)",
                          component, elapsed_min)
                dedup_key = f"escalated-{component}"
                _send_telegram(
                    f"*ESCALATED — {component}*\n\n"
                    f"{detail}\n\n"
                    f"Duration: {elapsed_min:.1f} min — requesting AI diagnosis.",
                    tier=3,
                    dedup_key=dedup_key,
                )
                if self._multi_ai:
                    try:
                        result = self._multi_ai.diagnose_multi(
                            component, detail, log_tail, ""
                        )
                        if result:
                            ai_dedup_key = f"ai-diagnosis-{component}"
                            _send_telegram(
                                f"*AI DIAGNOSIS — {component}*\n\n"
                                f"*Claude:* {(result.get('claude') or 'N/A')[:300]}\n\n"
                                f"*OMNI:* {(result.get('omni') or 'N/A')[:200]}\n\n"
                                f"*Cipher:* {(result.get('cipher') or 'N/A')[:200]}\n\n"
                                f"*Best action:* {result.get('best_action', 'Manual review')}",
                                tier=3,
                                dedup_key=ai_dedup_key,
                            )
                    except Exception as exc:
                        log.error("EscalationEngine Tier 3 AI diagnosis failed: %s", exc)


# ---------------------------------------------------------------------------
# V4 NEW: MultiAIBrain — Claude + OMNI + Cipher adversarial review
# ---------------------------------------------------------------------------

class MultiAIBrain:
    """
    Three independent AI analyses for novel failures:
    1. Claude (primary) — direct API call
    2. OMNI /synthesize (secondary) — pipeline-aware synthesis
    3. Cipher-brain via Claude API (tertiary) — adversarial review

    All three run concurrently. Combined analysis returned.
    """

    def __init__(self) -> None:
        self._claude_available = bool(ANTHROPIC_API_KEY)
        self._omni_url = "http://localhost:8004"

    def _call_claude(self, service: str, failure_desc: str,
                     stderr_tail: str, adversarial: bool = False) -> Optional[str]:
        if not self._claude_available:
            return None

        if adversarial:
            prompt = (
                f"You are Cipher, an adversarial AI reviewer. A primary AI diagnosed this failure:\n\n"
                f"Service: {service}\nFailure: {failure_desc}\n\n"
                f"Find what the primary diagnosis MISSED. What failure mode wasn't considered? "
                f"What could go wrong with the proposed fix? Answer in 2-3 sentences."
            )
            max_tokens = 300
        else:
            prompt = (
                f"Diagnose this Nexus trading system failure in 2-3 sentences.\n\n"
                f"Service: {service}\nFailure: {failure_desc}\n"
                f"Stderr (last 500 chars): {stderr_tail[-500:]}\n\n"
                f"Upstream deps: {json.dumps(DEPENDENCY_GRAPH.get(service, []))}\n\n"
                f"State: root_cause, recommended_action"
            )
            max_tokens = 400

        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=25,
            )
            if resp.ok:
                content = resp.json().get("content", [])
                return content[0].get("text", "") if content else None
        except (requests.RequestException, KeyError):
            pass
        return None

    def _call_omni_synthesize(self, service: str, failure_desc: str) -> Optional[str]:
        try:
            resp = requests.post(
                f"{self._omni_url}/synthesize",
                headers={"X-Nexus-Secret": NEXUS_SECRET, "Content-Type": "application/json"},
                json={
                    "source": "guardian_angel",
                    "context": f"FAILURE DIAGNOSIS REQUEST\nService: {service}\n{failure_desc}",
                    "ticker": "SYSTEM",
                    "force": True,
                },
                timeout=20,
            )
            if resp.ok:
                data = resp.json()
                return data.get("synthesis", data.get("result", str(data)[:300]))
        except (requests.RequestException, ValueError):
            pass
        return None

    def diagnose_multi(self, service: str, failure_desc: str,
                       stderr_tail: str, health_history: str) -> Optional[Dict[str, Any]]:
        """Run all three brains concurrently. Return combined analysis or None."""
        results: Dict[str, Optional[str]] = {"claude": None, "omni": None, "cipher": None}

        def _run_claude() -> None:
            results["claude"] = self._call_claude(service, failure_desc, stderr_tail)

        def _run_omni() -> None:
            results["omni"] = self._call_omni_synthesize(service, failure_desc)

        def _run_cipher() -> None:
            primary = results.get("claude") or failure_desc
            results["cipher"] = self._call_claude(
                service,
                f"Primary analysis: {primary}\nOriginal failure: {failure_desc}",
                stderr_tail,
                adversarial=True,
            )

        t_claude = threading.Thread(target=_run_claude, daemon=True)
        t_omni = threading.Thread(target=_run_omni, daemon=True)
        t_claude.start()
        t_omni.start()
        t_claude.join(timeout=30)
        t_omni.join(timeout=25)

        # Cipher runs after Claude so it can reference Claude's analysis
        t_cipher = threading.Thread(target=_run_cipher, daemon=True)
        t_cipher.start()
        t_cipher.join(timeout=25)

        if all(v is None for v in results.values()):
            return None

        best_action = "Manual review required"
        claude_text = (results.get("claude") or "").lower()
        for keyword, action in [
            ("restart", f"Restart {service} via launchctl"),
            ("checkpoint", f"Run WAL checkpoint on {service}.db"),
            ("memory", f"Restart {service} (memory pressure)"),
            ("dependency", "Check upstream services"),
        ]:
            if keyword in claude_text:
                best_action = action
                break

        return {
            "claude": results["claude"],
            "omni": results["omni"],
            "cipher": results["cipher"],
            "best_action": best_action,
        }


# ---------------------------------------------------------------------------
# V4 NEW: PipelineTelemetry — 8 pipeline-level checks (4a-4h)
# ---------------------------------------------------------------------------

class PipelineTelemetry:
    """
    Pipeline-level health checks. Runs during market hours (09:15-16:15 ET).
    Detects silent failures in the agent→buffer→execution pipeline.
    """

    AGENT_SERVICES = [
        {"name": "cipher", "port": 9001, "db": "cipher.db"},
        {"name": "atlas",  "port": 9002, "db": "atlas.db"},
        {"name": "sage",   "port": 9003, "db": "sage.db"},
    ]
    EXEC_SERVICES = [
        {"name": "alpha-execution", "port": 8005},
        {"name": "prime-execution", "port": 8006},
    ]

    def __init__(self, escalation: EscalationEngine) -> None:
        self._escalation = escalation
        self._vix_states: Dict[str, str] = {}
        self._zero_trades_last_alert: Dict[str, float] = {}  # cooldown: 60min per service

    def _query_agent_db(self, db_name: str, sql: str,
                        params: tuple = ()) -> Optional[Any]:
        """Query a service SQLite DB. Returns first column of first row, or None."""
        db_path = Path(DATA_DIR) / db_name
        if not db_path.exists():
            return None
        try:
            conn = sqlite3.connect(str(db_path), timeout=3)
            row = conn.execute(sql, params).fetchone()
            conn.close()
            return row[0] if row else None
        except sqlite3.Error:
            return None

    def check_agent_pick_rates(self) -> None:
        """4a: Agents should produce picks >30 min after market open."""
        now_et = _get_et_now()
        if now_et is None:
            return
        # Market opens 9:30, check after 9:45 (>15 min buffer)
        if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 45):
            return

        cutoff = time.time() - 1800
        for agent in self.AGENT_SERVICES:
            # Try created_at first, fall back to timestamp column
            count = self._query_agent_db(
                agent["db"],
                "SELECT COUNT(*) FROM picks WHERE created_at >= ?",
                (cutoff,),
            )
            if count is None:
                count = self._query_agent_db(
                    agent["db"],
                    "SELECT COUNT(*) FROM picks WHERE timestamp >= ?",
                    (cutoff,),
                )
            if count == 0:
                log.error("AGENT_SILENT: %s — 0 picks in last 30 min", agent["name"])
                self._escalation.report_failure(
                    agent["name"],
                    "AGENT_SILENT: 0 picks in last 30 min — concordance chain dead",
                    _read_stderr_tail(agent["name"], 50),
                )
                healed = _launchctl_restart(agent["name"])
                _send_telegram(
                    f"*AGENT SILENT — {agent['name']}*\n"
                    f"0 picks in 30 min. Restart: {'✅' if healed else '❌'}",
                    tier=3,
                )
                if healed:
                    self._escalation.report_healed(agent["name"])

    def check_execution_state(self) -> None:
        """4b: execution_paused, alpaca_reachable, vix_brake checks."""
        for svc in self.EXEC_SERVICES:
            try:
                resp = requests.get(
                    f"http://localhost:{svc['port']}/health",
                    headers={"X-Nexus-Secret": NEXUS_SECRET},
                    timeout=5,
                )
                if not resp.ok:
                    continue
                data = resp.json()

                if data.get("execution_paused") is True:
                    # Dedup: only fire /resume if not already in active escalation
                    if svc["name"] not in self._escalation._active:
                        log.error("EXECUTION_PAUSED on %s — sending /resume", svc["name"])
                        try:
                            # GAP-B fix: prime-execution requires X-Nexus-Prime-Secret
                            _resume_header = (
                                {"X-Nexus-Prime-Secret": NEXUS_PRIME_SECRET}
                                if svc["name"] == "prime-execution"
                                else {"X-Nexus-Secret": NEXUS_SECRET}
                            )
                            requests.post(
                                f"http://localhost:{svc['port']}/resume",
                                headers=_resume_header,
                                timeout=5,
                            )
                        except requests.RequestException:
                            pass
                        self._escalation.report_failure(
                            svc["name"], "execution_paused=True — auto-resume sent"
                        )
                        _send_telegram(
                            f"*EXECUTION PAUSED — {svc['name']}*\n"
                            f"POST /resume sent. Verify trades resuming.",
                            tier=3,
                        )

                if data.get("alpaca_reachable") is False:
                    self._escalation.report_failure(
                        svc["name"], "alpaca_reachable=False — cannot auto-fix"
                    )
                    _send_telegram(
                        f"*ALPACA UNREACHABLE from {svc['name']}*\n"
                        f"Cannot auto-fix. Check Alpaca API status.",
                        tier=4,
                    )

            except requests.RequestException:
                pass

    def check_omni_brain_health(self) -> None:
        """4c: OMNI should have >=3 brains available; >0 activity past noon."""
        try:
            resp = requests.get(
                "http://localhost:8004/health",
                headers={"X-Nexus-Secret": NEXUS_SECRET},
                timeout=5,
            )
            if not resp.ok:
                return
            data = resp.json()

            brains = data.get("brains_available", data.get("active_brains", 3))
            if isinstance(brains, int) and brains < 3:
                self._escalation.report_failure(
                    "omni", f"Only {brains}/3 AI brains available"
                )
                _send_telegram(
                    f"*OMNI DEGRADED* — {brains}/3 brains available\nRestarting OMNI.",
                    tier=3,
                )
                _launchctl_restart("omni")

            now_et = _get_et_now()
            if now_et and now_et.hour >= 12:
                syntheses = data.get("syntheses_today", -1)
                go_verdicts = data.get("go_verdicts_today", -1)
                if syntheses == 0 and go_verdicts == 0:
                    self._escalation.report_failure(
                        "omni",
                        "0 syntheses + 0 GO verdicts today past noon — pipeline may be silent",
                    )
                    _send_telegram(
                        "*OMNI ZERO ACTIVITY*\n"
                        "0 syntheses + 0 GO verdicts past noon.",
                        tier=3,
                    )

        except requests.RequestException:
            pass

    def check_concordance_formation(self) -> None:
        """4d: Alpha Buffer should form concordances when agents have picks."""
        cutoff = time.time() - 1800
        count = self._query_agent_db(
            "alpha_buffer.db",
            "SELECT COUNT(*) FROM concordance_results WHERE created_at >= ?",
            (cutoff,),
        )
        if count == 0:
            agent_active = any(
                (self._query_agent_db(
                    a["db"],
                    "SELECT COUNT(*) FROM picks WHERE created_at >= ?",
                    (cutoff,),
                ) or 0) > 0
                for a in self.AGENT_SERVICES
            )
            if agent_active:
                self._escalation.report_failure(
                    "alpha-buffer",
                    "0 concordances in 30 min despite agents having picks — buffer logic broken",
                )
                _send_telegram(
                    "*CONCORDANCE FAILURE*\n"
                    "Agents have picks but Alpha Buffer formed 0 concordances in 30 min.",
                    tier=3,
                )

    def check_trade_execution_rate(self) -> None:
        """4e: trades_today should be >0 by noon; harder alert by 2PM."""
        now_et = _get_et_now()
        if now_et is None or now_et.hour < 12:
            return

        now_ts = time.time()
        for svc in self.EXEC_SERVICES:
            try:
                auth = (
                    {"X-Nexus-Prime-Secret": NEXUS_PRIME_SECRET}
                    if svc["name"] == "prime-execution"
                    else {"X-Nexus-Secret": NEXUS_SECRET}
                )
                resp = requests.get(
                    f"http://localhost:{svc['port']}/health",
                    headers=auth,
                    timeout=5,
                )
                if not resp.ok:
                    continue
                data = resp.json()
                trades = data.get("trades_today", -1)
                if isinstance(trades, int) and trades == 0:
                    last = self._zero_trades_last_alert.get(svc["name"], 0.0)
                    if now_ts - last < 3600:  # 60-min cooldown per service
                        continue
                    self._zero_trades_last_alert[svc["name"]] = now_ts
                    tier = 3 if now_et.hour >= 14 else 2
                    # Use condition-based dedup_key (no timestamps) for proper alert deduplication
                    dedup_key = f"zero-trades-{svc['name']}"
                    _send_telegram(
                        f"*ZERO TRADES — {svc['name']}*\n"
                        f"trades_today=0 at {now_et.strftime('%H:%M')} ET.",
                        tier=tier,
                        dedup_key=dedup_key,
                    )
            except requests.RequestException:
                pass

    def check_position_limits(self) -> None:
        """4f: Alert if open_positions >= max_positions (informational)."""
        for svc in self.EXEC_SERVICES:
            try:
                resp = requests.get(
                    f"http://localhost:{svc['port']}/health",
                    headers={"X-Nexus-Secret": NEXUS_SECRET},
                    timeout=5,
                )
                if not resp.ok:
                    continue
                data = resp.json()
                open_pos = data.get("open_positions", 0)
                max_pos = data.get("max_positions", 999)
                if isinstance(open_pos, int) and isinstance(max_pos, int) and open_pos >= max_pos:
                    dedup_key = f"position-limit-{svc['name']}"
                    _send_telegram(
                        f"*POSITION LIMIT — {svc['name']}*\n"
                        f"open_positions={open_pos} >= max={max_pos}. No new entries possible.",
                        tier=2,
                        dedup_key=dedup_key,
                    )
            except requests.RequestException:
                pass

    def check_alpha_buffer_submission_rate(self) -> None:
        """4g: Alpha Buffer should receive submissions every 30 min."""
        cutoff = time.time() - 1800
        count = self._query_agent_db(
            "alpha_buffer.db",
            "SELECT COUNT(*) FROM submissions WHERE received_at >= ?",
            (cutoff,),
        )
        if count == 0:
            self._escalation.report_failure(
                "alpha-buffer",
                "0 submissions to Alpha Buffer in 30 min — agents may not be submitting",
            )
            _send_telegram(
                "*ALPHA BUFFER SILENT*\n0 submissions received in last 30 min.",
                tier=3,
                dedup_key="alpha-buffer-silent",
            )

    def check_vix_brake_states(self) -> None:
        """4h: Track VIX brake state transitions, alert Ahmed on changes."""
        for svc in self.EXEC_SERVICES:
            try:
                resp = requests.get(
                    f"http://localhost:{svc['port']}/health",
                    headers={"X-Nexus-Secret": NEXUS_SECRET},
                    timeout=5,
                )
                if not resp.ok:
                    continue
                data = resp.json()
                current = data.get("vix_brake", "CLEAR")
                prev = self._vix_states.get(svc["name"], "CLEAR")

                if current != prev:
                    log.info("VIX brake transition on %s: %s → %s", svc["name"], prev, current)
                    self._vix_states[svc["name"]] = current

                    if current == "HALTED":
                        _send_telegram(
                            f"*VIX BRAKE HALTED — {svc['name']}*\n"
                            f"Trading halted by VIX volatility brake. Correct behavior — FYI.",
                            tier=3,
                        )
                    elif current == "ELEVATED":
                        _send_telegram(
                            f"*VIX BRAKE ELEVATED — {svc['name']}*\n"
                            f"Position sizing reduced.",
                            tier=2,
                        )
                    elif current == "CLEAR" and prev in ("HALTED", "ELEVATED"):
                        _send_telegram(
                            f"*VIX BRAKE CLEARED — {svc['name']}*\n"
                            f"Normal trading conditions resumed.",
                            tier=2,
                        )

            except requests.RequestException:
                pass

    def run_all(self) -> None:
        """Run all 8 pipeline checks."""
        for check in [
            self.check_agent_pick_rates,
            self.check_execution_state,
            self.check_omni_brain_health,
            self.check_concordance_formation,
            self.check_trade_execution_rate,
            self.check_position_limits,
            self.check_alpha_buffer_submission_rate,
            self.check_vix_brake_states,
        ]:
            try:
                check()
            except Exception as exc:
                log.error("PipelineTelemetry.%s raised: %s", check.__name__, exc)


# ---------------------------------------------------------------------------
# V4 NEW: ExternalAPIProbe — 5 API health probes every 15 min
# ---------------------------------------------------------------------------

class ExternalAPIProbe:
    """Probes external APIs every 15 min during market hours."""

    def __init__(self, escalation: EscalationEngine) -> None:
        self._escalation = escalation

    def probe_orats(self) -> bool:
        """5a: ORATS — IV rank data; failure means 0 picks."""
        if not ORATS_API_KEY:
            return True
        try:
            resp = requests.get(
                f"https://api.orats.io/datav2/tickers?token={ORATS_API_KEY}&tickers=SPY",
                timeout=15,
            )
            if resp.ok:
                self._escalation.report_healed("orats_api")
                return True
            self._escalation.report_failure(
                "orats_api", f"ORATS API HTTP {resp.status_code} — IV rank null, 0 picks expected"
            )
            _send_telegram(
                f"*ORATS API DOWN* — HTTP {resp.status_code}\n"
                f"IV rank data unavailable. Agent picks will be zero.",
                tier=3,
            )
            return False
        except requests.RequestException as exc:
            self._escalation.report_failure("orats_api", f"ORATS unreachable: {exc}")
            _send_telegram(f"*ORATS API UNREACHABLE*\n{str(exc)[:150]}", tier=3)
            return False

    def probe_anthropic(self) -> bool:
        """5b: Anthropic API — Cipher brain health."""
        if not ANTHROPIC_API_KEY:
            return True
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 5,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=15,
            )
            if resp.ok:
                self._escalation.report_healed("anthropic_api")
                return True
            self._escalation.report_failure(
                "anthropic_api", f"Anthropic API HTTP {resp.status_code} — Cipher brain dead"
            )
            _send_telegram(
                f"*ANTHROPIC API DOWN* — HTTP {resp.status_code}\n"
                f"Cipher + OMNI Claude brain unavailable.",
                tier=3,
            )
            return False
        except requests.RequestException as exc:
            self._escalation.report_failure("anthropic_api", f"Anthropic unreachable: {exc}")
            _send_telegram(f"*ANTHROPIC API UNREACHABLE*\n{str(exc)[:150]}", tier=3)
            return False

    def probe_gemini(self) -> bool:
        """5c: Gemini API — Atlas brain + OMNI brain."""
        if not GEMINI_API_KEY:
            return True
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
                json={"contents": [{"parts": [{"text": "ping"}]}]},
                timeout=15,
            )
            if resp.ok:
                self._escalation.report_healed("gemini_api")
                return True
            self._escalation.report_failure(
                "gemini_api", f"Gemini API HTTP {resp.status_code} — Atlas + OMNI degraded"
            )
            _send_telegram(
                f"*GEMINI API DOWN* — HTTP {resp.status_code}\n"
                f"Atlas + OMNI Gemini brain unavailable.",
                tier=3,
            )
            return False
        except requests.RequestException as exc:
            self._escalation.report_failure("gemini_api", f"Gemini unreachable: {exc}")
            _send_telegram(f"*GEMINI API UNREACHABLE*\n{str(exc)[:150]}", tier=3)
            return False

    def probe_deepseek(self) -> bool:
        """5d: DeepSeek API — Sage brain + OMNI brain."""
        if not DEEPSEEK_API_KEY:
            return True
        try:
            resp = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "max_tokens": 5,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=15,
            )
            if resp.ok:
                self._escalation.report_healed("deepseek_api")
                return True
            self._escalation.report_failure(
                "deepseek_api", f"DeepSeek API HTTP {resp.status_code} — Sage + OMNI degraded"
            )
            _send_telegram(
                f"*DEEPSEEK API DOWN* — HTTP {resp.status_code}\n"
                f"Sage + OMNI DeepSeek brain unavailable.",
                tier=3,
            )
            return False
        except requests.RequestException as exc:
            self._escalation.report_failure("deepseek_api", f"DeepSeek unreachable: {exc}")
            _send_telegram(f"*DEEPSEEK API UNREACHABLE*\n{str(exc)[:150]}", tier=3)
            return False

    def probe_alpaca(self) -> bool:
        """5e: Alpaca paper API — account status, buying_power, trading_blocked."""
        acct = _alpaca_get("/v2/account")
        if acct is None:
            self._escalation.report_failure(
                "alpaca_api", "Alpaca /v2/account unreachable — execution cannot trade"
            )
            _send_telegram("*ALPACA API UNREACHABLE*\n/v2/account returned None.", tier=4)
            return False

        self._escalation.report_healed("alpaca_api")

        if acct.get("trading_blocked") or acct.get("account_blocked"):
            self._escalation.alert_tier4(
                "alpaca_api",
                f"trading_blocked={acct.get('trading_blocked')} — manual intervention required",
            )
            return False

        buying_power = float(acct.get("buying_power", 9999))
        if buying_power < 1000:
            _send_telegram(
                f"*ALPACA BUYING POWER LOW*\n"
                f"buying_power=${buying_power:,.0f} — may not open new positions.",
                tier=3,
            )

        return True

    def run_all(self) -> None:
        """Run all 5 external API probes."""
        for probe in [
            self.probe_orats,
            self.probe_anthropic,
            self.probe_gemini,
            self.probe_deepseek,
            self.probe_alpaca,
        ]:
            try:
                probe()
            except Exception as exc:
                log.error("ExternalAPIProbe.%s raised: %s", probe.__name__, exc)


# ---------------------------------------------------------------------------
# Main Guardian Angel V4 Daemon
# ---------------------------------------------------------------------------

class GuardianAngelV4:
    """
    V4: Full coverage — infrastructure + trading pipeline + structured escalation
    + OMNI/Cipher AI redundancy + GENESIS notification + self-heartbeat.
    """

    def __init__(self) -> None:
        Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
        Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)

        self._healing_conn = _init_healing_db(HEALING_DB_PATH)
        self._telemetry_conn = _init_telemetry_db(TELEMETRY_DB_PATH)
        self._heal_lock = threading.Lock()

        # V3 subsystems — all preserved
        self._collector = TelemetryCollector(self._telemetry_conn)
        self._baseline = BaselineEngine(self._telemetry_conn, self._collector)
        self._causal = CausalGraph(self._telemetry_conn, self._collector)
        self._forecast = ForecastEngine(self._collector, self._telemetry_conn)
        self._ai_brain = AIBrain()
        self._catalog = HealCatalog(self._telemetry_conn)
        self._coherence = CoherenceVerifier(self._healing_conn, self._telemetry_conn)

        # Wire the connection-replaced callback so all dependents get the new
        # sqlite3.Connection when TelemetryCollector.prune_old() recreates telemetry.db.
        self._collector._on_conn_replaced = self._refresh_telemetry_conn

        # V4 subsystems
        self._escalation = EscalationEngine()
        self._multi_ai = MultiAIBrain()
        self._escalation.set_ai_brain(self._multi_ai)
        self._pipeline = PipelineTelemetry(self._escalation)
        self._api_probe = ExternalAPIProbe(self._escalation)

        self._env_hashes = _hash_env_files()
        self._crash_history: Dict[str, List[float]] = {}
        self._failure_times: Dict[str, float] = {}
        self._last_heal_ts: Dict[str, float] = {}
        self._heal_cooldown = 300  # 5 min between heal attempts per service
        # Block 5: Per-service daily heal attempt counter — resets at midnight ET
        self._heal_attempts_today: Dict[str, int] = {}
        self._heal_attempts_reset_date: str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        self.MAX_HEAL_ATTEMPTS: int = 3  # hard cap per service per calendar day
        self._last_db_check_ts: float = 0.0

        _now = time.time()
        self._last_reconcile_ts: float = _now
        self._last_alpaca_ts: float = _now
        self._last_backup_ts: float = _now
        self._last_baseline_ts: float = _now
        self._last_coherence_ts: float = _now
        self._last_eod_ts: float = 0.0
        self._last_api_probe_ts: float = _now
        self._last_e2e_ts: float = 0.0   # Block 9: 7AM OMNI E2E diagnostic
        self._running = True

        # Block 9: ensure CHRONICLE cron_log table exists at startup
        _ensure_cron_log_table()
        log.info("Guardian Angel v4 — Full Coverage initialized")

    def _health_check_service(self, svc: Dict[str, Any]) -> bool:
        """
        Poll health endpoint. On failure:
        1. Register with EscalationEngine (Tier 1 ladder starts)
        2. Find root cause via causal graph
        3. Select best patch from self-learning catalog
        4. Execute. If novel failure, call MultiAIBrain.
        Returns True if healthy.
        """
        name = svc["name"]
        # Skip services with no port or health_path (e.g. chronicle — remote/undeployed)
        if not svc.get("port") or not svc.get("health_path"):
            return True
        url = f"http://localhost:{svc['port']}{svc['health_path']}"
        failure_type = "UNKNOWN"
        t0 = time.time()
        # Per-service timeout override — ATG runs multi-minute QI scans
        http_timeout = svc.get("health_timeout", 5)

        try:
            resp = requests.get(url, timeout=http_timeout, headers={"X-Nexus-Secret": NEXUS_SECRET})
            elapsed = (time.time() - t0) * 1000

            if resp.ok:
                sigma = self._baseline.check_anomaly(name, "response_time_ms", elapsed)
                if sigma is not None:
                    log.info("STAT ANOMALY: %s response_time_ms %.0fms (%.1fσ)", name, elapsed, sigma)
                    self._baseline.flag_anomaly(name, "response_time_ms", elapsed, sigma,
                                                self._telemetry_conn)
                if name in self._failure_times:
                    del self._failure_times[name]
                self._escalation.report_healed(name)
                return True

            # Service responded but status was not healthy
            try:
                body_status = resp.json().get("status", "unknown")
                execution_paused = resp.json().get("execution_paused", False)
                if execution_paused:
                    failure_type = "EXECUTION_PAUSED"
                else:
                    failure_type = f"STATUS_{body_status.upper()}"
            except Exception:
                failure_type = f"HTTP_{resp.status_code}"

        except requests.ConnectionError:
            failure_type = "CONNECTION_REFUSED"
        except requests.Timeout:
            failure_type = "TIMEOUT"
        except requests.RequestException:
            failure_type = "UNKNOWN"

        self._failure_times[name] = time.time()
        self._crash_history.setdefault(name, [])
        self._crash_history[name].append(time.time())
        cutoff = time.time() - 600
        self._crash_history[name] = [t for t in self._crash_history[name] if t >= cutoff]

        stderr = _read_stderr_tail(name, 50)
        self._escalation.report_failure(name, f"{failure_type} — {url}", stderr)

        if len(self._crash_history[name]) < 2:
            return False

        if len(self._crash_history[name]) >= 3:
            now_iso = datetime.now(timezone.utc).isoformat()
            self._healing_conn.execute(
                "INSERT INTO anomalies (detected_at, service, anomaly_type, severity, details) "
                "VALUES (?,?,?,?,?)",
                (now_iso, name, "REPEATED_CRASHES", "CRITICAL",
                 "3+ crashes in 10 min — stopping auto-restart"),
            )
            self._healing_conn.execute(
                "INSERT INTO flags (key, value, set_at, set_by) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "set_at=excluded.set_at, set_by=excluded.set_by",
                ("healing_active", "true", now_iso, "guardian_v4"),
            )
            self._healing_conn.commit()
            self._escalation.alert_tier4(
                name,
                "3+ crashes in 10 min. Auto-restart SUSPENDED. Manual intervention required.",
            )
            return False

        # ── Block 5: Daily heal cap ─────────────────────────────────────────────
        # Reset counter at day boundary (ET calendar day)
        _today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if _today_str != self._heal_attempts_reset_date:
            self._heal_attempts_today = {}
            self._heal_attempts_reset_date = _today_str

        if self._heal_attempts_today.get(name, 0) >= self.MAX_HEAL_ATTEMPTS:
            log.warning(
                "HEAL SUPPRESSED: %s has reached MAX_HEAL_ATTEMPTS=%d today — escalating Tier 4",
                name, self.MAX_HEAL_ATTEMPTS,
            )
            self._escalation.alert_tier4(
                name,
                f"Max heal attempts ({self.MAX_HEAL_ATTEMPTS}) reached today. "
                "Auto-healing stopped. Manual intervention required.",
            )
            return False

        last_heal = self._last_heal_ts.get(name, 0.0)
        if time.time() - last_heal < self._heal_cooldown:
            remaining = int(self._heal_cooldown - (time.time() - last_heal))
            log.info("HEAL SKIPPED: %s — cooldown active (%ds remaining)", name, remaining)
            return False
        # Block 5: _last_heal_ts set ONLY after verified-healthy post-heal (moved below)

        # ── Block 6: SENTINEL Routing Table ─────────────────────────────────────
        # Read full health response to choose the correct heal action.
        # Do NOT blindly restart — read what the service is telling us first.
        _route_action = "RESTART"  # default
        _route_reason = failure_type
        _port = svc.get("port")
        _is_execution_svc = _port in (8005, 8006)

        # Market hours gate: execution services before 9:25 AM or after 16:05 PM ET
        _now_et = datetime.now(__import__("zoneinfo").ZoneInfo("America/New_York"))
        _market_start = _now_et.replace(hour=9, minute=25, second=0, microsecond=0)
        _market_end   = _now_et.replace(hour=16, minute=5,  second=0, microsecond=0)
        if _is_execution_svc and not (_market_start <= _now_et <= _market_end):
            log.info(
                "HEAL SUPPRESSED: %s — outside market hours (%s ET), expected state",
                name, _now_et.strftime("%H:%M"),
            )
            return False

        # Read latest health payload for routing decision
        _health_payload: dict = {}
        try:
            _hr = requests.get(url, timeout=5, headers={"X-Nexus-Secret": NEXUS_SECRET})
            if _hr.ok or _hr.status_code in (200, 503):
                _health_payload = _hr.json()
        except Exception:
            pass  # Connection refused — routing falls through to default RESTART

        _svc_status = _health_payload.get("status", "unknown")

        if _svc_status in ("standby", "warming"):
            # Block 2: service is self-recovering — do NOT interfere.
            # 'warming' = Oracle post-restart cache fill (normal, takes 15-30s)
            # 'standby' = any other service preflight retry in progress
            log.info("HEAL SKIPPED: %s — status=%s, service is self-recovering", name, _svc_status)
            return False

        if _health_payload.get("stale_deploy") is True:
            # Deploy gate owns stale deploys — GA alerts but never heals
            log.warning("STALE DEPLOY detected on %s — alerting only (deploy gate owns this)", name)
            _send_telegram(
                f"*STALE DEPLOY* — `{name}`\n"
                "code_hash mismatch detected.\n"
                "Deploy gate should have caught this. Check `post_deploy_check.py`.",
                tier=2,
            )
            return False

        if _health_payload.get("alpaca_reachable") is False:
            # Alpaca unreachable — wait 60s, retry probe, alert if still down
            log.warning("HEAL: %s — alpaca_reachable=False, waiting 60s before retry", name)
            time.sleep(60)
            try:
                _retry_r = requests.get(url, timeout=5, headers={"X-Nexus-Secret": NEXUS_SECRET})
                if _retry_r.ok and _retry_r.json().get("alpaca_reachable") is not False:
                    log.info("HEAL: %s — Alpaca recovered after 60s wait", name)
                    return True
            except Exception:
                pass
            _send_telegram(
                f"*ALPACA UNREACHABLE* — `{name}`\n"
                "Still down after 60s retry. Check Alpaca status.",
                tier=3,
            )
            return False

        if _health_payload.get("execution_paused") is True:
            _route_action = "RESUME"
            _route_reason = "execution_paused=True"
        elif (_health_payload.get("execution_valid") is False
              and _svc_status not in ("standby",)
              and _health_payload.get("alpaca_reachable") is not False):
            _route_action = "RESTART"
            _route_reason = "execution_valid=False (all deps normal)"
        elif failure_type == "CONNECTION_REFUSED":
            _route_action = "RESTART"
        # else: keep default RESTART

        log.info(
            "Block 6 routing: %s → action=%s reason=%s",
            name, _route_action, _route_reason,
        )

        root = self._causal.find_root_cause(name)
        causal_chain = self._causal.explain_chain(name)
        best_patch = self._catalog.best_patch(failure_type)
        success_rate = self._catalog.success_rate(failure_type, best_patch)
        rate_str = f"{success_rate:.0%}" if success_rate is not None else "no history"

        log.info("HEAL: %s | type=%s | root=%s | patch=%s (%s success)",
                 name, failure_type, root, best_patch, rate_str)

        # Block 5: Increment attempt counter BEFORE heal (regardless of outcome)
        self._heal_attempts_today[name] = self._heal_attempts_today.get(name, 0) + 1

        t_heal = time.time()
        healed = False

        # Block 6: Execute routed heal action
        if _route_action == "RESUME":
            # Execution service paused — send /resume, not a restart
            _resume_secret = NEXUS_PRIME_SECRET if svc.get("port") == 8006 else NEXUS_SECRET
            _resume_url = f"http://localhost:{svc['port']}/resume"
            try:
                _resume_resp = requests.post(
                    _resume_url,
                    headers={"X-Nexus-Secret": _resume_secret, "X-Nexus-Prime-Secret": _resume_secret},
                    timeout=5,
                )
                healed = _resume_resp.ok
                log.info("RESUME sent to %s: status=%d", name, _resume_resp.status_code)
            except Exception as _re:
                log.warning("RESUME failed for %s: %s", name, _re)
                healed = False
        elif best_patch == "RESTART" or _route_action == "RESTART":
            healed = _launchctl_restart(root if root != name else name)
            if healed:
                time.sleep(5)
                try:
                    verify = requests.get(url, timeout=5, headers={"X-Nexus-Secret": NEXUS_SECRET})
                    healed = verify.ok
                except requests.RequestException:
                    healed = False
        elif best_patch == "WAIT_RETRY":
            time.sleep(10)
            try:
                verify = requests.get(url, timeout=5, headers={"X-Nexus-Secret": NEXUS_SECRET})
                healed = verify.ok
            except requests.RequestException:
                healed = False

        heal_time = time.time() - t_heal
        self._catalog.record_outcome(failure_type, best_patch, healed,
                                     f"root={root}, causal={causal_chain}")

        if healed:
            # Block 5: _last_heal_ts set ONLY after verified-healthy post-heal
            self._last_heal_ts[name] = time.time()
            _send_telegram(
                f"*FIXED* — `{name}` recovered\n"
                f"Type: {failure_type} | Root: `{root}`\n"
                f"Patch: {best_patch} | Time: {heal_time:.1f}s\n"
                f"Catalog success rate: {rate_str}",
                tier=2,
            )
            self._crash_history[name] = []
            if name in self._failure_times:
                del self._failure_times[name]
            self._escalation.report_healed(name)
        else:
            # Novel failure — call MultiAIBrain (3 independent analyses)
            history_vals = self._collector.get_recent(name, "http_ok", 600)
            history_str = f"Last {len(history_vals)} checks, {sum(history_vals):.0f} successful"
            result = self._multi_ai.diagnose_multi(
                name,
                f"{failure_type} failure — causal chain: {causal_chain}",
                stderr, history_str,
            )
            if result:
                _send_telegram(
                    f"*MULTI-AI DIAGNOSIS — `{name}`*\n\n"
                    f"*Claude:* {(result.get('claude') or 'N/A')[:300]}\n\n"
                    f"*OMNI:* {(result.get('omni') or 'N/A')[:200]}\n\n"
                    f"*Cipher:* {(result.get('cipher') or 'N/A')[:200]}\n\n"
                    f"*Best action:* {result.get('best_action', 'Manual review')}",
                    tier=3,
                )
            else:
                _send_telegram(
                    f"*UNRESOLVED — `{name}` still down*\n"
                    f"Type: {failure_type}\n"
                    f"Causal chain: {causal_chain}\n"
                    f"Check logs: `tail -50 {LOGS_DIR}/{name}/stderr.log`",
                    tier=3,
                )

        return healed

    def _check_memory_cpu(self) -> None:
        for svc in SERVICES:
            pid = _get_service_pid(svc["name"])
            if not pid:
                continue
            try:
                proc = psutil.Process(pid)
                mem_pct = proc.memory_percent()
                cpu_pct = proc.cpu_percent(interval=None)

                for signal_name, value in [("memory_pct", mem_pct), ("cpu_pct", cpu_pct)]:
                    sigma = self._baseline.check_anomaly(svc["name"], signal_name, value)
                    if sigma is not None:
                        self._baseline.flag_anomaly(svc["name"], signal_name, value, sigma,
                                                    self._telemetry_conn)

                if mem_pct > 85:
                    ok = _launchctl_restart(svc["name"])
                    self._catalog.record_outcome("MEMORY_LEAK", "RESTART", ok, f"mem={mem_pct:.1f}%")
                    _send_telegram(
                        f"`{svc['name']}` restarted — memory at {mem_pct:.1f}% "
                        f"({'✅' if ok else '❌'})",
                        tier=3,
                    )

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def _check_forecasts(self) -> None:
        impending = self._forecast.check_all_forecasts(self._healing_conn)
        for item in impending:
            _send_telegram(
                f"*PREDICTED FAILURE* — `{item['service']}`\n\n"
                f"Signal: {item['signal']} approaching {item['threshold']}\n"
                f"ETA: *{item['minutes']} minutes*\n"
                f"Preemptive restart initiated.",
                tier=3,
            )
            _launchctl_restart(item["service"])

    def _check_config_hashes(self) -> None:
        current = _hash_env_files()
        for path, new_hash in current.items():
            old = self._env_hashes.get(path)
            if old and new_hash != old and new_hash != "MISSING":
                now_iso = datetime.now(timezone.utc).isoformat()
                self._healing_conn.execute(
                    "INSERT INTO anomalies (detected_at, service, anomaly_type, severity, details) "
                    "VALUES (?,?,?,?,?)",
                    (now_iso, "system", "CONFIG_TAMPERED", "CRITICAL", f"Changed: {path}"),
                )
                self._healing_conn.execute(
                    "INSERT INTO flags (key, value, set_at, set_by) VALUES (?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "set_at=excluded.set_at, set_by=excluded.set_by",
                    ("healing_active", "true", now_iso, "guardian_v4"),
                )
                self._healing_conn.commit()
                self._env_hashes[path] = new_hash
                self._escalation.alert_tier4(
                    "config",
                    f"{path} modified: {old[:16]}... → {new_hash[:16]}...",
                )

    def _check_disk(self) -> None:
        pct = _disk_usage_pct(NEXUS_ROOT)
        if pct >= 90:
            freed = _rotate_logs(LOGS_DIR)
            self._escalation.alert_tier4(
                "disk", f"Disk at {pct:.1f}% — freed {freed//1024}KB via log rotation"
            )
        elif pct >= 80:
            _send_telegram(f"Disk at {pct:.1f}%", tier=2)

    def _check_db_integrity_all(self) -> None:
        now = time.time()
        if now - self._last_db_check_ts < 600:
            return
        self._last_db_check_ts = now

        for svc in SERVICES:
            # Resolve DB path — ext_db (absolute) takes precedence over db (relative)
            ext_db = svc.get("ext_db")
            rel_db = svc.get("db")
            if ext_db:
                db_path = ext_db
            elif rel_db:
                db_path = str(Path(DATA_DIR) / rel_db)
            else:
                continue  # no DB for this service entry
            if not Path(db_path).exists():
                continue

            try:
                conn = sqlite3.connect(db_path, timeout=5)
                result = conn.execute("PRAGMA integrity_check").fetchone()
                ok = result is not None and result[0] == "ok"
                conn.close()
                if not ok:
                    self._escalation.alert_tier4(
                        svc["name"],
                        f"DB corruption in {db_path} — restore from backup required",
                    )
            except sqlite3.Error as exc:
                log.error("DB check failed for %s: %s", svc["name"], exc)

            wal = Path(db_path).with_suffix(".db-wal")
            if wal.exists() and wal.stat().st_size > 50 * 1024 * 1024:
                try:
                    conn = sqlite3.connect(db_path, timeout=10)
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("VACUUM")
                    conn.close()
                    _send_telegram(f"WAL checkpoint `{svc['name']}` ✅", tier=2)
                except sqlite3.Error as exc:
                    log.error("WAL checkpoint failed: %s", exc)

    def _cascade_check(self) -> None:
        """Detect 2+ services with persistent failures → CASCADE."""
        if not _is_market_hours():
            return
        now = time.time()
        recent_failures = [
            s for s, t in self._failure_times.items()
            if now - t < 300
            and len(self._crash_history.get(s, [])) >= 2
        ]
        if len(recent_failures) >= 2:
            flag_val = self._healing_conn.execute(
                "SELECT value FROM flags WHERE key='cascade_alerted'"
            ).fetchone()
            if not flag_val:
                now_iso = datetime.now(timezone.utc).isoformat()
                self._healing_conn.execute(
                    "INSERT INTO flags (key, value, set_at, set_by) VALUES (?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "set_at=excluded.set_at, set_by=excluded.set_by",
                    ("cascade_alerted", "true", now_iso, "guardian_v4"),
                )
                self._healing_conn.execute(
                    "INSERT INTO flags (key, value, set_at, set_by) VALUES (?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "set_at=excluded.set_at, set_by=excluded.set_by",
                    ("healing_active", "true", now_iso, "guardian_v4"),
                )
                self._healing_conn.commit()
                self._escalation.alert_tier4(
                    "cascade",
                    f"CASCADE FAILURE: {', '.join(recent_failures)} — execution frozen",
                )
        else:
            self._healing_conn.execute("DELETE FROM flags WHERE key='cascade_alerted'")
            self._healing_conn.commit()

    def _send_health_pulse(self) -> None:
        svc_parts = []
        for svc in SERVICES:
            ok = svc["name"] not in self._failure_times
            svc_parts.append(f"`{svc['name']}` {'✅' if ok else '❌'}")

        acct = _alpaca_get("/v2/account")
        equity_str = f"${float(acct['equity']):,.0f}" if acct else "⚠️ unreachable"

        anomalies_today = self._healing_conn.execute(
            "SELECT COUNT(*) FROM anomalies WHERE detected_at >= ?",
            (datetime.now().strftime("%Y-%m-%d"),)
        ).fetchone()[0]

        prevented = self._healing_conn.execute(
            "SELECT COUNT(*) FROM prevented_crashes WHERE ts >= ?",
            (datetime.now().strftime("%Y-%m-%d"),)
        ).fetchone()[0]

        _send_telegram(
            f"*NEXUS PULSE — {datetime.now().strftime('%H:%M ET')}*\n\n"
            + "\n".join(svc_parts) + "\n\n"
            f"Alpaca: {equity_str}\n"
            f"Anomalies today: {anomalies_today} | Prevented: {prevented}",
            tier=2,
        )

    def _write_heartbeat(self) -> None:
        """Write timestamp to heartbeat file every loop. Pipeline Sentinel reads this."""
        try:
            Path(HEARTBEAT_PATH).write_text(str(time.time()))
        except OSError as exc:
            log.warning("Heartbeat write failed: %s", exc)

    def _refresh_telemetry_conn(self, new_conn: sqlite3.Connection) -> None:
        """
        Propagate a new telemetry sqlite3.Connection to ALL dependent objects.

        Called by TelemetryCollector._on_conn_replaced after prune_old() recreates
        telemetry.db. Without this, BaselineEngine, ForecastEngine, CausalGraph,
        HealCatalog, and CoherenceVerifier all retain a reference to the closed
        connection and raise "Cannot operate on a closed database" on every check.
        """
        self._telemetry_conn = new_conn
        self._baseline._conn = new_conn
        self._causal._conn = new_conn
        self._forecast._conn = new_conn
        self._catalog._conn = new_conn
        self._coherence._telemetry = new_conn
        log.info("_refresh_telemetry_conn: all dependents updated with new telemetry connection.")

    def _run_omni_e2e_diagnostic(self) -> None:
        """Block 9: 7AM ET pre-market OMNI E2E diagnostic.

        Sends a lightweight health probe to OMNI at 7 AM ET to verify the full
        synthesis path is reachable before market open. Uses a 620s timeout to
        accommodate cold-start brain initialization. Duration is logged to
        CHRONICLE cron_log regardless of outcome.

        Fires once per day — guarded by self._last_e2e_ts.
        """
        _e2e_start = time.time()
        outcome    = "success"
        error_msg  = ""
        try:
            resp = requests.get(
                "http://localhost:8004/health",
                timeout=620,
                headers={"X-Nexus-Secret": NEXUS_SECRET},
            )
            if not resp.ok:
                outcome   = "error"
                error_msg = f"OMNI /health returned {resp.status_code}"
                _send_telegram(
                    f"*7AM E2E DIAGNOSTIC FAILED* — OMNI /health → {resp.status_code}\n"
                    f"Detail: {error_msg}",
                    tier=3,
                )
            else:
                _svc_status = resp.json().get("status", "unknown")
                if _svc_status == "standby":
                    outcome   = "error"
                    error_msg = f"OMNI is in STANDBY at 7AM: {resp.json().get('reason', '')}"
                    _send_telegram(
                        f"*7AM E2E DIAGNOSTIC — OMNI STANDBY*\n{error_msg}\n"
                        "OMNI cannot synthesize. Check Anthropic API key.",
                        tier=3,
                    )
                else:
                    log.info(
                        "Block 9 7AM E2E diagnostic: OMNI healthy (status=%s, %.1fs)",
                        _svc_status, time.time() - _e2e_start,
                    )
        except requests.Timeout:
            outcome   = "timeout"
            error_msg = f"OMNI /health timed out after 620s"
            log.error("Block 9 7AM E2E: %s", error_msg)
            _send_telegram(f"*7AM E2E TIMEOUT* — OMNI /health timed out (>620s)", tier=3)
        except Exception as _e2e_exc:
            outcome   = "error"
            error_msg = str(_e2e_exc)[:200]
            log.error("Block 9 7AM E2E diagnostic error: %s", error_msg)
        finally:
            _log_cron("omni_e2e_7am", _e2e_start, outcome, error_msg)

    def run(self) -> None:
        """Main monitoring loop."""
        log.info("Guardian Angel v4 — Full Coverage running")
        self._collector.start()

        while self._running:
            loop_start = time.time()

            # --- Service health + root cause healing ---
            _sh_start = time.time()
            for svc in SERVICES:
                self._health_check_service(svc)
            _log_cron("service_health_checks", _sh_start, "success")

            # --- Escalation ladder tick ---
            self._escalation.tick()

            # --- Predictive + infrastructure checks ---
            self._check_forecasts()
            self._check_memory_cpu()
            self._check_config_hashes()
            self._check_disk()
            self._check_db_integrity_all()
            self._cascade_check()

            # --- V4: Pipeline telemetry during market hours ---
            if _is_pipeline_hours():
                _pt_start = time.time()
                _pt_err = ""
                try:
                    self._pipeline.run_all()
                    _log_cron("pipeline_telemetry", _pt_start, "success")
                except Exception as _pt_exc:
                    _pt_err = str(_pt_exc)[:200]
                    _log_cron("pipeline_telemetry", _pt_start, "error", _pt_err)
                    raise

            now = time.time()

            # --- Periodic tasks ---
            if now - self._last_coherence_ts >= 300:
                self._last_coherence_ts = now
                self._coherence.run_all()

            if now - self._last_reconcile_ts >= 1800 and _is_market_hours():
                self._last_reconcile_ts = now
                self._coherence.check_alpaca_db_agreement()

            if now - self._last_alpaca_ts >= 900 and _is_market_hours():
                self._last_alpaca_ts = now
                acct = _alpaca_get("/v2/account")
                if acct and (acct.get("trading_blocked") or acct.get("account_blocked")):
                    self._escalation.alert_tier4(
                        "alpaca",
                        f"trading_blocked={acct.get('trading_blocked')} status={acct.get('status')}",
                    )

            if now - self._last_backup_ts >= 3600:
                self._last_backup_ts = now
                _bk_start = time.time()
                try:
                    _backup_dbs(self._healing_conn)
                    _log_cron("hourly_backup", _bk_start, "success")
                except Exception as _bk_exc:
                    _log_cron("hourly_backup", _bk_start, "error", str(_bk_exc)[:200])
                    raise
                # FIX: prune_old() must run while _BACKUP_LOCK is still effectively
                # held — previously it ran AFTER _backup_dbs released the lock, letting
                # TelemetryWriter resume and grab the SQLite connection first, causing
                # "database table is locked" every hour and telemetry.db recreation.
                # Acquiring _BACKUP_LOCK here forces TelemetryWriter to yield during prune.
                # [GENESIS 2026-04-23]
                with _BACKUP_LOCK:
                    self._collector.prune_old()
                log.info("Hourly backup + telemetry prune complete")

            if now - self._last_baseline_ts >= 3600:
                self._last_baseline_ts = now
                try:
                    self._baseline.update_baselines()
                    log.info("Baselines updated")
                except sqlite3.DatabaseError as _db_err:
                    log.error("telemetry.db corrupted (%s) — rebuilding", _db_err)
                    try:
                        os.remove(TELEMETRY_DB_PATH)
                    except OSError:
                        pass
                    self._telemetry_conn = _init_telemetry_db(TELEMETRY_DB_PATH)
                    self._collector._conn = self._telemetry_conn
                    self._baseline._conn = self._telemetry_conn
                    log.info("telemetry.db rebuilt — continuing")

            # V4: External API probes every 15 min during market hours
            if now - self._last_api_probe_ts >= 900 and _is_market_hours():
                self._last_api_probe_ts = now
                self._api_probe.run_all()

            if _is_market_hours() and _is_on_the_hour():
                self._send_health_pulse()

            # Block 9: 7AM ET OMNI E2E diagnostic (once per day, weekdays only)
            try:
                import zoneinfo as _zi
                _tz_e2e  = _zi.ZoneInfo("America/New_York")
                _now_e2e = datetime.now(_tz_e2e)
                if (_now_e2e.weekday() < 5
                        and _now_e2e.hour == 7
                        and now - self._last_e2e_ts > 3600):
                    self._last_e2e_ts = time.time()
                    self._run_omni_e2e_diagnostic()
            except ImportError:
                pass

            # EOD report at 4:30 PM ET on weekdays
            eod_due = False
            try:
                import zoneinfo
                tz = zoneinfo.ZoneInfo("America/New_York")
                now_et = datetime.now(tz)
                if (now_et.weekday() < 5 and now_et.hour == 16 and
                        now_et.minute == 30 and now - self._last_eod_ts > 3600):
                    eod_due = True
            except ImportError:
                pass
            if eod_due:
                self._last_eod_ts = time.time()
                _send_eod_report(self._healing_conn, self._telemetry_conn)

            # V4: Self-heartbeat — written at end of every loop iteration
            self._write_heartbeat()

            elapsed = time.time() - loop_start
            time.sleep(max(0.0, MONITOR_INTERVAL_S - elapsed))

        self._collector.stop()
        log.info("Guardian Angel v4 stopped.")

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _start_health_server(guardian_ref: Any) -> None:
    """
    Serve GET /health on port 8009 in a background thread.
    Uses stdlib http.server — no extra dependencies.
    """
    import http.server
    import json as _json

    HEALTH_PORT = 8009
    HEARTBEAT_FILE = "/Users/ahmedsadek/nexus/data/guardian_heartbeat"

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            """POST /acknowledge?type=<anomaly_type> — resolve false-positive anomalies."""
            from urllib.parse import urlparse, parse_qs
            import sqlite3 as _sqlite3
            parsed = urlparse(self.path)
            if parsed.path != "/acknowledge":
                self.send_response(404); self.end_headers(); return
            params = parse_qs(parsed.query)
            anom_type = params.get("type", [None])[0]
            now = __import__("datetime").datetime.utcnow().isoformat()
            try:
                conn = _sqlite3.connect(HEALING_DB_PATH)
                if anom_type:
                    r = conn.execute(
                        "UPDATE anomalies SET resolved=1, resolved_at=? WHERE anomaly_type=? AND resolved=0",
                        (now, anom_type)
                    )
                else:
                    r = conn.execute(
                        "UPDATE anomalies SET resolved=1, resolved_at=? WHERE resolved=0", (now,)
                    )
                conn.commit()
                resolved = r.rowcount
                remaining = conn.execute("SELECT COUNT(*) FROM anomalies WHERE resolved=0").fetchone()[0]
                conn.close()
                body = _json.dumps({"ok": True, "resolved": resolved, "remaining": remaining}).encode()
                self.send_response(200)
            except Exception as e:
                body = _json.dumps({"ok": False, "error": str(e)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path not in ("/health", "/"):
                self.send_response(404)
                self.end_headers()
                return
            hb_age = None
            try:
                ts = float(Path(HEARTBEAT_FILE).read_text())
                hb_age = round(time.time() - ts, 1)
            except Exception:
                pass
            body = _json.dumps({
                "status":   "healthy",
                "service":  "guardian-angel",
                "version":  "4.0.0",
                "heartbeat_age_s": hb_age,
                "services_monitored": len(SERVICES),
                "pipeline_checks": "active",
                "escalation": "3-tier",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:  # silence access logs
            pass

    server = http.server.HTTPServer(("0.0.0.0", HEALTH_PORT), _Handler)
    log.info("Health endpoint listening on port %d", HEALTH_PORT)
    server.serve_forever()


def main() -> None:
    """Start Guardian Angel v4 daemon."""
    guardian = GuardianAngelV4()

    def _handle_signal(sig: int, _frame: Any) -> None:
        log.info("Signal %d — shutting down", sig)
        guardian.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Start health endpoint in background thread
    health_thread = threading.Thread(
        target=_start_health_server, args=(guardian,), daemon=True, name="health-server"
    )
    health_thread.start()

    guardian.run()


if __name__ == "__main__":
    main()
