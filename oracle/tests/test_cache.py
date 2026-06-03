"""
ORACLE Tests — Cache Manager Tests
"""

import os
import tempfile
import time

import pytest

# Env setup
os.environ.setdefault("ORACLE_DB_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("ORACLE_SECRET", "test")
os.environ.setdefault("POLYGON_API_KEY", "test")
os.environ.setdefault("ORATS_API_KEY", "test")
os.environ.setdefault("MARKET_CHAMELEON_KEY", "test")
os.environ.setdefault("UNUSUAL_WHALES_KEY", "test")
os.environ.setdefault("SPOTGAMMA_KEY", "test")
os.environ.setdefault("TRADING_ECONOMICS_KEY", "test")
os.environ.setdefault("FRED_API_KEY", "test")
os.environ.setdefault("ALPHA_VANTAGE_KEY", "test")
os.environ.setdefault("SEC_EDGAR_USER_AGENT", "Test test@test.com")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("BENZINGA_API_KEY", "test")
os.environ.setdefault("AILS_URL", "http://localhost:8008")
os.environ.setdefault("AXIOM_URL", "http://localhost:8001")
os.environ.setdefault("POLYGON_RATE_LIMIT", "100")
os.environ.setdefault("ORATS_RATE_LIMIT", "60")
os.environ.setdefault("UNUSUAL_WHALES_RATE_LIMIT", "60")
os.environ.setdefault("SPOTGAMMA_RATE_LIMIT", "60")
os.environ.setdefault("MARKET_CHAMELEON_RATE_LIMIT", "60")
os.environ.setdefault("TRADING_ECONOMICS_RATE_LIMIT", "30")
os.environ.setdefault("EDGAR_RATE_LIMIT", "600")

import cache
cache.init_db()


def test_cache_set_and_get():
    """Set a value and retrieve it."""
    cache.set("NVDA", "price", {"last": 875.40}, ttl_seconds=300)
    result = cache.get("NVDA", "price")
    assert result is not None
    assert result["last"] == 875.40


def test_cache_miss_returns_none():
    """Non-existent key returns None."""
    result = cache.get("ZZZZZ_NONEXISTENT", "price")
    assert result is None


def test_cache_expiry():
    """Expired entries return None."""
    cache.set("EXPTEST", "price", {"last": 100.0}, ttl_seconds=1)
    time.sleep(1.1)
    result = cache.get("EXPTEST", "price")
    assert result is None


def test_cache_hit_rate():
    """Hit rate increases on cache hits."""
    cache.set("HITEST", "vol", {"iv_rank": 72}, ttl_seconds=300)
    cache.get("HITEST", "vol")  # hit
    cache.get("HITEST_MISS", "vol")  # miss
    rate = cache.hit_rate()
    assert 0.0 <= rate <= 1.0


def test_cache_card_type_isolation():
    """Preliminary and full cards are stored separately."""
    cache.set("ISOTEST", "price", {"last": 100.0}, ttl_seconds=300, card_type="preliminary")
    cache.set("ISOTEST", "price", {"last": 200.0}, ttl_seconds=300, card_type="full")

    prelim = cache.get("ISOTEST", "price", card_type="preliminary")
    full = cache.get("ISOTEST", "price", card_type="full")

    assert prelim["last"] == 100.0
    assert full["last"] == 200.0


def test_cache_overwrite():
    """Writing the same key twice overwrites correctly."""
    cache.set("OVERWRITE", "flow", {"bias": "BULLISH"}, ttl_seconds=300)
    cache.set("OVERWRITE", "flow", {"bias": "BEARISH"}, ttl_seconds=300)
    result = cache.get("OVERWRITE", "flow")
    assert result["bias"] == "BEARISH"


def test_warm_count():
    """Warm count reflects non-expired entries."""
    cache.set("WARMTEST1", "price", {"last": 1.0}, ttl_seconds=300)
    cache.set("WARMTEST2", "price", {"last": 2.0}, ttl_seconds=300)
    warm = cache.warm_count()
    assert warm >= 2


def test_log_api_call():
    """API call logging does not raise."""
    cache.log_api_call("price", "polygon", "NVDA", 45, True, cache_hit=False)
    cache.log_api_call("price", "polygon", "AAPL", 12, True, cache_hit=True)


def test_log_failover():
    """Failover event logging does not raise."""
    cache.log_failover("price", "polygon", "yfinance")
