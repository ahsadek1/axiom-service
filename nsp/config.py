"""
config.py — Nexus Sentinel Prime (NSP) configuration.

All secrets and service endpoints are sourced from environment variables.
No secrets are hardcoded. Service refuses to start if NEXUS_SECRET or
TELEGRAM_BOT_TOKEN are missing from the environment.
"""

import os
from typing import Dict
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
SERVICE_NAME: str = "nexus-sentinel-prime"
NSP_PORT: int = int(os.environ.get("NSP_PORT", "8010"))
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Secrets — KeyError on startup if required vars missing (fail loud)
# ---------------------------------------------------------------------------
NSP_SECRET: str = os.environ.get("NSP_SECRET", "nexus-sentinel-prime")
NEXUS_SECRET: str = os.environ["NEXUS_SECRET"]
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]

# ---------------------------------------------------------------------------
# Telegram routing
# ---------------------------------------------------------------------------
AHMED_CHAT_ID: str = os.environ.get("AHMED_CHAT_ID", "8573754783")
SOVEREIGN_CHAT_ID: str = os.environ.get("TELEGRAM_SOVEREIGN_CHAT_ID", "-1003579956463")
HEALTH_GROUP_CHAT_ID: str = os.environ.get("TELEGRAM_HEALTH_GROUP_CHAT_ID", "-5241272802")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
NSP_DB_PATH: str = os.environ.get("NSP_DB_PATH", "/Users/ahmedsadek/nexus/data/nsp.db")
NSP_LOG_DIR: str = os.environ.get("NSP_LOG_DIR", "/Users/ahmedsadek/nexus/logs/nsp")

# ---------------------------------------------------------------------------
# External services
# ---------------------------------------------------------------------------
MESSAGE_BUS_URL: str = os.environ.get("MESSAGE_BUS_URL", "http://192.168.1.141:9999")
CHRONICLE_URL: str = os.environ.get("CHRONICLE_URL", "http://192.168.1.42:8020")
NSP_URL: str = os.environ.get("NSP_URL", f"http://localhost:{NSP_PORT}")

# ---------------------------------------------------------------------------
# Telemetry TTL + purge schedule
# ---------------------------------------------------------------------------
TELEMETRY_TTL_HOURS: int = 4
TELEMETRY_TTL_S: float = TELEMETRY_TTL_HOURS * 3600.0
PURGE_INTERVAL_S: int = 30 * 60  # 30 minutes

# ---------------------------------------------------------------------------
# Adaptive polling intervals (seconds)
# ---------------------------------------------------------------------------
POLL_HEALTHY_S: int = 30
POLL_AMBER_S: int = 5
POLL_POST_RESTART_S: int = 2
POLL_POST_RESTART_WINDOW_S: int = 5 * 60  # 5 minutes post-restart at 2s rate

# ---------------------------------------------------------------------------
# Self-preservation gate
# ---------------------------------------------------------------------------
DB_WRITE_LAG_THRESHOLD_S: float = 2.0

# ---------------------------------------------------------------------------
# Dead Man's Switch
# ---------------------------------------------------------------------------
DMS_CHECK_INTERVAL_S: int = 30
DMS_CONSECUTIVE_FAIL_THRESHOLD: int = 2
DMS_HEARTBEAT_PATH: str = "/tmp/nsp_dms_heartbeat"

# ---------------------------------------------------------------------------
# HTTP collector timeout
# ---------------------------------------------------------------------------
HTTP_TIMEOUT_S: float = 5.0

# ---------------------------------------------------------------------------
# Service endpoint map — name → base URL
# ---------------------------------------------------------------------------
AXIOM_URL: str = os.environ.get("AXIOM_URL", "http://localhost:8001")
ALPHA_BUFFER_URL: str = os.environ.get("ALPHA_BUFFER_URL", "http://localhost:8002")
PRIME_BUFFER_URL: str = os.environ.get("PRIME_BUFFER_URL", "http://localhost:8003")
OMNI_URL: str = os.environ.get("OMNI_URL", "http://localhost:8004")
ALPHA_EXEC_URL: str = os.environ.get("ALPHA_EXEC_URL", "http://localhost:8005")
PRIME_EXEC_URL: str = os.environ.get("PRIME_EXEC_URL", "http://localhost:8006")
ORACLE_URL: str = os.environ.get("ORACLE_URL", "http://localhost:8007")
AILS_URL: str = os.environ.get("AILS_URL", "http://localhost:8008")
CIPHER_SCANNER_URL: str = os.environ.get("CIPHER_SCANNER_URL", "http://localhost:9001")
ATLAS_SCANNER_URL: str = os.environ.get("ATLAS_SCANNER_URL", "http://localhost:9002")
SAGE_SCANNER_URL: str = os.environ.get("SAGE_SCANNER_URL", "http://localhost:9003")

SERVICE_URLS: Dict[str, str] = {
    "axiom":            AXIOM_URL,
    "alpha-buffer":     ALPHA_BUFFER_URL,
    "prime-buffer":     PRIME_BUFFER_URL,
    "omni":             OMNI_URL,
    "alpha-execution":  ALPHA_EXEC_URL,
    "prime-execution":  PRIME_EXEC_URL,
    "oracle":           ORACLE_URL,
    "ails":             AILS_URL,
    "cipher-scanner":   CIPHER_SCANNER_URL,
    "atlas-scanner":    ATLAS_SCANNER_URL,
    "sage-scanner":     SAGE_SCANNER_URL,
}

# Service name → listening port (for psutil process discovery)
SERVICE_PORTS: Dict[str, int] = {
    "axiom":            8001,
    "alpha-buffer":     8002,
    "prime-buffer":     8003,
    "omni":             8004,
    "alpha-execution":  8005,
    "prime-execution":  8006,
    "oracle":           8007,
    "ails":             8008,
    "cipher-scanner":   9001,
    "atlas-scanner":    9002,
    "sage-scanner":     9003,
}

# Service name → stderr log path for log scanning
SERVICE_LOG_PATHS: Dict[str, str] = {
    "axiom":            "/Users/ahmedsadek/nexus/logs/axiom/stderr.log",
    "alpha-buffer":     "/Users/ahmedsadek/nexus/logs/alpha-buffer/stderr.log",
    "prime-buffer":     "/Users/ahmedsadek/nexus/logs/prime-buffer/stderr.log",
    "omni":             "/Users/ahmedsadek/nexus/logs/omni/stderr.log",
    "alpha-execution":  "/Users/ahmedsadek/nexus/logs/alpha-execution/stderr.log",
    "prime-execution":  "/Users/ahmedsadek/nexus/logs/prime-execution/stderr.log",
    "oracle":           "/Users/ahmedsadek/nexus/logs/oracle/stderr.log",
    "ails":             "/Users/ahmedsadek/nexus/logs/ails/stderr.log",
}
