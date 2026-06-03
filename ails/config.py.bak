"""
config.py — AILS configuration
All env vars required at startup. No silent defaults for secrets.
"""

import os
from typing import Dict


def _require(key: str) -> str:
    """Load required env var; raise loudly if missing."""
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Required env var '{key}' is not set. Check .env file.")
    return val


# Auth
AILS_SECRET: str = _require("AILS_SECRET")
NEXUS_SECRET: str = _require("NEXUS_SECRET")

# External APIs
POLYGON_API_KEY: str = _require("POLYGON_API_KEY")
FRED_API_KEY: str = _require("FRED_API_KEY")
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_AHMED_CHAT_ID: str = _require("TELEGRAM_AHMED_CHAT_ID")

# Paths
DATA_DIR: str = os.environ.get("DATA_DIR", "/Users/ahmedsadek/nexus/data")
AILS_DB_PATH: str = os.environ.get("AILS_DB_PATH", f"{DATA_DIR}/ails.db")
BACKTEST_DB_PATH: str = os.environ.get("BACKTEST_DB_PATH", f"{DATA_DIR}/backtest.db")

# Service URLs (inter-service communication)
ORACLE_URL: str = os.environ.get("ORACLE_URL", "http://localhost:8007")
ORACLE_SECRET: str = os.environ.get("ORACLE_SECRET", "")

# Bayesian weighting
LIVE_OUTCOME_WEIGHT: int = int(os.environ.get("LIVE_OUTCOME_WEIGHT", "3"))
MIN_LIVE_OUTCOMES_TO_SHIFT: int = int(os.environ.get("MIN_LIVE_OUTCOMES_TO_SHIFT", "10"))
MIN_BACKTEST_SAMPLES: int = int(os.environ.get("MIN_BACKTEST_SAMPLES", "5"))

# Report timing
EOD_HOUR_ET: int = int(os.environ.get("EOD_HOUR_ET", "16"))
EOD_MINUTE_ET: int = int(os.environ.get("EOD_MINUTE_ET", "30"))

# Regime thresholds (VIX buckets)
REGIME_THRESHOLDS: Dict[str, float] = {
    "LOW_VOL":     12.0,   # VIX ≤ 12  → very calm
    "NORMAL":      20.0,   # VIX ≤ 20  → typical market
    "ELEVATED":    30.0,   # VIX ≤ 30  → elevated concern
    "STRESS":      40.0,   # VIX ≤ 40  → market stress
    "HIGH_STRESS": 55.0,   # VIX ≤ 55  → severe stress
    # Above HIGH_STRESS → CRISIS
}

# Parameter drift alert threshold
DRIFT_ALERT_PCT: float = float(os.environ.get("DRIFT_ALERT_PCT", "0.15"))

# Server
HOST: str = os.environ.get("HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PORT", "8008"))
