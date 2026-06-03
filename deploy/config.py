"""
config.py — DEPLOY Agent Configuration
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Immutable settings for the DEPLOY agent service."""

    port: int
    nexus_secret: str
    deepseek_api_key: str
    deepseek_api_url: str
    chronicle_db_path: str
    sovereign_bus_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    db_path: str
    agent_name: str = "DEPLOY"
    brain_model: str = "deepseek-chat"


def load_settings() -> Settings:
    """Load and validate required environment variables."""
    required = {
        "NEXUS_SECRET":       os.getenv("NEXUS_SECRET"),
        "DEEPSEEK_API_KEY":   os.getenv("DEEPSEEK_API_KEY"),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
    }

    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(
            f"DEPLOY: missing required environment variables: {', '.join(missing)}"
        )

    return Settings(
        port=int(os.getenv("PORT", "9013")),
        nexus_secret=required["NEXUS_SECRET"],
        deepseek_api_key=required["DEEPSEEK_API_KEY"],
        deepseek_api_url=os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1").rstrip("/"),
        chronicle_db_path=os.getenv("CHRONICLE_DB_PATH", "/Users/ahmedsadek/nexus/data/chronicle.db"),
        sovereign_bus_url=os.getenv("SOVEREIGN_BUS_URL", "http://192.168.1.141:9999").rstrip("/"),
        telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "8573754783"),
        db_path=os.getenv("DB_PATH", "/Users/ahmedsadek/nexus/data/deploy.db"),
    )
