"""
Guardian Angel v2 — Nexus Self-Healing System Daemon
=====================================================
The immune system for the Nexus trading platform.
Watches all 7 microservices, detects failure before it becomes disaster,
heals what it can, escalates what it cannot.

Three layers:
  Layer A — WATCH:  Know the state of every component at all times
  Layer B — DETECT: Recognize failure modes before they cause losses
  Layer C — HEAL:   Fix what can be fixed; escalate what cannot

Run with: python guardian_angel.py
"""

import os
import sys
import time
import json
import signal
import hashlib
import logging
import sqlite3
import subprocess
import threading
import gzip
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import psutil
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Config — all from env, fail loudly if missing
# ---------------------------------------------------------------------------

def _require(key: str) -> str:
    """Load required env var; raise on missing."""
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Required env var '{key}' is not set. Check .env file.")
    return val


TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_AHMED_CHAT_ID: str = _require("TELEGRAM_AHMED_CHAT_ID")
# Cipher P2-2 fix: Alpaca credentials are now optional in Guardian Angel.
# INV-8 (credential isolation): only execution services may hold Alpaca creds.
# Guardian Angel uses execution service /positions endpoints for reconciliation
# (which already have Alpaca access). Direct Alpaca calls remain ONLY for
# /v2/account health monitoring which has no other source. If keys are absent,
# account monitoring is disabled — all safety-critical paths remain functional.
ALPACA_API_KEY: str    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_API_SECRET: str = os.environ.get("ALPACA_API_SECRET", "")
ALPACA_BASE_URL: str   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
NEXUS_ROOT: str = os.environ.get("NEXUS_ROOT", "/Users/ahmedsadek/nexus")
DATA_DIR: str = os.environ.get("DATA_DIR", f"{NEXUS_ROOT}/data")
LOGS_DIR: str = os.environ.get("LOGS_DIR", f"{NEXUS_ROOT}/logs")
BACKUP_DIR: str = os.environ.get("BACKUP_DIR", f"{DATA_DIR}/backups")
HEALING_DB_PATH: str = os.environ.get("HEALING_DB_PATH", f"{DATA_DIR}/healing.db")
MONITOR_INTERVAL_S: int = int(os.environ.get("MONITOR_INTERVAL_S", "60"))
NEXUS_SECRET: str       = _require("NEXUS_SECRET")
NEXUS_PRIME_SECRET: str = os.environ.get("NEXUS_PRIME_SECRET", NEXUS_SECRET)  # defaults to shared value

# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------

SERVICES: List[Dict[str, Any]] = [
    {"name": "axiom",           "port": 8001, "health_path": "/health", "launchd": "ai.nexus.axiom",          "db": "axiom.db"},
    {"name": "alpha-buffer",    "port": 8002, "health_path": "/health", "launchd": "ai.nexus.alpha-buffer",   "db": "alpha_buffer.db"},
    {"name": "prime-buffer",    "port": 8003, "health_path": "/health", "launchd": "ai.nexus.prime-buffer",   "db": "prime_buffer.db"},
    {"name": "omni",            "port": 8004, "health_path": "/health", "launchd": "ai.nexus.omni",           "db": "omni.db"},
    {"name": "alpha-execution", "port": 8005, "health_path": "/health", "launchd": "ai.nexus.alpha-execution","db": "alpha_execution.db"},
    {"name": "prime-execution", "port": 8006, "health_path": "/health", "launchd": "ai.nexus.prime-execution","db": "prime_execution.db"},
    {"name": "oracle",          "port": 8007, "health_path": "/ping",   "launchd": "ai.nexus.oracle",         "db": "oracle.db"},
    # OMNI H3 fix: AILS was invisible to Guardian Angel — crashes went undetected
    {"name": "ails",            "port": 8008, "health_path": "/health", "launchd": "ai.nexus.ails",            "db": "ails.db"},
]

ENV_FILES: List[str] = [
    f"{NEXUS_ROOT}/{svc['name']}/.env" for svc in SERVICES
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"{LOGS_DIR}/guardian-angel/guardian_angel.log", mode="a")
            if Path(f"{LOGS_DIR}/guardian-angel").exists() or
               (Path(f"{LOGS_DIR}/guardian-angel").mkdir(parents=True, exist_ok=True) or True)
            else logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("guardian")

# ---------------------------------------------------------------------------
# Healing database
# ---------------------------------------------------------------------------

def _init_healing_db(path: str) -> sqlite3.Connection:
    """Initialize healing.db with WAL mode and all tables."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS anomalies (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT    NOT NULL,
            service     TEXT    NOT NULL,
            anomaly_type TEXT   NOT NULL,
            severity    TEXT    NOT NULL,
            details     TEXT,
            resolved    INTEGER DEFAULT 0,
            resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS healing_actions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            anomaly_id      INTEGER,
            action_at       TEXT    NOT NULL,
            service         TEXT    NOT NULL,
            heal_type       TEXT    NOT NULL,
            success         INTEGER NOT NULL,
            time_to_heal_s  REAL,
            notes           TEXT
        );
        CREATE TABLE IF NOT EXISTS patterns (
            service         TEXT NOT NULL,
            anomaly_type    TEXT NOT NULL,
            occurrence_count INTEGER DEFAULT 1,
            first_seen      TEXT NOT NULL,
            last_seen       TEXT NOT NULL,
            UNIQUE(service, anomaly_type)
        );
        CREATE TABLE IF NOT EXISTS flags (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL,
            set_at  TEXT NOT NULL,
            set_by  TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Diagnostic classifier (7 failure types)
# ---------------------------------------------------------------------------

FAILURE_TYPES: Dict[str, Dict[str, str]] = {
    "CONNECTION_REFUSED": {
        "label": "Connection Refused",
        "emoji": "🔴",
        "cause": "Service not listening on port — crashed or never started",
        "action": "RESTART",
    },
    "TIMEOUT": {
        "label": "Response Timeout",
        "emoji": "🟠",
        "cause": "Service alive but unresponsive — memory leak or deadlock",
        "action": "RESTART",
    },
    "HTTP_500": {
        "label": "Internal Server Error",
        "emoji": "🟠",
        "cause": "Service running but erroring — bad state or DB issue",
        "action": "RESTART",
    },
    "HTTP_503": {
        "label": "Service Unavailable",
        "emoji": "🟡",
        "cause": "Temporary overload or startup",
        "action": "WAIT_RETRY",
    },
    "DNS_FAILURE": {
        "label": "DNS Failure",
        "emoji": "🔴",
        "cause": "Network configuration issue",
        "action": "ALERT_ONLY",
    },
    "HEARTBEAT_MISSED": {
        "label": "Heartbeat Missed",
        "emoji": "🟠",
        "cause": "Service stopped sending heartbeats — likely frozen",
        "action": "RESTART",
    },
    "UNKNOWN": {
        "label": "Unknown Failure",
        "emoji": "❓",
        "cause": "Unclassified error",
        "action": "ALERT_ONLY",
    },
}


def classify_failure(exc: Exception, status_code: Optional[int] = None) -> str:
    """Classify exception into one of 7 failure types."""
    err_str = str(exc).lower()
    if status_code == 500:
        return "HTTP_500"
    if status_code == 503:
        return "HTTP_503"
    if "connection refused" in err_str or "connection error" in err_str:
        return "CONNECTION_REFUSED"
    if "timeout" in err_str or "timed out" in err_str:
        return "TIMEOUT"
    if "name or service not known" in err_str or "nodename nor servname" in err_str:
        return "DNS_FAILURE"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _send_telegram(message: str, tier: int) -> None:
    """Send Telegram message to Ahmed. tier 1 = silent (no send)."""
    if tier < 2:
        return
    prefix = {2: "🔧", 3: "⚠️", 4: "🚨"}.get(tier, "ℹ️")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_AHMED_CHAT_ID,
        "text": f"{prefix} *GUARDIAN ANGEL*\n\n{message}",
        "parse_mode": "Markdown",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            log.error("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
    except requests.RequestException as exc:
        log.error("Telegram send exception: %s", exc)


# ---------------------------------------------------------------------------
# launchctl helpers
# ---------------------------------------------------------------------------

def _launchctl_restart(service_name: str) -> bool:
    """Restart a launchd service via launchctl kickstart -k."""
    uid = os.getuid()
    label = f"ai.nexus.{service_name}"
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.error("launchctl restart failed for %s: %s", service_name, exc)
        return False


def _get_service_pid(service_name: str) -> Optional[int]:
    """Get PID of a running launchd service."""
    label = f"ai.nexus.{service_name}"
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.split("\n"):
            stripped = line.strip()
            if '"PID"' in stripped:
                try:
                    return int(stripped.split("=")[-1].strip().rstrip(";").strip())
                except (ValueError, IndexError):
                    pass
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("launchctl list failed for %s: %s", service_name, exc)
    return None


def _sigterm_service(pid: int) -> None:
    """Send SIGTERM to a process."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# Alpaca client
# ---------------------------------------------------------------------------

def _alpaca_get(path: str) -> Optional[Dict[str, Any]]:
    """GET from Alpaca paper API."""
    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
    }
    try:
        resp = requests.get(
            f"{ALPACA_BASE_URL}{path}",
            headers=headers, timeout=10,
        )
        if resp.ok:
            return resp.json()
        log.warning("Alpaca %s returned %s", path, resp.status_code)
    except requests.RequestException as exc:
        log.error("Alpaca request failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Healing database operations
# ---------------------------------------------------------------------------

class HealingDB:
    """Thread-safe healing database interface."""

    def __init__(self, path: str) -> None:
        """Initialize with database path."""
        self._conn = _init_healing_db(path)
        self._lock = threading.Lock()

    def log_anomaly(self, service: str, anomaly_type: str, severity: str, details: str) -> int:
        """Log a detected anomaly; return its ID."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO anomalies (detected_at, service, anomaly_type, severity, details) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, service, anomaly_type, severity, details),
            )
            self._conn.commit()
            anomaly_id = cur.lastrowid
            # Update pattern count
            self._conn.execute(
                "INSERT INTO patterns (service, anomaly_type, occurrence_count, first_seen, last_seen) "
                "VALUES (?, ?, 1, ?, ?) "
                "ON CONFLICT(service, anomaly_type) DO UPDATE SET "
                "occurrence_count = occurrence_count + 1, last_seen = excluded.last_seen",
                (service, anomaly_type, now, now),
            )
            self._conn.commit()
            # Check pattern threshold (3 = recurring)
            row = self._conn.execute(
                "SELECT occurrence_count FROM patterns WHERE service=? AND anomaly_type=?",
                (service, anomaly_type),
            ).fetchone()
            if row and row[0] == 3:
                log.warning("RECURRING PATTERN: %s / %s (3rd occurrence — feeds AILS)", service, anomaly_type)
            return anomaly_id

    def log_heal(self, anomaly_id: int, service: str, heal_type: str,
                 success: bool, time_s: float, notes: str) -> None:
        """Log a healing action taken."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO healing_actions "
                "(anomaly_id, action_at, service, heal_type, success, time_to_heal_s, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (anomaly_id, now, service, heal_type, int(success), time_s, notes),
            )
            if success:
                self._conn.execute(
                    "UPDATE anomalies SET resolved=1, resolved_at=? WHERE id=?",
                    (now, anomaly_id),
                )
            self._conn.commit()

    def set_flag(self, key: str, value: str, set_by: str) -> None:
        """Set a system flag (e.g. healing_active)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO flags (key, value, set_at, set_by) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, set_at=excluded.set_at, set_by=excluded.set_by",
                (key, value, now, set_by),
            )
            self._conn.commit()

    def get_flag(self, key: str) -> Optional[str]:
        """Get a flag value."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM flags WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None

    def clear_flag(self, key: str) -> None:
        """Clear a flag."""
        with self._lock:
            self._conn.execute("DELETE FROM flags WHERE key=?", (key,))
            self._conn.commit()

    def count_today(self) -> Tuple[int, int]:
        """Return (anomalies_today, heals_today)."""
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            a = self._conn.execute(
                "SELECT COUNT(*) FROM anomalies WHERE detected_at LIKE ?", (f"{today}%",)
            ).fetchone()[0]
            h = self._conn.execute(
                "SELECT COUNT(*) FROM healing_actions WHERE action_at LIKE ? AND success=1",
                (f"{today}%",)
            ).fetchone()[0]
            return a, h

    def recent_crashes(self, service: str, window_minutes: int = 10) -> int:
        """Count crash anomalies for a service in the last N minutes."""
        cutoff = (datetime.now(timezone.utc).timestamp() - window_minutes * 60)
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM anomalies "
                "WHERE service=? AND anomaly_type IN ('SERVICE_CRASH','CONNECTION_REFUSED') "
                "AND detected_at >= ?",
                (service, cutoff_iso),
            ).fetchone()
            return row[0] if row else 0


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

class ServiceState:
    """Per-service runtime state."""

    def __init__(self, name: str) -> None:
        """Initialize state for a service."""
        self.name = name
        self.consecutive_failures: int = 0
        self.response_times: List[float] = []  # rolling 10-check window
        self.error_rate_window: List[bool] = []  # True = success
        self.last_ok_ts: float = time.time()
        self.last_restart_ts: float = 0.0

    def record_success(self, response_time_s: float) -> None:
        """Record a successful health check."""
        self.consecutive_failures = 0
        self.last_ok_ts = time.time()
        self.response_times.append(response_time_s)
        if len(self.response_times) > 10:
            self.response_times.pop(0)
        self.error_rate_window.append(True)
        if len(self.error_rate_window) > 20:
            self.error_rate_window.pop(0)

    def record_failure(self) -> None:
        """Record a failed health check."""
        self.consecutive_failures += 1
        self.error_rate_window.append(False)
        if len(self.error_rate_window) > 20:
            self.error_rate_window.pop(0)

    @property
    def avg_response_time(self) -> float:
        """Rolling average response time in seconds."""
        if not self.response_times:
            return 0.0
        return sum(self.response_times) / len(self.response_times)

    @property
    def error_rate(self) -> float:
        """Error rate 0.0–1.0 over last 20 checks."""
        if not self.error_rate_window:
            return 0.0
        return 1.0 - (sum(self.error_rate_window) / len(self.error_rate_window))


# ---------------------------------------------------------------------------
# Config hash monitor
# ---------------------------------------------------------------------------

def _hash_env_files() -> Dict[str, str]:
    """SHA-256 hash of every service .env file."""
    hashes: Dict[str, str] = {}
    for path in ENV_FILES:
        if Path(path).exists():
            content = Path(path).read_bytes()
            hashes[path] = hashlib.sha256(content).hexdigest()
        else:
            hashes[path] = "MISSING"
    return hashes


# ---------------------------------------------------------------------------
# Disk utilities
# ---------------------------------------------------------------------------

def _disk_usage_pct(path: str) -> float:
    """Return disk usage percentage for the volume containing path."""
    usage = shutil.disk_usage(path)
    return (usage.used / usage.total) * 100.0


def _rotate_logs(logs_dir: str) -> int:
    """Gzip logs older than 7 days; delete .gz older than 30 days. Return bytes freed."""
    freed = 0
    cutoff_7d = time.time() - 7 * 86400
    cutoff_30d = time.time() - 30 * 86400
    for log_file in Path(logs_dir).rglob("*.log"):
        if log_file.stat().st_mtime < cutoff_7d:
            gz_path = log_file.with_suffix(".log.gz")
            with open(log_file, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            freed += log_file.stat().st_size
            log_file.unlink()
    for gz_file in Path(logs_dir).rglob("*.log.gz"):
        if gz_file.stat().st_mtime < cutoff_30d:
            freed += gz_file.stat().st_size
            gz_file.unlink()
    return freed


# ---------------------------------------------------------------------------
# DB backup
# ---------------------------------------------------------------------------

def _backup_dbs() -> None:
    """Hourly snapshot of all service DBs to /data/backups/."""
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    hour_tag = datetime.now().strftime("%Y%m%d_%H")
    for svc in SERVICES:
        src = Path(DATA_DIR) / svc["db"]
        if not src.exists():
            continue
        dst = Path(BACKUP_DIR) / f"{svc['name']}_{hour_tag}.db"
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            log.error("Backup failed for %s: %s", svc["name"], exc)
    # Prune: keep last 24 per service
    for svc in SERVICES:
        backups = sorted(Path(BACKUP_DIR).glob(f"{svc['name']}_*.db"), reverse=True)
        for old in backups[24:]:
            old.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# SQLite integrity check
# ---------------------------------------------------------------------------

def _check_db_integrity(db_path: str) -> bool:
    """Run PRAGMA integrity_check; return True if OK."""
    if not Path(db_path).exists():
        return True  # not created yet
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return result is not None and result[0] == "ok"
    except sqlite3.Error as exc:
        log.error("DB integrity check failed for %s: %s", db_path, exc)
        return False


def _wal_size_bytes(db_path: str) -> int:
    """Return WAL file size in bytes (0 if not present)."""
    wal = Path(db_path).with_suffix(".db-wal")
    if wal.exists():
        return wal.stat().st_size
    return 0


def _checkpoint_wal(db_path: str) -> bool:
    """Force WAL checkpoint + VACUUM."""
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.close()
        return True
    except sqlite3.Error as exc:
        log.error("WAL checkpoint failed for %s: %s", db_path, exc)
        return False


# ---------------------------------------------------------------------------
# Alpaca DB reconciliation
# ---------------------------------------------------------------------------

def _get_db_positions(db_name: str) -> List[Dict[str, Any]]:
    """Fetch active positions from an execution service DB.

    Cipher P2-1 fix: original query used 'symbol', 'qty', 'side' — column names
    that don't exist in either execution service schema. The OperationalError was
    caught at debug level and returned [] — orphan detection has never worked.

    Alpha Execution schema: ticker, contracts, direction
    Prime Execution schema: ticker, shares, direction
    Both aliased to symbol/qty/side for uniform comparison downstream.
    """
    db_path = Path(DATA_DIR) / db_name
    if not db_path.exists():
        return []
    positions: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        # Detect which qty column exists — alpha uses 'contracts', prime uses 'shares'
        col_info = {row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
        qty_col = "contracts" if "contracts" in col_info else "shares"
        rows = conn.execute(
            f"SELECT ticker AS symbol, {qty_col} AS qty, direction AS side "
            f"FROM positions WHERE status IN ('open', 'pending')"
        ).fetchall()
        conn.close()
        for row in rows:
            positions.append({"symbol": row["symbol"], "qty": row["qty"], "side": row["side"]})
    except sqlite3.Error as exc:
        log.warning("DB position fetch failed for %s: %s", db_name, exc)  # WARNING not DEBUG
    return positions


def _get_alpaca_positions_via_services() -> Optional[Dict[str, Any]]:
    """
    Fetch Alpaca positions via execution services rather than directly.

    Cipher P2-2 fix: Guardian Angel should NOT call Alpaca directly for
    reconciliation — execution services already hold Alpaca credentials
    and provide /positions endpoints for exactly this purpose.

    Returns dict of {symbol: position_data} or None on failure.
    """
    positions: Dict[str, Any] = {}
    service_map = [
        ("http://localhost:8005/positions", {"X-Nexus-Secret":       NEXUS_SECRET}),
        ("http://localhost:8006/positions", {"X-Nexus-Prime-Secret": NEXUS_PRIME_SECRET}),
    ]
    for url, headers in service_map:
        try:
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.ok:
                data = resp.json()
                # Response format: {"positions": [...]} or list directly
                pos_list = data.get("positions", data) if isinstance(data, dict) else data
                for p in (pos_list if isinstance(pos_list, list) else []):
                    sym = p.get("ticker") or p.get("symbol")
                    if sym:
                        positions[sym] = p
            else:
                log.warning("Position fetch from %s returned %s", url, resp.status_code)
        except requests.RequestException as exc:
            log.error("Position fetch from %s failed: %s", url, exc)
    return positions if positions is not None else None


def _reconcile_positions(healing_db: "HealingDB") -> None:
    """Compare execution service positions vs DB — detect orphans and drift.

    Cipher P2-2 fix: uses execution service /positions endpoints instead of
    calling Alpaca directly. Guardian Angel no longer needs Alpaca credentials
    for reconciliation.
    """
    alpaca_positions = _get_alpaca_positions_via_services()
    if alpaca_positions is None:
        return

    db_positions: List[Dict[str, Any]] = []
    for db_name in ["alpha_execution.db", "prime_execution.db"]:
        db_positions.extend(_get_db_positions(db_name))

    db_symbols = {p["symbol"] for p in db_positions}
    alpaca_symbols = set(alpaca_positions.keys())

    # In Alpaca, not in DB → orphan
    for sym in alpaca_symbols - db_symbols:
        aid = healing_db.log_anomaly(
            "execution", "ORPHAN_POSITION", "CRITICAL",
            f"Position {sym} in Alpaca but not in any DB",
        )
        healing_db.set_flag("healing_active", "true", "guardian_angel")
        msg = (
            f"🚨 *ORPHAN POSITION DETECTED*\n\n"
            f"Symbol: `{sym}`\n"
            f"Alpaca qty: {alpaca_positions[sym].get('qty')}\n"
            f"Not found in any Nexus DB.\n\n"
            f"*New executions FROZEN. Your action required.*"
        )
        _send_telegram(msg, tier=4)
        log.critical("ORPHAN POSITION: %s — execution frozen", sym)

    # In DB, not in Alpaca → closed externally
    for sym in db_symbols - alpaca_symbols:
        aid = healing_db.log_anomaly(
            "execution", "POSITION_CLOSED_EXTERNALLY", "HIGH",
            f"Position {sym} in DB but not in Alpaca — closed externally",
        )
        _send_telegram(
            f"Position `{sym}` closed externally (in DB, not Alpaca). DB will need update.",
            tier=3,
        )
        healing_db.log_heal(aid, "execution", "ALERT_ONLY", True, 0.0, "Alerted Ahmed")


# ---------------------------------------------------------------------------
# Memory/CPU monitor (psutil)
# ---------------------------------------------------------------------------

def _check_process_resources(
    service_name: str, pid: int, healing_db: "HealingDB",
    states: Dict[str, "ServiceState"],
) -> None:
    """Check memory and CPU usage for a service PID."""
    try:
        proc = psutil.Process(pid)
        mem_pct = proc.memory_percent()
        cpu_pct = proc.cpu_percent(interval=1)

        if mem_pct > 85:
            aid = healing_db.log_anomaly(service_name, "MEMORY_LEAK", "HIGH",
                f"Memory {mem_pct:.1f}% — restarting (threshold 85%)")
            t0 = time.time()
            ok = _launchctl_restart(service_name)
            healing_db.log_heal(aid, service_name, "RESTART_MEMORY",
                ok, time.time() - t0, f"mem={mem_pct:.1f}%")
            _send_telegram(
                f"*MEMORY_LEAK* — `{service_name}` at {mem_pct:.1f}% RSS. "
                f"{'Restarted ✅' if ok else 'Restart FAILED ❌'}",
                tier=3,
            )
        elif mem_pct > 75:
            healing_db.log_anomaly(service_name, "MEMORY_WARNING", "MEDIUM",
                f"Memory {mem_pct:.1f}% — watch (threshold 75%)")
            _send_telegram(f"⚠️ `{service_name}` memory at {mem_pct:.1f}% — watching", tier=2)

    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        log.debug("psutil check skipped for %s (pid %s): %s", service_name, pid, exc)


# ---------------------------------------------------------------------------
# Health check + healing
# ---------------------------------------------------------------------------

def _health_check(svc: Dict[str, Any], state: "ServiceState",
                  healing_db: "HealingDB", crash_history: Dict[str, List[float]]) -> bool:
    """
    Poll a service health endpoint. Classify failure, heal, escalate.
    Returns True if healthy.
    """
    name = svc["name"]
    url = f"http://localhost:{svc['port']}{svc['health_path']}"
    status_code: Optional[int] = None
    t0 = time.time()

    try:
        resp = requests.get(url, timeout=5, headers={"X-Nexus-Secret": NEXUS_SECRET})
        elapsed = time.time() - t0
        status_code = resp.status_code

        if resp.ok:
            state.record_success(elapsed)
            # Response time drift detection
            if state.avg_response_time > 3.0 and len(state.response_times) >= 5:
                healing_db.log_anomaly(name, "RESPONSE_TIME_DRIFT", "MEDIUM",
                    f"Avg response {state.avg_response_time:.2f}s (threshold 3.0s)")
                _send_telegram(
                    f"⚠️ `{name}` response time drift: avg {state.avg_response_time:.2f}s",
                    tier=2,
                )
            return True
        else:
            raise requests.HTTPError(f"HTTP {status_code}")

    except (requests.ConnectionError, requests.HTTPError,
            requests.Timeout, requests.RequestException) as exc:
        state.record_failure()
        elapsed = time.time() - t0

        # Only act on 2+ consecutive failures
        if state.consecutive_failures < 2:
            log.warning("%s health check failed (%d/2): %s", name, state.consecutive_failures, exc)
            return False

        failure_type = classify_failure(exc, status_code)
        ft_info = FAILURE_TYPES.get(failure_type, FAILURE_TYPES["UNKNOWN"])

        log.error("%s %s %s — %s", ft_info["emoji"], name, ft_info["label"], ft_info["cause"])

        # Check repeated crashes
        now_ts = time.time()
        crash_history.setdefault(name, [])
        crash_history[name] = [t for t in crash_history[name] if now_ts - t < 600]
        crash_history[name].append(now_ts)

        if len(crash_history[name]) >= 3:
            # REPEATED_CRASHES — stop restarting, escalate
            aid = healing_db.log_anomaly(name, "REPEATED_CRASHES", "CRITICAL",
                f"3+ crashes in 10 minutes — stopping auto-restart")
            healing_db.set_flag("healing_active", "true", "guardian_angel")
            _send_telegram(
                f"🚨 *REPEATED CRASHES* — `{name}`\n\n"
                f"Crashed 3+ times in 10 minutes.\n"
                f"Auto-restart SUSPENDED.\n"
                f"*Your intervention required.*\n\n"
                f"Run: `launchctl kickstart -k gui/$(id -u)/ai.nexus.{name}`",
                tier=4,
            )
            healing_db.log_heal(aid, name, "ESCALATED_NO_RESTART", False, 0.0,
                "3 crashes in 10min — escalated to Ahmed")
            return False

        if ft_info["action"] in ("RESTART", "WAIT_RETRY"):
            aid = healing_db.log_anomaly(name, failure_type, "HIGH",
                f"{ft_info['cause']} (consecutive: {state.consecutive_failures})")
            t_heal = time.time()
            ok = _launchctl_restart(name)
            elapsed_heal = time.time() - t_heal

            # Verify recovery
            time.sleep(5)
            try:
                verify = requests.get(url, timeout=5, headers={"X-Nexus-Secret": NEXUS_SECRET})
                recovered = verify.ok
            except requests.RequestException:
                recovered = False

            healing_db.log_heal(aid, name, f"RESTART_{failure_type}",
                recovered, elapsed_heal, f"Restart {'ok' if ok else 'failed'}, verify {'ok' if recovered else 'failed'}")

            if recovered:
                state.consecutive_failures = 0
                _send_telegram(
                    f"🔧 *FIXED* — `{name}` restarted and recovered\n"
                    f"Failure: {ft_info['label']}\n"
                    f"Heal time: {elapsed_heal:.1f}s",
                    tier=2,
                )
                log.info("✅ %s recovered after restart", name)
            else:
                _send_telegram(
                    f"⚠️ *RESTART FAILED* — `{name}` not responding after restart\n"
                    f"Failure: {ft_info['label']}\n"
                    f"Check logs: `tail -50 {LOGS_DIR}/{name}/stderr.log`",
                    tier=3,
                )
        else:
            aid = healing_db.log_anomaly(name, failure_type, "MEDIUM",
                ft_info["cause"])
            healing_db.log_heal(aid, name, "ALERT_ONLY", True, 0.0, "Non-restartable failure")
            _send_telegram(f"⚠️ `{name}` — {ft_info['label']}: {ft_info['cause']}", tier=3)

        return False


# ---------------------------------------------------------------------------
# Execution pause check
# ---------------------------------------------------------------------------

def _auth_header_for(svc_name: str) -> Dict[str, str]:
    """Return the correct auth header for a given execution service.

    Cipher P2-5 fix: original code always sent X-Nexus-Secret for all services.
    Prime-execution requires X-Nexus-Prime-Secret — every auto-resume call to
    prime-execution was silently rejected with 403.
    """
    if svc_name == "prime-execution":
        return {"X-Nexus-Prime-Secret": NEXUS_PRIME_SECRET}
    return {"X-Nexus-Secret": NEXUS_SECRET}


def _check_execution_paused(svc: Dict[str, Any], healing_db: "HealingDB") -> None:
    """Check if an execution service is paused; auto-resume if reconciler clean.

    Cipher P2-5 fix: /resume endpoint now exists on both execution services.
    Uses service-appropriate auth header via _auth_header_for().
    """
    name    = svc["name"]
    headers = _auth_header_for(name)
    url     = f"http://localhost:{svc['port']}/health"
    try:
        resp = requests.get(url, timeout=5, headers=headers)
        if not resp.ok:
            return
        data = resp.json()
        if data.get("execution_paused") or data.get("paused"):
            aid = healing_db.log_anomaly(name, "EXECUTION_PAUSED", "MEDIUM",
                "Execution paused flag detected")
            resume_url = f"http://localhost:{svc['port']}/resume"
            r2 = requests.post(resume_url, timeout=5, headers=headers, json={})
            success = r2.ok
            healing_db.log_heal(aid, name, "AUTO_RESUME", success, 0.0,
                f"Resume response: {r2.status_code if hasattr(r2, 'status_code') else 'err'}")
            _send_telegram(
                f"{'🔧 *FIXED*' if success else '⚠️ *ALERT*'} — `{name}` execution "
                f"{'resumed ✅' if success else 'PAUSED and auto-resume failed ❌'}",
                tier=2 if success else 3,
            )
    except requests.RequestException:
        pass


# ---------------------------------------------------------------------------
# Cascade detection
# ---------------------------------------------------------------------------

def _check_cascade(failure_times: Dict[str, float], healing_db: "HealingDB") -> None:
    """Detect 2+ services failing within 5 minutes → cascade."""
    now = time.time()
    recent = [svc for svc, t in failure_times.items() if now - t < 300]
    if len(recent) >= 2:
        key = "cascade_alerted"
        if healing_db.get_flag(key):
            return  # Already alerted for this cascade
        healing_db.set_flag(key, "true", "guardian_angel")
        healing_db.set_flag("healing_active", "true", "guardian_angel")
        aid = healing_db.log_anomaly(
            "system", "CASCADE_FAILURE", "CRITICAL",
            f"Services failing together: {', '.join(recent)}",
        )
        healing_db.log_heal(aid, "system", "CASCADE_DETECTED", False, 0.0,
            "Set healing_active, alerted Ahmed")
        _send_telegram(
            f"🚨 *CASCADE FAILURE DETECTED*\n\n"
            f"Services failing together: `{', '.join(recent)}`\n\n"
            f"New executions FROZEN.\n"
            f"*Immediate investigation required.*",
            tier=4,
        )
        log.critical("CASCADE: %s", recent)
    else:
        # Clear cascade flag if resolved
        healing_db.clear_flag("cascade_alerted")


# ---------------------------------------------------------------------------
# Market hours helper
# ---------------------------------------------------------------------------

def _is_market_hours() -> bool:
    """Return True if current ET time is between 9:25 and 16:05 on a weekday."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("America/New_York")
    except ImportError:
        return False
    now = datetime.now(tz)
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    start = now.replace(hour=9, minute=25, second=0, microsecond=0)
    end = now.replace(hour=16, minute=5, second=0, microsecond=0)
    return start <= now <= end


def _is_on_the_hour() -> bool:
    """Return True if current time is within the first 60s of an hour."""
    now = datetime.now()
    return now.minute == 0 and now.second < MONITOR_INTERVAL_S


# ---------------------------------------------------------------------------
# Hourly health pulse
# ---------------------------------------------------------------------------

def _send_health_pulse(states: Dict[str, "ServiceState"],
                       healing_db: "HealingDB") -> None:
    """Send hourly health summary to Ahmed."""
    lines: List[str] = []
    now_str = datetime.now().strftime("%H:%M ET")
    lines.append(f"💓 *NEXUS HEALTH PULSE — {now_str}*\n")

    # Service status
    svc_parts: List[str] = []
    for svc in SERVICES:
        state = states.get(svc["name"])
        ok = state is not None and state.consecutive_failures == 0
        svc_parts.append(f"`{svc['name']}` {'✅' if ok else '❌'}")
    lines.append("*Services:*\n" + " | ".join(svc_parts))

    # Alpaca account
    acct = _alpaca_get("/v2/account")
    if acct:
        equity = float(acct.get("equity", 0))
        blocked = acct.get("trading_blocked", False)
        lines.append(f"\n*Alpaca:* Equity ${equity:,.0f} | Trading {'🔴 BLOCKED' if blocked else '✅ OK'}")
    else:
        lines.append("\n*Alpaca:* ⚠️ Unreachable")

    # Healing stats
    anomalies_today, heals_today = healing_db.count_today()
    lines.append(f"\n*Healing:* {anomalies_today} anomalies | {heals_today} resolved today")

    # Flags
    healing_active = healing_db.get_flag("healing_active")
    if healing_active:
        lines.append("\n🚨 *healing_active FLAG IS SET — new trades blocked*")

    _send_telegram("\n".join(lines), tier=2)


# ---------------------------------------------------------------------------
# Main Guardian Angel daemon
# ---------------------------------------------------------------------------

class GuardianAngel:
    """
    The Guardian Angel daemon.
    Watches, detects, and heals all Nexus services.
    """

    def __init__(self) -> None:
        """Initialize Guardian Angel."""
        Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
        Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)

        self._healing_db = HealingDB(HEALING_DB_PATH)
        self._states: Dict[str, ServiceState] = {
            svc["name"]: ServiceState(svc["name"]) for svc in SERVICES
        }
        self._crash_history: Dict[str, List[float]] = {}
        self._failure_times: Dict[str, float] = {}  # service → last failure epoch
        self._env_hashes: Dict[str, str] = _hash_env_files()
        self._last_db_check_ts: float = 0.0
        self._last_reconcile_ts: float = 0.0
        self._last_alpaca_ts: float = 0.0
        self._last_backup_ts: float = 0.0
        self._running = True

        log.info("🌱 Guardian Angel v2 initialized — watching %d services", len(SERVICES))

    def _check_config_hashes(self) -> None:
        """Detect unauthorized .env file changes."""
        current = _hash_env_files()
        for path, new_hash in current.items():
            old_hash = self._env_hashes.get(path)
            if old_hash is None:
                self._env_hashes[path] = new_hash
                continue
            if new_hash != old_hash and new_hash != "MISSING":
                aid = self._healing_db.log_anomaly(
                    "system", "CONFIG_TAMPERED", "CRITICAL",
                    f".env changed: {path}",
                )
                self._healing_db.set_flag("healing_active", "true", "guardian_angel")
                self._healing_db.log_heal(aid, "system", "ALERT_ONLY", True, 0.0,
                    f"Hash changed: {old_hash[:16]}... → {new_hash[:16]}...")
                _send_telegram(
                    f"🚨 *CONFIG TAMPERED*\n\n"
                    f"File modified: `{path}`\n\n"
                    f"Old hash: `{old_hash[:16]}...`\n"
                    f"New hash: `{new_hash[:16]}...`\n\n"
                    f"*healing_active set. Investigate immediately.*",
                    tier=4,
                )
                # Update stored hash to avoid repeat alerts
                self._env_hashes[path] = new_hash

    def _check_disk(self) -> None:
        """Monitor disk usage and rotate logs if critical."""
        pct = _disk_usage_pct(NEXUS_ROOT)
        if pct >= 90:
            aid = self._healing_db.log_anomaly(
                "system", "DISK_CRITICAL", "CRITICAL", f"Disk {pct:.1f}% used"
            )
            freed = _rotate_logs(LOGS_DIR)
            self._healing_db.log_heal(aid, "system", "LOG_ROTATION", True, 0.0,
                f"Freed {freed // 1024}KB via log rotation")
            _send_telegram(
                f"🚨 *DISK CRITICAL* — {pct:.1f}% used\n"
                f"Emergency log rotation ran — freed {freed // 1024}KB",
                tier=4,
            )
        elif pct >= 80:
            self._healing_db.log_anomaly(
                "system", "DISK_WARNING", "MEDIUM", f"Disk {pct:.1f}% used"
            )
            _send_telegram(f"⚠️ Disk at {pct:.1f}% — approaching critical", tier=2)

    def _check_db_integrity_all(self) -> None:
        """Run integrity check on all service DBs every 10 minutes."""
        now = time.time()
        if now - self._last_db_check_ts < 600:
            return
        self._last_db_check_ts = now

        for svc in SERVICES:
            db_path = str(Path(DATA_DIR) / svc["db"])
            if not Path(db_path).exists():
                continue
            if not _check_db_integrity(db_path):
                aid = self._healing_db.log_anomaly(
                    svc["name"], "DB_CORRUPTION", "CRITICAL",
                    f"PRAGMA integrity_check failed for {db_path}",
                )
                _send_telegram(
                    f"🚨 *DB CORRUPTION* — `{svc['name']}`\n"
                    f"Path: `{db_path}`\n"
                    f"Restore from backup required.",
                    tier=4,
                )
                self._healing_db.log_heal(aid, svc["name"], "ALERT_ONLY", True, 0.0,
                    "Alerted Ahmed — manual restore required")

            # WAL check
            wal_bytes = _wal_size_bytes(db_path)
            if wal_bytes > 50 * 1024 * 1024:  # 50MB
                aid = self._healing_db.log_anomaly(
                    svc["name"], "WAL_STUCK", "MEDIUM",
                    f"WAL size {wal_bytes // 1048576}MB (threshold 50MB)",
                )
                ok = _checkpoint_wal(db_path)
                self._healing_db.log_heal(aid, svc["name"], "WAL_CHECKPOINT", ok, 0.0,
                    f"wal_checkpoint(TRUNCATE) + VACUUM {'ok' if ok else 'failed'}")
                _send_telegram(
                    f"🔧 WAL checkpoint `{svc['name']}` — "
                    f"{'completed ✅' if ok else 'FAILED ❌'} ({wal_bytes // 1048576}MB)",
                    tier=2,
                )

    def _maybe_backup(self) -> None:
        """Hourly DB backup."""
        now = time.time()
        if now - self._last_backup_ts >= 3600:
            self._last_backup_ts = now
            _backup_dbs()
            log.info("Hourly DB backup completed")

    def _maybe_reconcile(self) -> None:
        """DB/Alpaca reconciliation every 30 min during market hours."""
        if not _is_market_hours():
            return
        now = time.time()
        if now - self._last_reconcile_ts >= 1800:
            self._last_reconcile_ts = now
            _reconcile_positions(self._healing_db)

    def _maybe_check_alpaca(self) -> None:
        """Alpaca account status check every 15 min during market hours."""
        if not _is_market_hours():
            return
        now = time.time()
        if now - self._last_alpaca_ts < 900:
            return
        self._last_alpaca_ts = now
        acct = _alpaca_get("/v2/account")
        if acct is None:
            aid = self._healing_db.log_anomaly(
                "alpaca", "API_DEGRADED", "HIGH", "Alpaca API unreachable"
            )
            _send_telegram("⚠️ Alpaca API unreachable — check connectivity", tier=3)
        elif acct.get("trading_blocked") or acct.get("account_blocked"):
            self._healing_db.log_anomaly(
                "alpaca", "TRADING_BLOCKED", "CRITICAL",
                f"trading_blocked={acct.get('trading_blocked')} account_blocked={acct.get('account_blocked')}",
            )
            _send_telegram(
                f"🚨 *ALPACA TRADING BLOCKED*\n\n"
                f"`trading_blocked`: {acct.get('trading_blocked')}\n"
                f"`account_blocked`: {acct.get('account_blocked')}\n"
                f"Status: {acct.get('status')}",
                tier=4,
            )

    def run(self) -> None:
        """Main monitoring loop."""
        log.info("🌱 Guardian Angel v2 — monitoring loop started (interval=%ds)", MONITOR_INTERVAL_S)
        _send_telegram("🌱 *Guardian Angel v2 started*\nAll systems under watch.", tier=2)

        while self._running:
            loop_start = time.time()

            # --- Layer A: Watch every service ---
            for svc in SERVICES:
                name = svc["name"]
                state = self._states[name]
                healthy = _health_check(svc, state, self._healing_db, self._crash_history)

                if not healthy:
                    self._failure_times[name] = time.time()
                elif name in self._failure_times:
                    del self._failure_times[name]

                # Memory/CPU check if process is running
                pid = _get_service_pid(name)
                if pid:
                    _check_process_resources(name, pid, self._healing_db, self._states)

                # Execution pause check
                if name in ("alpha-execution", "prime-execution"):
                    _check_execution_paused(svc, self._healing_db)

            # --- Layer B: Detect system-wide failure modes ---
            self._check_config_hashes()
            self._check_disk()
            self._check_db_integrity_all()
            _check_cascade(self._failure_times, self._healing_db)

            # --- Layer C: Periodic healing tasks ---
            self._maybe_backup()
            self._maybe_reconcile()
            self._maybe_check_alpaca()

            # Hourly health pulse (market hours)
            if _is_market_hours() and _is_on_the_hour():
                _send_health_pulse(self._states, self._healing_db)

            # Sleep remainder of interval
            elapsed = time.time() - loop_start
            sleep_s = max(0.0, MONITOR_INTERVAL_S - elapsed)
            time.sleep(sleep_s)

        log.info("Guardian Angel stopped.")

    def stop(self) -> None:
        """Signal the daemon to stop."""
        self._running = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Start Guardian Angel daemon."""
    guardian = GuardianAngel()

    def _handle_signal(sig: int, _frame: Any) -> None:
        log.info("Received signal %d — shutting down", sig)
        guardian.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    guardian.run()


if __name__ == "__main__":
    main()
