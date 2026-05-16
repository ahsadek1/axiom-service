"""
config.py — Alpha Execution Engine Configuration

Alpha trades options: bull put credit spreads (bullish) and bear call credit spreads (bearish).
40 DTE target. ETF strikes rounded to $5, stocks to $1.
"""

import os
from dataclasses import dataclass

# ── ETF universe — strikes rounded to $5 ─────────────────────────────────────
ETF_TICKERS = frozenset({
    "SPY", "QQQ", "IWM", "XLK", "XLF", "XLV", "XLE",
    "TLT", "GLD", "SOXX", "DIA", "VXX",
})

# ── Options Parameters ────────────────────────────────────────────────────────
TARGET_DTE            = 40     # days to expiration target
MIN_DTE               = 30     # minimum DTE on open (won't open below this)
DTE_ROLL_THRESHOLD    = 21     # roll at this many DTE
DTE_CLOSE_THRESHOLD   = 7      # close at this many DTE
DTE_EMERGENCY_CLOSE   = 5      # emergency close at this many DTE

# ── Exit Rules (approved Apr 10, 2026) ───────────────────────────────────────
EXIT_PARTIAL_TRIGGER_PCT    = 0.35   # +35% → sell 50%
EXIT_PARTIAL_SELL_FRACTION  = 0.50   # sell this fraction at trigger
EXIT_TRAILING_STOP_PCT      = 0.15   # 15% trailing stop after partial exit
EXIT_FULL_CLOSE_PCT         = 1.00   # +100% → close everything immediately

# ── Earnings Block ───────────────────────────────────────────────────────────
# Block trades when earnings fall within this many days of expiry.
EARNINGS_BLOCK_DAYS = 5

# ── VIX Brakes ───────────────────────────────────────────────────────────────
VIX_BRAKE_ELEVATED = 25   # above this: reduce sizing
VIX_BRAKE_FULL     = 35   # above this: block all new positions

# ── Position Limits ───────────────────────────────────────────────────────────
# MUST match axiom/config.py MAX_POSITIONS — single source of truth is axiom.
# C-2 fix (2026-05-02): was 10, corrected to 3 to match Ahmed's mandate and Axiom gate.
MAX_CONCURRENT_POSITIONS = 3  # Alpha + Prime combined (per MEMORY.md + Axiom) — FIXED 2026-05-16
MAX_NEW_PER_DAY          = 3

# ── Strike Calculation ────────────────────────────────────────────────────────
# Bull put credit spread: sell ATM-5%, buy ATM-10%
# Bear call credit spread: sell ATM+5%, buy ATM+10%
SPREAD_WIDTH_PCT = 0.05    # 5% OTM for short strike
WING_WIDTH_PCT   = 0.05    # additional 5% for long strike (wing)

ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL  = "https://data.alpaca.markets"


@dataclass(frozen=True)
class Settings:
    """Alpha Execution runtime configuration."""

    nexus_webhook_secret:  str
    alpha_db_path:         str
    alpaca_api_key:        str
    alpaca_secret_key:     str
    alpaca_url:            str
    alpha_buffer_url:      str
    telegram_bot_token:    str
    ahmed_chat_id:         str
    port:                  int
    axiom_url:             str
    axiom_secret:          str
    polygon_api_key:       str


def load_settings() -> Settings:
    """
    Load and validate all required environment variables.

    Raises:
        ValueError: If any required env vars are missing.
    """
    required = {
        "NEXUS_WEBHOOK_SECRET": os.getenv("NEXUS_WEBHOOK_SECRET"),
        "ALPHA_EXEC_DB_PATH":   os.getenv("ALPHA_EXEC_DB_PATH"),
        "ALPACA_API_KEY":       os.getenv("ALPACA_API_KEY"),
        "ALPACA_SECRET_KEY":    os.getenv("ALPACA_SECRET_KEY"),
        "ALPHA_BUFFER_URL":     os.getenv("ALPHA_BUFFER_URL"),
        "TELEGRAM_BOT_TOKEN":   os.getenv("TELEGRAM_BOT_TOKEN"),
        "AHMED_CHAT_ID":        os.getenv("AHMED_CHAT_ID"),
        "AXIOM_URL":            os.getenv("AXIOM_URL"),
        "AXIOM_SECRET":         os.getenv("AXIOM_SECRET"),
    }
    # Polygon key is optional — earnings gate skips gracefully if absent
    polygon_key = os.getenv("POLYGON_API_KEY", "")
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError(
            f"Alpha Execution startup failed — missing env vars: {', '.join(missing)}"
        )
    return Settings(
        nexus_webhook_secret = required["NEXUS_WEBHOOK_SECRET"],
        alpha_db_path        = required["ALPHA_EXEC_DB_PATH"],
        alpaca_api_key       = required["ALPACA_API_KEY"],
        alpaca_url           = os.getenv("ALPACA_URL", "https://paper-api.alpaca.markets"),
        alpaca_secret_key    = required["ALPACA_SECRET_KEY"],
        alpha_buffer_url     = required["ALPHA_BUFFER_URL"],
        telegram_bot_token   = required["TELEGRAM_BOT_TOKEN"],
        ahmed_chat_id        = required["AHMED_CHAT_ID"],
        port                 = int(os.getenv("PORT", "8005")),
        axiom_url            = required["AXIOM_URL"],
        axiom_secret         = required["AXIOM_SECRET"],
        polygon_api_key      = polygon_key,
    )
