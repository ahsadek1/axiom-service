"""
config.py — PROBE Agent Configuration

Validates all required environment variables at startup.
Crashes loudly with a full list of missing vars — never runs degraded.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Immutable settings for the PROBE agent service."""

    port: int
    nexus_secret: str
    deepseek_api_key: str
    deepseek_api_url: str
    chronicle_db_path: str
    sovereign_bus_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    db_path: str
    agent_name: str = "PROBE"
    brain_model: str = "deepseek-chat"


def load_settings() -> Settings:
    """
    Load and validate all required environment variables.
    Raises RuntimeError with full list of missing vars if any are absent.
    """
    required = {
        "NEXUS_SECRET":       os.getenv("NEXUS_SECRET"),
        "DEEPSEEK_API_KEY":   os.getenv("DEEPSEEK_API_KEY"),
        "DEEPSEEK_API_URL":   os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1"),
        "CHRONICLE_DB_PATH":  os.getenv("CHRONICLE_DB_PATH", "/Users/ahmedsadek/nexus/data/chronicle.db"),
        "SOVEREIGN_BUS_URL":  os.getenv("SOVEREIGN_BUS_URL", "http://192.168.1.141:9999"),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_CHAT_ID":   os.getenv("TELEGRAM_CHAT_ID", "8573754783"),
        "DB_PATH":            os.getenv("DB_PATH", "/Users/ahmedsadek/nexus/data/probe.db"),
    }

    secrets_required = ["NEXUS_SECRET", "DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN"]
    missing = [k for k in secrets_required if not required[k]]
    if missing:
        raise RuntimeError(
            f"PROBE: missing required environment variables: {', '.join(missing)}"
        )

    return Settings(
        port=int(os.getenv("PORT", "9010")),
        nexus_secret=required["NEXUS_SECRET"],
        deepseek_api_key=required["DEEPSEEK_API_KEY"],
        deepseek_api_url=required["DEEPSEEK_API_URL"].rstrip("/"),
        chronicle_db_path=required["CHRONICLE_DB_PATH"],
        sovereign_bus_url=required["SOVEREIGN_BUS_URL"].rstrip("/"),
        telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=required["TELEGRAM_CHAT_ID"],
        db_path=required["DB_PATH"],
    )
