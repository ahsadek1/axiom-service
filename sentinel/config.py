"""
config.py — Pipeline Sentinel Configuration

All constants, thresholds, and service maps. Secrets read from env only.
"""

import os
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ── Timing ─────────────────────────────────────────────────────────────────────
CHECK_INTERVAL_SECONDS    = 300        # 5 minutes
MARKET_OPEN_TIME          = (9, 30)    # HH, MM ET
MARKET_CLOSE_TIME         = (16, 15)   # HH, MM ET
PRE_MARKET_CHECK_TIME     = (9, 10)    # service-only check before open
HEARTBEAT_PATH            = "/tmp/nexus_sentinel_heartbeat"
HEARTBEAT_STALE_THRESHOLD = 600        # 10 minutes — if exceeded, sentinel was down

# ── Escalation Thresholds ──────────────────────────────────────────────────────
TIER2_WAIT_SECONDS        = 300        # 5 min after Tier 1 before escalating
TIER3_WAIT_SECONDS        = 180        # 3 min after Tier 2 before OMNI+Cipher
HEAL_RECHECK_SECONDS      = 20         # wait after restart before rechecking
OMNI_RESTART_WAIT_SECONDS = 30         # OMNI needs longer to initialize
PICK_LOOKBACK_MINUTES     = 30         # agent must have picked in last N min
OMNI_DISPATCH_LOOKBACK_M  = 60         # OMNI must have dispatched in last N min (market hours)

# ── Service → Port Map ─────────────────────────────────────────────────────────
SERVICE_PORTS = {
    "axiom":           8001,
    "alpha-buffer":    8002,
    "prime-buffer":    8003,
    "omni":            8004,
    "alpha-execution": 8005,
    "prime-execution": 8006,
    "oracle":          8007,
    "ails":            8008,
    "guardian-angel":  8009,
    "cipher":          9001,
    "atlas":           9002,
    "sage":            9003,
}

# ── Service → LaunchD Plist Name ───────────────────────────────────────────────
SERVICE_PLIST = {
    "axiom":           "ai.nexus.axiom",
    "alpha-buffer":    "ai.nexus.alpha-buffer",
    "prime-buffer":    "ai.nexus.prime-buffer",
    "omni":            "ai.nexus.omni",
    "alpha-execution": "ai.nexus.alpha-execution",
    "prime-execution": "ai.nexus.prime-execution",
    "oracle":          "ai.nexus.oracle",
    "ails":            "ai.nexus.ails",
    "cipher":          "ai.nexus.cipher",
    "atlas":           "ai.nexus.atlas",
    "sage":            "ai.nexus.sage",
}

# ── Agent DB Paths ─────────────────────────────────────────────────────────────
AGENT_DB_PATHS = {
    "cipher": "/Users/ahmedsadek/nexus/data/cipher.db",
    "atlas":  "/Users/ahmedsadek/nexus/data/atlas.db",
    "sage":   "/Users/ahmedsadek/nexus/data/sage.db",
}

# ── Agent Log Paths ────────────────────────────────────────────────────────────
AGENT_LOG_PATHS = {
    "cipher": "/Users/ahmedsadek/nexus/logs/cipher/stderr.log",
    "atlas":  "/Users/ahmedsadek/nexus/logs/atlas/stderr.log",
    "sage":   "/Users/ahmedsadek/nexus/logs/sage/stderr.log",
}

# ── Secrets (env only — never hardcoded) ──────────────────────────────────────
NEXUS_SECRET        = os.environ["NEXUS_SECRET"]
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Service URLs ───────────────────────────────────────────────────────────────
OMNI_URL        = os.environ.get("OMNI_URL",       "http://localhost:8004")
ALPHA_EXEC_URL  = os.environ.get("ALPHA_EXEC_URL", "http://localhost:8005")
PRIME_EXEC_URL  = os.environ.get("PRIME_EXEC_URL", "http://localhost:8006")
SENTINEL_LOG    = "/Users/ahmedsadek/nexus/logs/sentinel/sentinel.log"

# ── Operational Mode ─────────────────────────────────────────────────────────────
OBSERVE_ONLY    = False   # When True: checks run but healer skips restarts
