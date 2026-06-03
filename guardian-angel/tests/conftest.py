"""
conftest.py — Set all required env vars before any test module is imported.
This prevents collection-order race conditions with module-level env var loading.
"""
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "7973500599:AAFakeTokenForTesting")
os.environ.setdefault("TELEGRAM_AHMED_CHAT_ID", "8573754783")
os.environ.setdefault("ALPACA_API_KEY", "TEST_KEY")
os.environ.setdefault("ALPACA_API_SECRET", "TEST_SECRET")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("NEXUS_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
os.environ.setdefault("NEXUS_ROOT", "/tmp/nexus_test")
os.environ.setdefault("DATA_DIR", "/tmp/nexus_test/data")
os.environ.setdefault("LOGS_DIR", "/tmp/nexus_test/logs")
os.environ.setdefault("BACKUP_DIR", "/tmp/nexus_test/data/backups")
os.environ.setdefault("HEALING_DB_PATH", "/tmp/nexus_test/data/healing.db")
os.environ.setdefault("MONITOR_INTERVAL_S", "60")

import pathlib
pathlib.Path("/tmp/nexus_test/data/backups").mkdir(parents=True, exist_ok=True)
pathlib.Path("/tmp/nexus_test/logs/guardian-angel").mkdir(parents=True, exist_ok=True)
