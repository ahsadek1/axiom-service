"""
config.py — CONTRACT Agent Configuration
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Immutable settings for the CONTRACT agent service."""

    port: int
    nexus_secret: str
    google_api_key: str
    chronicle_db_path: str
    sovereign_bus_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    db_path: str
    agent_name: str = "CONTRACT"
    brain_model: str = "gemini-2.0-flash"


def load_settings() -> Settings:
    """Load and validate required environment variables."""
    required_strict = {
        "NEXUS_SECRET":       os.getenv("NEXUS_SECRET"),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
    }

    missing = [k for k, v in required_strict.items() if not v]
    if missing:
        raise RuntimeError(
            f"CONTRACT: missing required environment variables: {', '.join(missing)}"
        )

    return Settings(
        port=int(os.getenv("PORT", "9012")),
        nexus_secret=required_strict["NEXUS_SECRET"],
        google_api_key=os.getenv("GOOGLE_API_KEY", "PLACEHOLDER_SET_BY_AHMED"),
        chronicle_db_path=os.getenv("CHRONICLE_DB_PATH", "/Users/ahmedsadek/nexus/data/chronicle.db"),
        sovereign_bus_url=os.getenv("SOVEREIGN_BUS_URL", "http://192.168.1.141:9999").rstrip("/"),
        telegram_bot_token=required_strict["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "8573754783"),
        db_path=os.getenv("DB_PATH", "/Users/ahmedsadek/nexus/data/contract.db"),
    )
