"""
ORACLE Tests — Engine Tests (T3, T5, T8, T9)
All external HTTP calls mocked. Deterministic.
"""

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# Env vars already set by test_api.py if run together, set here for isolation
_TEST_DB = os.environ.get("ORACLE_DB_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("ORACLE_DB_PATH", _TEST_DB)
os.environ.setdefault("ORACLE_SECRET", "test-secret-123")
os.environ.setdefault("POLYGON_API_KEY", "test-polygon")
os.environ.setdefault("FRED_API_KEY", "test-fred")
os.environ.setdefault("ALPHA_VANTAGE_KEY", "test-av")
os.environ.setdefault("SEC_EDGAR_USER_AGENT", "TestSystem test@test.com")
os.environ.setdefault("DEEPSEEK_API_KEY", "test-ds")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini")
os.environ.setdefault("ORATS_API_KEY", "test")
os.environ.setdefault("MARKET_CHAMELEON_KEY", "test")
os.environ.setdefault("UNUSUAL_WHALES_KEY", "test")
os.environ.setdefault("SPOTGAMMA_KEY", "test")
os.environ.setdefault("TRADING_ECONOMICS_KEY", "test")
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


# ── T5: Macro Engine System-Wide ──────────────────────────────────────────────

def test_t5_macro_regime_classification():
    """T5: Macro engine returns regime with correct classification logic."""
    from engines import macro_engine

    fred_mock = {
        "vix": 22.3,
        "fed_funds_rate": 5.25,
        "yield_2y": 4.8,
        "yield_10y": 4.3,
        "yield_spread_bps": -50.0,
        "hy_spread_bps": 310.0,
        "latency_ms": 45,
    }

    import cache as cache_mod
    cache_mod._l1.clear()  # force fresh fetch, not cached from prior test

    with patch("clients.fred_client.get_macro_data", return_value=fred_mock), \
         patch("clients.trading_economics_client.get_calendar", return_value=[]), \
         patch("clients.trading_economics_client.get_next_high_impact_event",
               return_value={"days_until": 8}), \
         patch("cache.get", return_value=None):

        macro, freshness = macro_engine.fetch()

        assert macro is not None
        assert macro.vix == 22.3
        assert macro.regime in ("ELEVATED", "STRESS", "NORMAL", "LOW_VOL",
                                "HIGH_STRESS", "CRISIS")
        assert macro.composite_score >= 0
        assert freshness == "LIVE"
        assert macro.strategy_bias is not None


def test_t5_macro_vix_only():
    """T5: Macro engine handles partial data (VIX only)."""
    from engines import macro_engine

    fred_mock = {
        "vix": 35.0,
        "fed_funds_rate": None,
        "yield_2y": None,
        "yield_10y": None,
        "yield_spread_bps": None,
        "hy_spread_bps": None,
        "latency_ms": 50,
    }

    import cache as cache_mod
    cache_mod._l1.clear()

    with patch("clients.fred_client.get_macro_data", return_value=fred_mock), \
         patch("clients.trading_economics_client.get_calendar", return_value=[]), \
         patch("clients.trading_economics_client.get_next_high_impact_event",
               return_value=None), \
         patch("cache.get", return_value=None):

        macro, freshness = macro_engine.fetch()
        assert macro is not None
        assert macro.vix == 35.0
        # With VIX 35, should be STRESS or higher
        assert macro.regime in ("STRESS", "HIGH_STRESS", "CRISIS", "ELEVATED")


# ── T8: EDGAR Insider Data ────────────────────────────────────────────────────

def test_t8_edgar_insider_data():
    """T8: Fundamental engine includes insider transactions from EDGAR."""
    from engines import fundamental_engine

    mock_av = {
        "quarterly_history": [
            {"date": "2026-02-15", "estimated_eps": 3.5, "actual_eps": 3.9,
             "surprise_pct": 11.4, "beat": True},
        ],
        "latency_ms": 120,
    }
    # OMNI Pass 3 Finding 3: edgar_client now returns "direction" field per transaction.
    # "buy" = open-market purchase (P code), "sell" = open-market sale (S code), None = unknown.
    mock_form4 = [
        {"file_date": "2026-04-03", "filers": ["CEO John Smith"],
         "period_of_report": "2026-04-01", "form_type": "4", "direction": "buy"},
        {"file_date": "2026-03-28", "filers": ["CFO Jane Doe"],
         "period_of_report": "2026-03-27", "form_type": "4", "direction": "buy"},
        {"file_date": "2026-03-15", "filers": ["COO Bob Lee"],
         "period_of_report": "2026-03-14", "form_type": "4", "direction": "buy"},
    ]

    with patch("clients.alpha_vantage_client.get_earnings", return_value=mock_av), \
         patch("clients.edgar_client.get_insider_transactions", return_value=mock_form4), \
         patch("clients.market_chameleon_client.get_earnings_move_history",
               return_value={"avg_actual_move_pct": 8.5}):

        fund, freshness = fundamental_engine.fetch("AAPL", card_type="full")

        assert fund is not None
        assert len(fund.insider_transactions_90d) == 3
        assert fund.insider_cluster_flag is True  # 3+ insiders
        assert fund.insider_net_bias == "STRONG_BUYING"  # 3 confirmed buys → STRONG_BUYING
        assert freshness == "LIVE"


def test_t8_edgar_no_insiders():
    """T8: Fundamental engine handles empty insider data correctly."""
    from engines import fundamental_engine

    with patch("clients.alpha_vantage_client.get_earnings",
               return_value={"quarterly_history": []}), \
         patch("clients.edgar_client.get_insider_transactions", return_value=[]), \
         patch("clients.market_chameleon_client.get_earnings_move_history",
               return_value={}):

        fund, freshness = fundamental_engine.fetch("XYZ", card_type="full")
        assert fund is not None
        assert fund.insider_cluster_flag is False
        assert fund.insider_net_bias == "NEUTRAL"


# ── T9: AILS Non-Blocking ─────────────────────────────────────────────────────

def test_t9_ails_down_non_blocking():
    """T9: Historical engine returns None gracefully when AILS is unavailable."""
    from engines import historical_engine

    with patch("clients.ails_client.get_historical", return_value=None):
        hist, freshness = historical_engine.fetch("NVDA", "ELEVATED")

        # AILS unavailable must NOT crash — returns None cleanly
        assert hist is None
        assert freshness == "UNAVAILABLE"


def test_t9_ails_available():
    """T9: Historical engine parses AILS data correctly when available."""
    from engines import historical_engine

    mock_ails = {
        "instances": 47,
        "win_rate": 0.72,
        "avg_win_pct": 38.5,
        "avg_loss_pct": -18.2,
        "expected_value": 22.1,
        "most_common_failure": "Vol expansion post-entry",
    }

    with patch("clients.ails_client.get_historical", return_value=mock_ails):
        hist, freshness = historical_engine.fetch("NVDA", "ELEVATED")

        assert hist is not None
        assert hist.instances == 47
        assert hist.win_rate == 0.72
        assert hist.data_confidence == "HIGH"
        assert freshness == "LIVE"


# ── T3: Preliminary Card Tier ─────────────────────────────────────────────────

def test_t3_preliminary_card_engines():
    """T3: Verify preliminary cards use only price, vol, macro, and earnings date."""
    from engines import fundamental_engine, macro_engine, price_engine, vol_engine
    from models import FundamentalData, MacroData, PriceData, VolData

    mock_price = PriceData(last=150.0)
    mock_vol = VolData(iv_rank=65.0, iv_percentile=60.0)
    mock_macro = MacroData(regime="NORMAL", vix=16.0, composite_score=22)
    mock_fund = FundamentalData(earnings_date="2026-06-15", days_to_earnings=65,
                                earnings_clear_25d=True, earnings_clear_45d=True)

    price_calls = []
    vol_calls = []
    fund_calls = []

    def track_price(ticker, card_type="full"):
        price_calls.append(card_type)
        return mock_price, "LIVE"

    def track_vol(ticker, card_type="full"):
        vol_calls.append(card_type)
        return mock_vol, "LIVE"

    def track_fund(ticker, card_type="full"):
        fund_calls.append(card_type)
        return mock_fund, "LIVE"

    with patch.object(price_engine, "fetch", side_effect=track_price), \
         patch.object(vol_engine, "fetch", side_effect=track_vol), \
         patch.object(macro_engine, "fetch", return_value=(mock_macro, "LIVE")), \
         patch.object(fundamental_engine, "fetch", side_effect=track_fund):

        # Simulate preliminary assembly
        import asyncio
        from main import _assemble_preliminary_card
        result = asyncio.get_event_loop().run_until_complete(
            _assemble_preliminary_card("AAPL")
        )

        assert result["ticker"] == "AAPL"
        assert result["card_type"] == "preliminary"
        assert result["iv_rank"] == 65.0
        assert result["macro_regime"] == "NORMAL"
        assert result["tier2_signal"] in ("ADVANCE", "BORDERLINE", "HOLD")
        # Preliminary uses correct card_type
        assert "preliminary" in price_calls
        assert "preliminary" in vol_calls
