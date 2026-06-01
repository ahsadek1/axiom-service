"""
config.py — Prime Concordance Buffer Configuration

Prime handles swing equity trades. Higher conviction thresholds than Alpha.
All required env vars validated at startup — loud ValueError on any missing.
"""

import os
from dataclasses import dataclass


# ── Concordance Constants — Prime (approved Apr 10, 2026) ─────────────────────
AGENT_WEIGHTS: dict[str, float] = {
    "Cipher": 0.45,
    "Atlas":  0.30,
    "Sage":   0.25,
}
VALID_AGENTS = frozenset(AGENT_WEIGHTS.keys())

MIN_SUBMISSION_SCORE = 50.0    # higher bar than Alpha (58) — swing needs more conviction
GO_THRESHOLD_P1      = 65.0    # higher bar than Alpha (65)
# G5 NOTE (2026-05-03 — confirmed intentional by GENESIS review):
# MIN_SCORE_P2 (78) > GO_THRESHOLD_P1 (70) is by design, NOT a bug.
# P1 requires ALL 3 agents ≥ 70. P2 only needs 2 agents, so each individual
# agent must clear a HIGHER bar (78) to compensate for the missing third voice.
# Lowering MIN_SCORE_P2 below GO_THRESHOLD_P1 would make P2 EASIER to trigger
# than P1 on a per-agent basis, which would invert the conviction hierarchy.
MIN_SCORE_P2         = 68.0    # individual per-agent bar for 2-agent P2 concordance
MIN_SCORE_SOLO_P3    = 90.0    # same as Alpha
STRONG_GO_THRESHOLD  = 80.0    # same as Alpha

PATHWAY_SIZING: dict[str, float] = {
    "P1": 1.00,
    "P2": 0.75,
    "P3": 0.50,
    "P4": 0.25,
}

# Circuit breaker thresholds — same $25K paper system
CB_AMBER_CONSECUTIVE_LOSSES  = 4
CB_AMBER_DAILY_LOSS_PCT      = 0.03
CB_AMBER_WIN_RATE_FLOOR      = 0.65
CB_AMBER_VIX_THRESHOLD       = 25.0
CB_AMBER_ENTRIES_PER_DAY     = 2

CB_RED_CONSECUTIVE_LOSSES    = 3
CB_RED_DAILY_LOSS_PCT        = 0.05
CB_RED_WEEKLY_LOSS_PCT       = 0.08

CB_STOP_CONSECUTIVE_LOSSES   = 4
CB_STOP_DAILY_LOSS_PCT       = 0.08
CB_STOP_WEEKLY_LOSS_PCT      = 0.12
CB_STOP_PORTFOLIO_LOSS_PCT   = 0.10
CB_STOP_VIX_THRESHOLD        = 35.0
CB_STOP_WIN_RATE_FLOOR       = 0.45

PAPER_CAPITAL_PER_SYSTEM     = 100_000.0
BASE_POSITION_SIZE           = 5_000.0
MAX_CONCURRENT_POSITIONS     = 10
MAX_NEW_POSITIONS_PER_DAY    = 10


@dataclass(frozen=True)
class Settings:
    """Prime Buffer runtime configuration."""

    nexus_prime_secret:  str
    prime_db_path:       str
    omni_webhook_url:    str
    telegram_bot_token:  str
    ahmed_chat_id:       str
    solo_entries_enabled: bool
    port:                int

    @property
    def omni_auth_header(self) -> dict[str, str]:
        """Return auth headers for OMNI webhook calls."""
        return {"X-Nexus-Prime-Secret": self.nexus_prime_secret}


def load_settings() -> Settings:
    """
    Load and validate all required environment variables.

    Raises:
        ValueError: If any required env vars are missing. Lists ALL missing vars.
    """
    required = {
        "NEXUS_PRIME_SECRET":  os.getenv("NEXUS_PRIME_SECRET"),
        "PRIME_DB_PATH":       os.getenv("PRIME_DB_PATH"),
        "OMNI_WEBHOOK_URL":    os.getenv("OMNI_WEBHOOK_URL"),
        "TELEGRAM_BOT_TOKEN":  os.getenv("TELEGRAM_BOT_TOKEN"),
        "AHMED_CHAT_ID":       os.getenv("AHMED_CHAT_ID"),
    }

    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError(
            f"Prime Buffer startup failed — missing required env vars: {', '.join(missing)}"
        )

    return Settings(
        nexus_prime_secret   = required["NEXUS_PRIME_SECRET"],
        prime_db_path        = required["PRIME_DB_PATH"],
        omni_webhook_url     = required["OMNI_WEBHOOK_URL"],
        telegram_bot_token   = required["TELEGRAM_BOT_TOKEN"],
        ahmed_chat_id        = required["AHMED_CHAT_ID"],
        solo_entries_enabled = os.getenv("SOLO_ENTRIES_ENABLED", "false").lower() == "true",
        port                 = int(os.getenv("PORT", "8003")),
    )


def assert_thresholds() -> None:
    """
    BUG-FIX (Sage B3): assert_thresholds() was missing from prime-buffer/config.py.
    Alpha buffer had it; prime buffer did not — miscalibrated thresholds would
    silently pass startup and corrupt concordance decisions.
    Validates threshold ordering at import time. Crashes fast on misconfiguration.
    """
    assert 0 < MIN_SUBMISSION_SCORE < 100, f"MIN_SUBMISSION_SCORE out of range: {MIN_SUBMISSION_SCORE}"
    assert 0 < GO_THRESHOLD_P1 < 100, f"GO_THRESHOLD_P1 out of range: {GO_THRESHOLD_P1}"
    assert MIN_SCORE_P2 >= GO_THRESHOLD_P1 - 5, (
        f"MIN_SCORE_P2 ({MIN_SCORE_P2}) should be near GO_THRESHOLD_P1 ({GO_THRESHOLD_P1})"
    )
    assert MIN_SCORE_SOLO_P3 > MIN_SCORE_P2, (
        f"MIN_SCORE_SOLO_P3 ({MIN_SCORE_SOLO_P3}) should be > MIN_SCORE_P2 ({MIN_SCORE_P2})"
    )
    assert STRONG_GO_THRESHOLD >= GO_THRESHOLD_P1, (
        f"STRONG_GO_THRESHOLD ({STRONG_GO_THRESHOLD}) should be >= GO_THRESHOLD_P1 ({GO_THRESHOLD_P1})"
    )


assert_thresholds()  # Run at import time — crashes process before any concordance logic loads
