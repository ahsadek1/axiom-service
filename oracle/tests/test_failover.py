"""
ORACLE Tests — Failover & Rate Limit Tests (T4, T10)
"""

import asyncio
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("ORACLE_DB_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("ORACLE_SECRET", "test-secret-123")
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


# ── T4: Engine Failover ───────────────────────────────────────────────────────

def test_t4_polygon_down_yfinance_fallback():
    """T4: When Polygon fails, price engine falls back to yfinance."""
    from engines import price_engine
    from models import PriceData

    mock_yfinance_data = PriceData(last=875.40, day_high=880.0, day_low=868.0)

    with patch("clients.polygon_client.get_snapshot", return_value=None), \
         patch("engines.price_engine._fetch_yfinance", return_value=mock_yfinance_data):

        data, freshness = price_engine.fetch("NVDA_FAILOVER_TEST")

        # yfinance fallback should provide data
        assert data is not None
        assert data.last == 875.40
        assert freshness == "LIVE"


def test_t4_both_price_sources_down():
    """T4: When both Polygon and yfinance fail, returns UNAVAILABLE."""
    from engines import price_engine

    with patch("clients.polygon_client.get_snapshot", return_value=None), \
         patch("engines.price_engine._fetch_yfinance", return_value=None):

        data, freshness = price_engine.fetch("NVDA_BOTH_FAIL_TEST")

        # Both failed — must not crash, returns unavailable
        assert freshness in ("UNAVAILABLE", "STALE")


def test_t4_failover_logged():
    """T4: Failover events are logged to the database."""
    import sqlite3
    import config as cfg

    cache.log_failover("price", "polygon", "yfinance")

    conn = sqlite3.connect(cfg.ORACLE_DB_PATH)
    rows = conn.execute(
        "SELECT engine, platform, fallback_used FROM failover_events "
        "WHERE engine='price' AND platform='polygon'"
    ).fetchall()
    conn.close()

    assert len(rows) >= 1
    assert rows[-1][2] == "yfinance"


def test_t4_macro_engine_fred_partial_failure():
    """T4: Macro engine handles partial FRED data gracefully."""
    from engines import macro_engine

    # Only VIX available, other series fail
    partial_fred = {
        "vix": 22.3,
        "fed_funds_rate": None,
        "yield_2y": None,
        "yield_10y": None,
        "yield_spread_bps": None,
        "hy_spread_bps": None,
        "latency_ms": 50,
    }

    import cache as cache_mod
    cache_mod._l1.clear()

    with patch("clients.fred_client.get_macro_data", return_value=partial_fred), \
         patch("clients.trading_economics_client.get_calendar", return_value=[]), \
         patch("clients.trading_economics_client.get_next_high_impact_event", return_value=None), \
         patch("cache.get", return_value=None):

        macro, freshness = macro_engine.fetch()

        # Must not crash — partial data still produces a valid regime
        assert macro is not None
        assert macro.vix == 22.3
        assert macro.regime in ("LOW_VOL", "NORMAL", "ELEVATED",
                                "STRESS", "HIGH_STRESS", "CRISIS")
        assert freshness == "LIVE"


# ── T10: Rate Limit Management ────────────────────────────────────────────────

def test_t10_rate_limiter_acquires_tokens():
    """T10: Rate limiter allows requests within limits."""
    import rate_limiter

    # Should acquire immediately (well within rate limit)
    result = rate_limiter.acquire("polygon", timeout=2.0)
    assert result is True


def test_t10_rate_limiter_unknown_platform():
    """T10: Unknown platform returns True (no limit applied)."""
    import rate_limiter
    result = rate_limiter.acquire("unknown_platform_xyz")
    assert result is True


def test_t10_concurrent_requests_no_crashes():
    """T10: Multiple concurrent requests complete without errors.

    Patches are applied OUTSIDE thread spawning to avoid race conditions
    with unittest.mock which uses global module state (not thread-local).
    """
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)
    headers = {"X-Oracle-Secret": "test-secret-123"}

    from models import MacroData
    mock_macro = MacroData(regime="NORMAL", vix=15.0, composite_score=20)

    errors = []
    responses = []

    import threading

    # Patches applied once at test level, not inside each thread.
    with patch("engines.price_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.vol_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.flow_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.gamma_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.macro_engine.fetch", return_value=(mock_macro, "LIVE")), \
         patch("engines.fundamental_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.historical_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.news_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("intelligence.coherence.score", return_value=None):

        def make_request(ticker: str) -> None:
            try:
                resp = client.get(f"/oracle/context/{ticker}", headers=headers)
                responses.append(resp.status_code)
            except Exception as e:
                errors.append(str(e))

        tickers = [f"TEST{i:03d}" for i in range(20)]
        threads = [threading.Thread(target=make_request, args=(t,)) for t in tickers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert len(errors) == 0, f"Errors: {errors}"
    assert all(s == 200 for s in responses), f"Non-200 responses: {responses}"
    assert len(responses) == 20


# ── AILS Non-Blocking Under Load ──────────────────────────────────────────────

def test_ails_non_blocking_under_load():
    """AILS being down must not block or slow other engines."""
    import time
    from engines import historical_engine
    import cache as cache_mod

    import requests

    # Use a unique ticker to guarantee no cache hit from prior tests
    UNIQUE_TICKER = "AILS_DOWN_TEST_TICKER_XYZ"
    cache_mod._l1.pop(f"{UNIQUE_TICKER}:historical:full", None)

    # ails_client.get_historical returns None when AILS is down (it handles all
    # connection errors internally and never propagates them — non-blocking contract).
    with patch("clients.ails_client.get_historical", return_value=None), \
         patch("cache.get", return_value=None):
        start = time.monotonic()
        result, freshness = historical_engine.fetch(UNIQUE_TICKER, "ELEVATED")
        elapsed = time.monotonic() - start

    assert result is None
    assert freshness == "UNAVAILABLE"
    # Should complete quickly (no blocking on AILS timeout)
    assert elapsed < 5.0, f"AILS failure took too long: {elapsed:.1f}s"
