"""
config.py — OMNI Service Configuration

OMNI receives concordance from both Alpha and Prime buffers.
Runs Quad Intelligence synthesis (Claude + DeepSeek + Gemini + o3-mini).
Routes GO verdicts to Alpha or Prime execution engines.
"""

import os
from dataclasses import dataclass, field

# ── Voting Thresholds ─────────────────────────────────────────────────────────
# Fully autonomous — no CONDITIONAL verdict. System executes or it doesn't.
# Ahmed directive 2026-05-07: 2/4 GO → GO (pathway size), 3+/4 GO → STRONG_GO (full size)
VOTES_REQUIRED_STRONG_GO  = 3   # 3+ brains GO → STRONG_GO (full size)
VOTES_REQUIRED_GO         = 2   # 2/4 brains GO → GO (pathway size)
# 2/4 or fewer GO → NO_GO. No human escalation. No delay.
# FIX-CLAUDE-TIMEOUT (2026-06-01 10:31 ET): Reduced brain timeout from 120s to 45s
# Root cause: Claude API was hitting 120s+ read timeouts under load, blocking quad synthesis pool.
# Impact: 8 execution failures in 60 min (all "unconfirmed: no confirmation after 30s").
# Fix: 45s timeout per brain + allow synthesis with 2/4 brains responding (graceful degradation).
# FIX 2026-06-02 14:30 ET: Increased timeout to 60s due to API degradation across Anthropic + Gemini.
# Recent synthesis errors: 11 timeout/connection errors in 30 min.
# Action: Give APIs 60s instead of 45s + add 1 retry for transient failures.
BRAIN_TIMEOUT_SECONDS     = 30   # CANARY FIX 2026-06-04 09:06: lowered from 60s to prevent worker deadlock. Anthropic/Gemini timeout @30s + 5s buffer = 35s per brain max.
BRAIN_RETRY_COUNT         = 1    # 1 retry on timeout/transient error (changed from 0)
MIN_BRAINS_REQUIRED       = 2    # 2+ brains required for synthesis (was enforcing all 4). Enables cascade resilience.

# P3/P4 require minimum 3/4 brains GO to execute (higher bar for solo/OMNI-initiated)
P3_P4_MIN_VOTES_GO        = 2

# ── Brain Names ───────────────────────────────────────────────────────────────
BRAIN_CLAUDE    = "claude"
BRAIN_O3MINI    = "o3mini"
BRAIN_GEMINI    = "gemini"
BRAIN_DEEPSEEK  = "deepseek"

ALL_BRAINS = [BRAIN_CLAUDE, BRAIN_O3MINI, BRAIN_GEMINI, BRAIN_DEEPSEEK]

# ── Position Sizing ───────────────────────────────────────────────────────────
BASE_POSITION_SIZE = 2_000.0   # base dollar size per trade (paper: $25K system)

# Brain primary dimensions (used in synthesis card)
BRAIN_DIMENSIONS = {
    BRAIN_CLAUDE:   "synthesis",
    BRAIN_O3MINI:   "adversarial",
    BRAIN_GEMINI:   "pattern",
    BRAIN_DEEPSEEK: "momentum",
}


@dataclass(frozen=True)
class Settings:
    """OMNI runtime configuration."""

    # Auth
    nexus_secret:          str   # Alpha system secret (X-Nexus-Secret)
    nexus_prime_secret:    str   # Prime system secret (X-Nexus-Prime-Secret) — Pass B fix #1
    omni_secret:           str   # OMNI's own secret for downstream calls

    # AI Brain API Keys
    anthropic_api_key:     str
    openai_api_key:        str
    gemini_api_key:        str
    deepseek_api_key:      str

    # Service URLs
    axiom_url:             str
    axiom_secret:          str
    alpha_execution_url:   str
    prime_execution_url:   str

    # Telegram
    telegram_bot_token:    str
    ahmed_chat_id:         str

    # Database
    omni_db_path:          str
    port:                  int


def load_settings() -> Settings:
    """
    Load and validate all required environment variables.

    Raises:
        ValueError: If any required env vars are missing. Lists ALL missing vars.
    """
    required = {
        "NEXUS_WEBHOOK_SECRET":  os.getenv("NEXUS_WEBHOOK_SECRET"),
        "NEXUS_PRIME_SECRET":    os.getenv("NEXUS_PRIME_SECRET"),   # Pass B fix #1
        "OMNI_SECRET":           os.getenv("OMNI_SECRET"),
        "ANTHROPIC_API_KEY":     os.getenv("ANTHROPIC_API_KEY"),
        "OPENAI_API_KEY":        os.getenv("OPENAI_API_KEY"),
        "GEMINI_API_KEY":        os.getenv("GEMINI_API_KEY"),
        "DEEPSEEK_API_KEY":      os.getenv("DEEPSEEK_API_KEY"),
        "AXIOM_URL":             os.getenv("AXIOM_URL"),
        "AXIOM_SECRET":          os.getenv("AXIOM_SECRET"),
        "ALPHA_EXECUTION_URL":   os.getenv("ALPHA_EXECUTION_URL"),
        "PRIME_EXECUTION_URL":   os.getenv("PRIME_EXECUTION_URL"),
        "TELEGRAM_BOT_TOKEN":    os.getenv("TELEGRAM_BOT_TOKEN"),
        "AHMED_CHAT_ID":         os.getenv("AHMED_CHAT_ID"),
        "OMNI_DB_PATH":          os.getenv("OMNI_DB_PATH"),
    }

    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError(
            f"OMNI startup failed — missing required env vars: {', '.join(missing)}"
        )

    return Settings(
        nexus_secret         = required["NEXUS_WEBHOOK_SECRET"],
        nexus_prime_secret   = required["NEXUS_PRIME_SECRET"],    # Pass B fix #1
        omni_secret          = required["OMNI_SECRET"],
        anthropic_api_key    = required["ANTHROPIC_API_KEY"],
        openai_api_key       = required["OPENAI_API_KEY"],
        gemini_api_key       = required["GEMINI_API_KEY"],
        deepseek_api_key     = required["DEEPSEEK_API_KEY"],
        axiom_url            = required["AXIOM_URL"],
        axiom_secret         = required["AXIOM_SECRET"],
        alpha_execution_url  = required["ALPHA_EXECUTION_URL"],
        prime_execution_url  = required["PRIME_EXECUTION_URL"],
        telegram_bot_token   = required["TELEGRAM_BOT_TOKEN"],
        ahmed_chat_id        = required["AHMED_CHAT_ID"],
        omni_db_path         = required["OMNI_DB_PATH"],
        port                 = int(os.getenv("PORT", "8004")),
    )
