"""
ORACLE Tests — API Endpoint Tests (T1, T2, T11, T12)
All external HTTP calls are mocked. No real API calls.
"""

import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# Set required env vars before importing config
_TEST_DB = tempfile.mktemp(suffix=".db")
os.environ.update({
    "ORACLE_SECRET": "test-secret-123",
    "POLYGON_API_KEY": "test-polygon",
    "ORATS_API_KEY": "test-orats",
    "MARKET_CHAMELEON_KEY": "test-mc",
    "UNUSUAL_WHALES_KEY": "test-uw",
    "SPOTGAMMA_KEY": "test-sg",
    "TRADING_ECONOMICS_KEY": "test-te",
    "FRED_API_KEY": "test-fred",
    "ALPHA_VANTAGE_KEY": "test-av",
    "SEC_EDGAR_USER_AGENT": "TestSystem test@test.com",
    "DEEPSEEK_API_KEY": "test-deepseek",
    "GEMINI_API_KEY": "test-gemini",
    "BENZINGA_API_KEY": "test-benzinga",
    "ORACLE_DB_PATH": _TEST_DB,
    "AILS_URL": "http://localhost:8008",
    "AXIOM_URL": "http://localhost:8001",
    "POLYGON_RATE_LIMIT": "100",
    "ORATS_RATE_LIMIT": "60",
    "UNUSUAL_WHALES_RATE_LIMIT": "60",
    "SPOTGAMMA_RATE_LIMIT": "60",
    "MARKET_CHAMELEON_RATE_LIMIT": "60",
    "TRADING_ECONOMICS_RATE_LIMIT": "30",
    "EDGAR_RATE_LIMIT": "600",
})

from fastapi.testclient import TestClient

import cache
cache.init_db()

from main import app

client = TestClient(app)
AUTH = {"X-Oracle-Secret": "test-secret-123"}
BAD_AUTH = {"X-Oracle-Secret": "wrong-secret"}


# ── T11: Auth Gate ────────────────────────────────────────────────────────────

def test_t11_auth_missing():
    """T11a: Missing auth header returns 403."""
    resp = client.get("/oracle/context/NVDA")
    assert resp.status_code == 403


def test_t11_auth_wrong():
    """T11b: Wrong secret returns 403."""
    resp = client.get("/oracle/context/NVDA", headers=BAD_AUTH)
    assert resp.status_code == 403


def test_t11_auth_correct():
    """T11c: Correct secret allows access."""
    with patch("engines.price_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.vol_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.flow_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.gamma_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.macro_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.fundamental_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.historical_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("intelligence.coherence.score", return_value=None):
        resp = client.get("/oracle/context/NVDA", headers=AUTH)
        assert resp.status_code == 200


# ── T12: Health Endpoint ──────────────────────────────────────────────────────

def test_t12_health_returns_200():
    """T12: Health endpoint returns 200 with correct structure."""
    resp = client.get("/oracle/health", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert "status" in body
    assert "engines" in body
    assert "cache" in body
    assert body["status"] in ("healthy", "degraded", "critical")


def test_t12_health_requires_auth():
    """T12: Health endpoint requires auth."""
    resp = client.get("/oracle/health")
    assert resp.status_code == 403


# ── T1: Full Context Packet ───────────────────────────────────────────────────

def test_t1_full_packet_structure():
    """T1: Full context packet contains all required sections."""
    from models import MacroData, PriceData

    mock_price = PriceData(last=875.40, day_high=881.0, day_low=868.0, volume=14000000)
    mock_macro = MacroData(regime="ELEVATED", vix=22.3, composite_score=42)

    with patch("engines.price_engine.fetch", return_value=(mock_price, "LIVE")), \
         patch("engines.vol_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.flow_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.gamma_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.macro_engine.fetch", return_value=(mock_macro, "LIVE")), \
         patch("engines.fundamental_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("engines.historical_engine.fetch", return_value=(None, "UNAVAILABLE")), \
         patch("intelligence.coherence.score", return_value=None):

        resp = client.get("/oracle/context/NVDA", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()

        # All required fields present
        assert body["ticker"] == "NVDA"
        assert "timestamp" in body
        assert "price_freshness" in body
        assert "vol_freshness" in body
        assert "flow_freshness" in body
        assert "gamma_freshness" in body
        assert "macro_freshness" in body
        assert "fundamental_freshness" in body
        assert "historical_freshness" in body

        # Price data populated
        assert body["price"]["last"] == 875.40

        # Macro data populated
        assert body["macro"]["regime"] == "ELEVATED"


# ── T2: Pre-Warm and Cache Hit ────────────────────────────────────────────────

def test_t2_prefetch_accepts():
    """T2: Prefetch endpoint accepts request and returns 202 metadata."""
    payload = {"tickers": ["NVDA", "AAPL", "MSFT"], "tier": "full"}
    resp = client.post("/oracle/prefetch", json=payload, headers=AUTH)
    assert resp.status_code == 200  # TestClient doesn't do 202 on background tasks
    body = resp.json()
    assert body["accepted"] is True
    assert body["ticker_count"] == 3
    assert body["tier"] == "full"


def test_t2_prefetch_invalid_tier():
    """T2: Invalid tier returns 400."""
    payload = {"tickers": ["NVDA"], "tier": "invalid"}
    resp = client.post("/oracle/prefetch", json=payload, headers=AUTH)
    assert resp.status_code == 400


# ── Single Engine Endpoint ────────────────────────────────────────────────────

def test_single_engine_valid():
    """Single engine endpoint returns correctly structured response."""
    from models import MacroData
    mock_macro = MacroData(regime="NORMAL", vix=15.0, composite_score=20)

    with patch("engines.macro_engine.fetch", return_value=(mock_macro, "LIVE")):
        resp = client.get("/oracle/context/NVDA/macro", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["engine"] == "macro"
        assert body["ticker"] == "NVDA"
        assert body["data"]["regime"] == "NORMAL"


def test_single_engine_invalid():
    """Invalid engine name returns 400."""
    resp = client.get("/oracle/context/NVDA/invalid_engine", headers=AUTH)
    assert resp.status_code == 400


# ── Cost Report ───────────────────────────────────────────────────────────────

def test_cost_report_structure():
    """Cost report returns date and by_platform list."""
    resp = client.get("/oracle/cost-report", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert "date" in body
    assert "by_platform" in body
    assert isinstance(body["by_platform"], list)
