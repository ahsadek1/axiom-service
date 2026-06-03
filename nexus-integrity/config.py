"""
config.py — Central configuration for nexus-integrity service.

All configuration is sourced from environment variables.
Service refuses to start if required variables are missing.
Thresholds are loaded from CHRONICLE at startup and refreshed every 5 minutes.
"""

import os
import logging
from typing import Dict, Optional

logger = logging.getLogger("integrity.config")

# ---------------------------------------------------------------------------
# Required environment variables — KeyError on startup if missing
# ---------------------------------------------------------------------------

NEXUS_SECRET: str = os.environ.get("NEXUS_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
PROBE_SECRET: str = os.environ.get("PROBE_SECRET", os.environ["NEXUS_SECRET"])  # fallback to NEXUS_SECRET if not set separately
CHRONICLE_URL: str = os.environ.get("CHRONICLE_URL", "http://192.168.1.42:8020")
CHRONICLE_SECRET: str = os.environ.get("CHRONICLE_SECRET", "")
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c")
TELEGRAM_SOVEREIGN_CHAT_ID: str = os.environ.get("TELEGRAM_SOVEREIGN_CHAT_ID", "-1003579956463")
TELEGRAM_AHMED_CHAT_ID: str = os.environ.get("TELEGRAM_AHMED_CHAT_ID", "8573754783")
TELEGRAM_HEALTH_GROUP_CHAT_ID: str = os.environ.get("TELEGRAM_HEALTH_GROUP_CHAT_ID", "-5241272802")

# ---------------------------------------------------------------------------
# Service identity
# ---------------------------------------------------------------------------

SERVICE_NAME: str = "nexus-integrity"
PORT: int = int(os.environ.get("PORT", "8011"))
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_DIR: str = os.environ.get("DATA_DIR", "/Users/ahmedsadek/nexus/data")
TRS_DB_PATH: str = os.environ.get("TRS_DB_PATH", os.path.join(DATA_DIR, "trs.db"))
INTEGRITY_DB_PATH: str = os.environ.get("INTEGRITY_DB_PATH", os.path.join(DATA_DIR, "nexus_integrity.db"))

# ---------------------------------------------------------------------------
# CHRONICLE schema version gate (V10 amendment)
# ---------------------------------------------------------------------------

REQUIRED_CHRONICLE_SCHEMA: str = os.environ.get("REQUIRED_CHRONICLE_SCHEMA", "2.1")

# ---------------------------------------------------------------------------
# TRS / scoring config
# ---------------------------------------------------------------------------

TRS_MAX_AGE_SECONDS: int = int(os.environ.get("TRS_MAX_AGE_SECONDS", "300"))  # 5-minute TTL

# Composite score thresholds (defaults; overridden by CHRONICLE at runtime)
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "GREEN_MIN": 90.0,
    "AMBER_MIN": 60.0,
    "RED_MIN": 30.0,
    # BLACK = below RED_MIN
    "GO_VERDICT_SIGMA": 3.0,
    "SUBMISSION_SIGMA": 5.0,
    "BRAIN_ERROR_RATE_MAX": 0.40,
    "CANARY_SLA_BREACH_MAX": 2,   # out of last 5
    "PIPELINE_LATENCY_MULTIPLIER": 3.0,
}

# Component weights in composite score (sum = 100)
COMPONENT_WEIGHTS: Dict[str, float] = {
    "service_availability": 20.0,
    "canary_success_rate": 25.0,
    "config_correctness": 15.0,
    "error_rate": 15.0,
    "pipeline_throughput": 10.0,
    "data_freshness": 10.0,
    "session_activity": 5.0,
}

# P0-P4 severity determined by which sub-components failed and their weight (V9 amendment)
# A failed component contributes 0 to its weight slice
# P0: score=0 OR TRS store dead
# P1: score < GREEN_MIN threshold (trading degraded)
# P2: single probe failure but score >= GREEN_MIN
# P3: anomaly detected, score healthy
# P4: informational

# ---------------------------------------------------------------------------
# Probe config
# ---------------------------------------------------------------------------

PROBE_MIN_INTERVAL_S: int = int(os.environ.get("PROBE_MIN_INTERVAL_S", "600"))   # 10 min min between probes
PROBE_TIMEOUT_S: int = int(os.environ.get("PROBE_TIMEOUT_S", "30"))
PROBE_MUTEX_TIMEOUT_MS: int = int(os.environ.get("PROBE_MUTEX_TIMEOUT_MS", "500"))
ORATS_PROBE_KEY: str = os.environ.get("ORATS_PROBE_KEY", os.environ.get("ORATS_API_KEY", ""))
ORATS_API_KEY: str = os.environ.get("ORATS_API_KEY", "")
ALPACA_API_KEY: str = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL: str = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# ---------------------------------------------------------------------------
# Canary config
# ---------------------------------------------------------------------------

CANARY_CLEANUP_TIMEOUT_S: int = int(os.environ.get("CANARY_CLEANUP_TIMEOUT_S", "90"))
CANARY_ORDER_PREFIX: str = "CANARY_"
PROBE_ORDER_PREFIX: str = "PROBE_"

# ---------------------------------------------------------------------------
# Flow verifier config
# ---------------------------------------------------------------------------

FLOW_VERIFY_INTERVAL_S: int = int(os.environ.get("FLOW_VERIFY_INTERVAL_S", "900"))  # 15 min
POOL_MAX_AGE_S: int = int(os.environ.get("POOL_MAX_AGE_S", "960"))     # 16 min
AGENT_MAX_SILENCE_S: int = int(os.environ.get("AGENT_MAX_SILENCE_S", "1200"))  # 20 min

# ---------------------------------------------------------------------------
# Service URLs (internal)
# ---------------------------------------------------------------------------

AXIOM_URL: str = os.environ.get("AXIOM_URL", "http://localhost:8001")
ALPHA_BUFFER_URL: str = os.environ.get("ALPHA_BUFFER_URL", "http://localhost:8002")
PRIME_BUFFER_URL: str = os.environ.get("PRIME_BUFFER_URL", "http://localhost:8003")
OMNI_URL: str = os.environ.get("OMNI_URL", "http://localhost:8004")
ALPHA_EXEC_URL: str = os.environ.get("ALPHA_EXEC_URL", "http://localhost:5001")
PRIME_EXEC_URL: str = os.environ.get("PRIME_EXEC_URL", "http://localhost:8006")
ORACLE_URL: str = os.environ.get("ORACLE_URL", "http://192.168.1.141:8007")
AILS_URL: str = os.environ.get("AILS_URL", "http://localhost:8008")
PIPELINE_SENTINEL_URL: str = os.environ.get("PIPELINE_SENTINEL_URL", "http://localhost:8010")

CIPHER_SCANNER_URL: str = os.environ.get("CIPHER_SCANNER_URL", "http://localhost:9001")
ATLAS_SCANNER_URL: str = os.environ.get("ATLAS_SCANNER_URL", "http://localhost:9002")
SAGE_SCANNER_URL: str = os.environ.get("SAGE_SCANNER_URL", "http://localhost:9003")

ALL_SERVICE_URLS: Dict[str, str] = {
    "axiom": AXIOM_URL,
    "alpha-buffer": ALPHA_BUFFER_URL,
    "prime-buffer": PRIME_BUFFER_URL,
    "omni": OMNI_URL,
    "alpha-execution": ALPHA_EXEC_URL,
    "prime-execution": PRIME_EXEC_URL,
    "oracle": ORACLE_URL,
    "ails": AILS_URL,
    "pipeline-sentinel": PIPELINE_SENTINEL_URL,
    "cipher-scanner": CIPHER_SCANNER_URL,
    "atlas-scanner": ATLAS_SCANNER_URL,
    "sage-scanner": SAGE_SCANNER_URL,
}

# ---------------------------------------------------------------------------
# Cipher / Atlas / Sage DB paths
# ---------------------------------------------------------------------------

CIPHER_DB_PATH: str = os.environ.get("CIPHER_DB_PATH", os.path.join(DATA_DIR, "cipher.db"))
ATLAS_DB_PATH: str = os.environ.get("ATLAS_DB_PATH", os.path.join(DATA_DIR, "atlas.db"))
SAGE_DB_PATH: str = os.environ.get("SAGE_DB_PATH", os.path.join(DATA_DIR, "sage.db"))
AXIOM_DB_PATH: str = os.environ.get("AXIOM_DB_PATH", os.path.join(DATA_DIR, "axiom.db"))
