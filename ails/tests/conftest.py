"""conftest.py — Set all required env vars before any test module is imported."""
import os
os.environ.setdefault("AILS_SECRET", "test-ails-secret-12345")
os.environ.setdefault("NEXUS_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
os.environ.setdefault("POLYGON_API_KEY", "7LkpjSEBTFsr3HCodQNppdphNU0Qmr7f")
os.environ.setdefault("FRED_API_KEY", "5749ecf7ebd18e7f77e30ef2357f55b7")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "7973500599:AAGoTNnJgF0Muvok_FSXeTPHeAUaOKMahk0")
os.environ.setdefault("TELEGRAM_AHMED_CHAT_ID", "8573754783")
os.environ.setdefault("DATA_DIR", "/tmp/ails_test")
os.environ.setdefault("AILS_DB_PATH", "/tmp/ails_test/ails.db")
os.environ.setdefault("BACKTEST_DB_PATH", "/tmp/ails_test/backtest.db")
os.environ.setdefault("ORACLE_URL", "http://localhost:8007")
os.environ.setdefault("ORACLE_SECRET", "test")
os.environ.setdefault("LIVE_OUTCOME_WEIGHT", "3")
os.environ.setdefault("MIN_LIVE_OUTCOMES_TO_SHIFT", "10")
os.environ.setdefault("MIN_BACKTEST_SAMPLES", "5")
os.environ.setdefault("HOST", "0.0.0.0")
os.environ.setdefault("PORT", "8008")
os.environ.setdefault("DRIFT_ALERT_PCT", "0.15")

import pathlib
pathlib.Path("/tmp/ails_test").mkdir(parents=True, exist_ok=True)
