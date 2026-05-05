"""config.py — Outcome Monitor constants and configuration."""

# Service health endpoints (all localhost)
SERVICE_URLS: dict = {
    "alpha_buffer": "http://127.0.0.1:8002/health",
    "prime_buffer": "http://127.0.0.1:8003/health",
    "omni": "http://127.0.0.1:8004/health",
    "alpha_execution": "http://127.0.0.1:8005/health",
    "prime_execution": "http://127.0.0.1:8006/health",
    "cipher": "http://127.0.0.1:9001/health",
    "atlas": "http://127.0.0.1:9002/health",
    "sage": "http://127.0.0.1:9003/health",
}

# Alpaca paper trading accounts
ALPACA_V1_KEY = "PKPGM3BRNYPGCF5Z56IAUZCZJL"
ALPACA_V1_SECRET = "5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs"
ALPACA_V2_KEY = "PKGGXWNZTITUTZUNVK2QBLZJQL"
ALPACA_V2_SECRET = "DWwCxfRpv92ZLkGX8Ao4mDsAATafLwcuw8EeFAM7s89i"
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

# CHRONICLE write endpoint
CHRONICLE_URL = "http://192.168.1.42:8020/chronicle/adaptive"
CHRONICLE_AUTH = "b18f4b96b8e6a1111711dbffffe5402910180f36e09007c939644608079a1856"

# Nexus message bus (SOVEREIGN escalation fallback)
NEXUS_BUS_URL = "http://192.168.1.141:9999"

# State and fallback log paths
STATE_FILE = "/tmp/outcome_monitor_state.json"
FALLBACK_LOG = "/tmp/outcome_monitor_fallback.jsonl"

# Diagnosis thresholds
CAPITAL_FLOOR = 2000.0          # Min buying_power for either account to be considered active
SCANNER_STALE_MINUTES = 35      # Minutes before a scanner is considered stale
CONSECUTIVE_DRY_ESCALATE = 3   # Consecutive dry cycles before scanner_dry escalates (WARN)

# HTTP settings
HTTP_TIMEOUT = 8  # Seconds per service request
