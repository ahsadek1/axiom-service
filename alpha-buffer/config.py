"""
config.py — Alpha Concordance Buffer Configuration

Validates all required environment variables at startup.
Crashes loudly with a full list of missing vars — never runs degraded.
"""

import os
from dataclasses import dataclass, field


# ── Concordance Constants (approved Apr 10, 2026) ─────────────────────────────
AGENT_WEIGHTS: dict[str, float] = {
    "Cipher": 0.45,
    "Atlas":  0.30,
    "Sage":   0.25,
}
VALID_AGENTS          = frozenset(AGENT_WEIGHTS.keys())

MIN_SUBMISSION_SCORE  = 58.0    # below this → reject submission
GO_THRESHOLD_P1       = 65.0    # 3/3 agents, weighted score floor
MIN_SCORE_P2          = 65.0    # 2/3 agents, each must meet this.
                                 # Recalibrated 2026-04-24: was 78.0 (uncalibrated, never
                                 # validated against real agent output). Agents consistently
                                 # score 58-68 on quality setups. P2 now aligned with P1
                                 # weighted threshold (65) — two agents agreeing at 65 is
                                 # equivalent conviction to 3/3 agents weighted at 65.
MIN_SCORE_SOLO_P3     = 90.0    # solo high-conviction minimum
STRONG_GO_THRESHOLD   = 80.0    # upgrade to STRONG_GO label

# Sizing multipliers per pathway
PATHWAY_SIZING: dict[str, float] = {
    "P1": 1.00,
    "P2": 0.75,
    "P3": 0.50,
    "P4": 0.25,
}

# Circuit breaker thresholds (based on $25K paper per system)
CB_AMBER_CONSECUTIVE_LOSSES  = 2
CB_AMBER_DAILY_LOSS_PCT      = 0.03     # 3% = $750
CB_AMBER_WIN_RATE_FLOOR      = 0.65
CB_AMBER_VIX_THRESHOLD       = 25.0
CB_AMBER_ENTRIES_PER_DAY     = 2

CB_RED_CONSECUTIVE_LOSSES    = 3
CB_RED_DAILY_LOSS_PCT        = 0.05     # 5% = $1,250
CB_RED_WEEKLY_LOSS_PCT       = 0.08     # 8% = $2,000

CB_STOP_CONSECUTIVE_LOSSES   = 4
CB_STOP_DAILY_LOSS_PCT       = 0.08     # 8% = $2,000
CB_STOP_WEEKLY_LOSS_PCT      = 0.12     # 12% = $3,000
CB_STOP_PORTFOLIO_LOSS_PCT   = 0.10     # 10% = $5,000
CB_STOP_VIX_THRESHOLD        = 35.0
CB_STOP_WIN_RATE_FLOOR       = 0.45

PAPER_CAPITAL_PER_SYSTEM     = 25_000.0
BASE_POSITION_SIZE           = 2_000.0
MAX_CONCURRENT_POSITIONS     = 10
MAX_NEW_POSITIONS_PER_DAY    = 5


@dataclass(frozen=True)
class Settings:
    """Alpha Buffer runtime configuration."""

    nexus_webhook_secret:    str
    alpha_db_path:           str
    omni_webhook_url:        str
    telegram_bot_token:      str
    ahmed_chat_id:           str
    solo_entries_enabled:    bool
    port:                    int
    earnings_blocked_tickers: frozenset  # tickers blocked from concordance (e.g. earnings)

    @property
    def omni_auth_header(self) -> dict[str, str]:
        """Return auth headers for OMNI webhook calls."""
        return {"X-Nexus-Secret": self.nexus_webhook_secret}


def load_settings() -> Settings:
    """
    Load and validate all required environment variables.

    Raises:
        ValueError: If any required env vars are missing. Lists ALL missing vars.
    """
    required = {
        "NEXUS_WEBHOOK_SECRET": os.getenv("NEXUS_WEBHOOK_SECRET"),
        "ALPHA_DB_PATH":        os.getenv("ALPHA_DB_PATH"),
        "OMNI_WEBHOOK_URL":     os.getenv("OMNI_WEBHOOK_URL"),
        "TELEGRAM_BOT_TOKEN":   os.getenv("TELEGRAM_BOT_TOKEN"),
        "AHMED_CHAT_ID":        os.getenv("AHMED_CHAT_ID"),
    }

    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError(
            f"Alpha Buffer startup failed — missing required env vars: {', '.join(missing)}"
        )

    # EARNINGS_BLOCKED_TICKERS — comma-separated list of tickers to permanently
    # reject from concordance, regardless of DB state. Survives Railway restarts
    # because it's an env var. Set this to "NVDA,AVGO" when earnings are imminent.
    _blocked_raw = os.getenv("EARNINGS_BLOCKED_TICKERS", "")
    _blocked = frozenset(
        t.strip().upper() for t in _blocked_raw.split(",") if t.strip()
    )

    return Settings(
        nexus_webhook_secret     = required["NEXUS_WEBHOOK_SECRET"],
        alpha_db_path            = required["ALPHA_DB_PATH"],
        omni_webhook_url         = required["OMNI_WEBHOOK_URL"],
        telegram_bot_token       = required["TELEGRAM_BOT_TOKEN"],
        ahmed_chat_id            = required["AHMED_CHAT_ID"],
        solo_entries_enabled     = os.getenv("SOLO_ENTRIES_ENABLED", "false").lower() == "true",
        port                     = int(os.getenv("PORT", "8002")),
        earnings_blocked_tickers = _blocked,
    )


# ── Startup Sanity Check (permanent guard against threshold drift) ────────────
def assert_thresholds() -> None:
    """
    Fail loudly at startup if concordance thresholds are miscalibrated.
    Real agent scores range 58-80 in normal market conditions.
    P2 must be reachable given actual scoring ranges.
    """
    assert MIN_SCORE_P2 <= 70.0, (
        f"FATAL: MIN_SCORE_P2={MIN_SCORE_P2} is too high — agents score 58-68 on quality setups. "
        f"P2 would be permanently unreachable. Set ≤ 70."
    )
    assert GO_THRESHOLD_P1 <= 70.0, (
        f"FATAL: GO_THRESHOLD_P1={GO_THRESHOLD_P1} is too high — would block all P1 concordances."
    )
    assert MIN_SCORE_P2 >= MIN_SUBMISSION_SCORE, (
        f"FATAL: MIN_SCORE_P2={MIN_SCORE_P2} < MIN_SUBMISSION_SCORE={MIN_SUBMISSION_SCORE} — "
        f"contradicts submission gate."
    )
    assert MIN_SCORE_SOLO_P3 > 80.0, (
        f"FATAL: MIN_SCORE_SOLO_P3={MIN_SCORE_SOLO_P3} too low — solo entries would fire too often."
    )


assert_thresholds()  # Run at import time — crashes process before any trade logic loads
