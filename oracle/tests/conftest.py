"""
conftest.py — ORACLE test suite
Sets all required environment variables BEFORE any test module is imported.
This runs first during pytest collection, ensuring a consistent env state
regardless of which test file is collected first.
"""

import os
import tempfile

_TEST_DB = tempfile.mktemp(suffix="_oracle_test.db")

os.environ.setdefault("ORACLE_SECRET", "test-secret-123")
os.environ.setdefault("ORACLE_DB_PATH", _TEST_DB)
os.environ.setdefault("POLYGON_API_KEY", "test-polygon")
os.environ.setdefault("ORATS_API_KEY", "test-orats")
os.environ.setdefault("MARKET_CHAMELEON_KEY", "test-mc")
os.environ.setdefault("UNUSUAL_WHALES_KEY", "test-uw")
os.environ.setdefault("SPOTGAMMA_KEY", "test-sg")
os.environ.setdefault("TRADING_ECONOMICS_KEY", "test-te")
os.environ.setdefault("FRED_API_KEY", "test-fred")
os.environ.setdefault("ALPHA_VANTAGE_KEY", "test-av")
os.environ.setdefault("SEC_EDGAR_USER_AGENT", "TestSystem test@test.com")
os.environ.setdefault("DEEPSEEK_API_KEY", "test-deepseek")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini")
os.environ.setdefault("BENZINGA_API_KEY", "test-benzinga")
os.environ.setdefault("AILS_URL", "http://localhost:8008")
os.environ.setdefault("AXIOM_URL", "http://localhost:8001")
os.environ.setdefault("POLYGON_RATE_LIMIT", "100")
os.environ.setdefault("ORATS_RATE_LIMIT", "60")
os.environ.setdefault("UNUSUAL_WHALES_RATE_LIMIT", "60")
os.environ.setdefault("SPOTGAMMA_RATE_LIMIT", "60")
os.environ.setdefault("MARKET_CHAMELEON_RATE_LIMIT", "60")
os.environ.setdefault("TRADING_ECONOMICS_RATE_LIMIT", "30")
os.environ.setdefault("EDGAR_RATE_LIMIT", "600")
