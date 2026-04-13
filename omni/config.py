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
VOTES_REQUIRED_STRONG_GO  = 4   # 4/4 brains GO → STRONG_GO (full size)
VOTES_REQUIRED_GO         = 3   # 3/4 brains GO → GO (pathway size)
# 2/4 or fewer GO → NO_GO. No human escalation. No delay.
BRAIN_TIMEOUT_SECONDS     = 30  # per-brain API timeout
MIN_BRAINS_REQUIRED       = 3   # if fewer respond → CONDITIONAL (system degraded, Ahmed alerted)  # Cipher Finding 12

# P3/P4 require minimum 3/4 brains GO to execute (higher bar for solo/OMNI-initiated)
P3_P4_MIN_VOTES_GO        = 3

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
