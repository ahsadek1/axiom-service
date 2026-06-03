"""
config.py — Atlas Agent Configuration

Validates all required environment variables at startup.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Immutable settings for the Atlas agent service."""

    port: int
    nexus_secret: str
    nexus_prime_secret: str
    oracle_secret: str
    oracle_url: str
    alpha_buffer_url: str
    prime_buffer_url: str
    gemini_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    db_path: str

    def alpha_headers(self) -> dict:
        return {"X-Nexus-Secret": self.nexus_secret}

    def prime_headers(self) -> dict:
        return {"X-Nexus-Prime-Secret": self.nexus_prime_secret}

    def oracle_headers(self) -> dict:
        return {"X-Oracle-Secret": self.oracle_secret}


def load_settings() -> Settings:
    required = {
        "NEXUS_SECRET":       os.getenv("NEXUS_SECRET"),
        "NEXUS_PRIME_SECRET": os.getenv("NEXUS_PRIME_SECRET"),
        "ORACLE_SECRET":      os.getenv("ORACLE_SECRET"),
        "ORACLE_URL":         os.getenv("ORACLE_URL", "http://localhost:8007"),
        "ALPHA_BUFFER_URL":   os.getenv("ALPHA_BUFFER_URL", "http://localhost:8002"),
        "PRIME_BUFFER_URL":   os.getenv("PRIME_BUFFER_URL", "http://localhost:8003"),
        "GEMINI_API_KEY":     os.getenv("GEMINI_API_KEY"),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_CHAT_ID":   os.getenv("TELEGRAM_CHAT_ID", "8573754783"),
        "DB_PATH":            os.getenv("DB_PATH", "/Users/ahmedsadek/nexus/data/atlas.db"),
    }

    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(f"Atlas: missing required environment variables: {', '.join(missing)}")

    return Settings(
        port=int(os.getenv("PORT", "9002")),
        nexus_secret=required["NEXUS_SECRET"],
        nexus_prime_secret=required["NEXUS_PRIME_SECRET"],
        oracle_secret=required["ORACLE_SECRET"],
        oracle_url=required["ORACLE_URL"].rstrip("/"),
        alpha_buffer_url=required["ALPHA_BUFFER_URL"].rstrip("/"),
        prime_buffer_url=required["PRIME_BUFFER_URL"].rstrip("/"),
        gemini_api_key=required["GEMINI_API_KEY"],
        telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=required["TELEGRAM_CHAT_ID"],
        db_path=required["DB_PATH"],
    )
