"""
conftest.py — Axiom test suite
Sets all required environment variables BEFORE any test module is imported.
Single source of truth for test env — prevents collection-order race conditions.
"""

import os
import tempfile

_TEST_DB = tempfile.mktemp(suffix="_axiom_test.db")

os.environ.setdefault("AXIOM_SECRET",        "test-secret-12345")
os.environ.setdefault("POLYGON_API_KEY",     "test-polygon")
os.environ.setdefault("ALPHA_VANTAGE_KEY",   "test-av")
os.environ.setdefault("FRED_API_KEY",        "test-fred")
os.environ.setdefault("TELEGRAM_BOT_TOKEN",  "9999:TEST")
os.environ.setdefault("AHMED_CHAT_ID",       "8573754783")
os.environ.setdefault("CIPHER_WEBHOOK_URL",  "http://localhost:9001/receive-pool")
os.environ.setdefault("SAGE_WEBHOOK_URL",    "http://localhost:9002/receive-pool")
os.environ.setdefault("ATLAS_WEBHOOK_URL",   "http://localhost:9003/receive-pool")
os.environ.setdefault("ORACLE_URL",          "http://localhost:8007")
os.environ.setdefault("ORACLE_SECRET",       "test-oracle-secret")
os.environ.setdefault("AXIOM_DB_PATH",       _TEST_DB)
