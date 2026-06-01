"""
config.py — Prime Execution Engine Configuration

Prime trades swing equity — directional stock positions, not options.
Fixed dollar position sizing during paper phase. -18% hard backstop.
"""

import os
from dataclasses import dataclass

# ── Exit Rules (approved Apr 10, 2026) ───────────────────────────────────────
EXIT_PARTIAL_TRIGGER_PCT     = 0.35   # +35% → sell 50%
EXIT_PARTIAL_SELL_FRACTION   = 0.50
EXIT_TRAILING_STOP_PCT       = 0.12   # 12% trailing stop after +35%
EXIT_TRAILING_TIGHTEN_AT_PCT = 0.50   # tighten to 8% trail at +50%
EXIT_TRAILING_TIGHT_PCT      = 0.08   # tight trailing stop after +50%
EXIT_HARD_BACKSTOP_PCT       = -0.18  # -18% absolute backstop (technical stop preferred)

# ── Position Limits ───────────────────────────────────────────────────────────
MAX_CONCURRENT_POSITIONS = 15
# MAX_NEW_PER_DAY: hard ceiling on cumulative new entries per calendar day.
# Set to 10 — provides headroom for intraday high-throughput windows while
# capping runaway frequency. The concurrent cap (MAX_CONCURRENT_POSITIONS)
# is the primary safeguard; this is the secondary daily ceiling.
# History: was 5 (too tight for intraday close+reopen), raised to 50 (too loose),
# settled at 10 (right balance — approved by Ahmed 2026-04-27).
MAX_NEW_PER_DAY          = 15

ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL  = "https://data.alpaca.markets"

# ── C-11: Prime VIX Gate ─────────────────────────────────────────────────────
VIX_PAUSE_THRESHOLD_PRIME = 35.0   # matches alpha-execution — block new positions above this
AXIOM_URL_PRIME = os.getenv("AXIOM_URL", "http://localhost:8001")
AXIOM_SECRET_PRIME = os.getenv("AXIOM_SECRET", "")

# ── C-03: Separate Alpaca accounts — Alpha vs Prime ───────────────────────────
# Ahmed approved 2026-05-02. Awaiting separate paper account provisioning.
# When ready: set ALPACA_PRIME_KEY + ALPACA_PRIME_SECRET in prime-execution .env
ALPACA_PRIME_KEY    = os.getenv("ALPACA_PRIME_KEY", os.getenv("ALPACA_API_KEY", ""))
ALPACA_PRIME_SECRET = os.getenv("ALPACA_PRIME_SECRET", os.getenv("ALPACA_SECRET_KEY", ""))

# Warn at import time if same account as alpha
if ALPACA_PRIME_KEY and ALPACA_PRIME_KEY == os.getenv("ALPACA_API_KEY", ""):
    import logging as _log
    _log.getLogger("prime_exec.config").warning(
        "C-03: PRIME ALPACA ACCOUNT NOT SEPARATED — using same account as Alpha. "
        "Provision separate paper account and set ALPACA_PRIME_KEY + ALPACA_PRIME_SECRET."
    )


@dataclass(frozen=True)
class Settings:
    """Prime Execution runtime configuration."""

    nexus_prime_secret:  str
    prime_db_path:       str
    alpaca_api_key:      str
    alpaca_secret_key:   str
    prime_buffer_url:    str
    telegram_bot_token:  str
    ahmed_chat_id:       str
    port:                int


def load_settings() -> Settings:
    """
    Load and validate all required environment variables.

    Raises:
        ValueError: If any required env vars are missing.
    """
    required = {
        "NEXUS_PRIME_SECRET":   os.getenv("NEXUS_PRIME_SECRET"),
        "PRIME_EXEC_DB_PATH":   os.getenv("PRIME_EXEC_DB_PATH"),
        "ALPACA_API_KEY":       os.getenv("ALPACA_API_KEY"),
        "ALPACA_SECRET_KEY":    os.getenv("ALPACA_SECRET_KEY"),
        "PRIME_BUFFER_URL":     os.getenv("PRIME_BUFFER_URL"),
        "TELEGRAM_BOT_TOKEN":   os.getenv("TELEGRAM_BOT_TOKEN"),
        "AHMED_CHAT_ID":        os.getenv("AHMED_CHAT_ID"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError(
            f"Prime Execution startup failed — missing env vars: {', '.join(missing)}"
        )
    return Settings(
        nexus_prime_secret  = required["NEXUS_PRIME_SECRET"],
        prime_db_path       = required["PRIME_EXEC_DB_PATH"],
        alpaca_api_key      = required["ALPACA_API_KEY"],
        alpaca_secret_key   = required["ALPACA_SECRET_KEY"],
        prime_buffer_url    = required["PRIME_BUFFER_URL"],
        telegram_bot_token  = required["TELEGRAM_BOT_TOKEN"],
        ahmed_chat_id       = required["AHMED_CHAT_ID"],
        port                = int(os.getenv("PORT", "8006")),
    )
