"""
ORACLE Tests — Intelligence Layer Tests (T6, T7)
DeepSeek coherence + Gemini cross-ticker patterns.
All external calls mocked.
"""

import os
import tempfile
from unittest.mock import patch

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


# ── T6: DeepSeek Signal Coherence ────────────────────────────────────────────

def test_t6_coherence_conflicted_signals():
    """T6: Conflicted signals produce low coherence score."""
    from intelligence import coherence

    mock_deepseek_response = {
        "coherence_score": 28,
        "coherence_level": "CONFLICTED",
        "flags": [
            {
                "type": "FLOW_GAMMA_CONFLICT",
                "description": "Bullish options flow conflicts with negative dealer hedging direction",
                "severity": "WARNING",
            }
        ],
        "aligned_signals": ["high_iv_rank"],
        "conflicting_signals": ["bullish_sweeps", "negative_hiro"],
    }

    context_packet = {
        "ticker": "NVDA",
        "vol": {"iv_rank": 72, "iv_hv_spread": 13.8},
        "flow": {"net_flow_bias": "BULLISH", "put_call_ratio": 1.8, "unusual_activity": True},
        "gamma": {"hiro_signal": -0.32, "net_gex": "NEGATIVE"},
        "macro": {"regime": "ELEVATED", "strategy_bias": "CREDIT_SPREADS_PREFERRED"},
        "fundamental": {"insider_net_bias": "NEUTRAL", "days_to_earnings": 47},
    }

    with patch.object(coherence.deepseek_client, "get_coherence", return_value=mock_deepseek_response):
        result = coherence.score("NVDA", context_packet)

    assert result is not None
    assert result.coherence_score == 28
    assert result.coherence_level == "CONFLICTED"
    assert len(result.flags) >= 1
    assert result.flags[0].type == "FLOW_GAMMA_CONFLICT"
    # CRITICAL: No directional language in flag descriptions
    description = result.flags[0].description.lower()
    assert "buy" not in description
    assert "sell" not in description
    assert "bullish" not in description or "conflict" in description


def test_t6_coherence_aligned_signals():
    """T6: Aligned signals produce high coherence score."""
    from intelligence import coherence

    mock_response = {
        "coherence_score": 88,
        "coherence_level": "HIGH",
        "flags": [],
        "aligned_signals": ["iv_rank_elevated", "bullish_flow", "positive_hiro"],
        "conflicting_signals": [],
    }

    context_packet = {
        "ticker": "AAPL",
        "vol": {"iv_rank": 65, "iv_hv_spread": 10.2},
        "flow": {"net_flow_bias": "BULLISH", "put_call_ratio": 0.7, "unusual_activity": True},
        "gamma": {"hiro_signal": 0.45, "net_gex": "POSITIVE"},
        "macro": {"regime": "NORMAL", "strategy_bias": "NEUTRAL"},
        "fundamental": {"insider_net_bias": "BUYING", "days_to_earnings": 60},
    }

    with patch.object(coherence.deepseek_client, "get_coherence", return_value=mock_response):
        result = coherence.score("AAPL", context_packet)

    assert result is not None
    assert result.coherence_score >= 80
    assert result.coherence_level == "HIGH"
    assert len(result.flags) == 0


def test_t6_coherence_deepseek_unavailable_fallback():
    """T6: Fallback coherence fires when DeepSeek is down — no crash."""
    from intelligence import coherence

    context_packet = {
        "ticker": "TSLA",
        "vol": {"iv_rank": 80},
        "flow": {"net_flow_bias": "BEARISH", "put_call_ratio": 1.5},
        "gamma": {"hiro_signal": -0.5},
        "macro": {"regime": "STRESS", "strategy_bias": "CREDIT_SPREADS_ONLY_REDUCED_SIZE"},
        "fundamental": {"insider_net_bias": "NEUTRAL"},
    }

    with patch.object(coherence.deepseek_client, "get_coherence", return_value=None):
        result = coherence.score("TSLA", context_packet)

    # Must NOT crash — fallback must return a result
    assert result is not None
    assert result.coherence_score >= 0
    assert result.coherence_level in ("HIGH", "MEDIUM", "LOW", "CONFLICTED")


# ── T7: Gemini Cross-Ticker Pattern Detection ─────────────────────────────────

def test_t7_sector_flow_pattern_detected():
    """T7: Gemini detects sector flow event when multiple tickers show correlated activity."""
    from intelligence import patterns

    mock_gemini_response = {
        "patterns_detected": [
            {
                "type": "SECTOR_FLOW_EVENT",
                "tickers_involved": ["NVDA", "AMD", "SMCI", "AMAT"],
                "description": "4 semiconductor tickers showing correlated bullish sweep activity in same cycle",
                "implication": "Sector-level institutional positioning likely — concordance on semis may not be independent",
            }
        ],
        "echo_chamber_risk_tickers": ["NVDA", "AMD", "SMCI", "AMAT"],
    }

    pool_packets = [
        {"ticker": "NVDA", "flow": {"net_flow_bias": "BULLISH", "unusual_activity": True}},
        {"ticker": "AMD", "flow": {"net_flow_bias": "BULLISH", "unusual_activity": True}},
        {"ticker": "SMCI", "flow": {"net_flow_bias": "BULLISH", "unusual_activity": True}},
        {"ticker": "AMAT", "flow": {"net_flow_bias": "BULLISH", "unusual_activity": True}},
        {"ticker": "JPM", "flow": {"net_flow_bias": "NEUTRAL", "unusual_activity": False}},
    ]

    with patch("clients.gemini_client.detect_patterns", return_value=mock_gemini_response):
        result = patterns.detect("test-cycle-001", pool_packets)

    assert result is not None
    assert len(result.patterns_detected) >= 1
    assert result.patterns_detected[0].type == "SECTOR_FLOW_EVENT"
    assert "NVDA" in result.echo_chamber_risk_tickers
    assert len(result.tickers_in_pool) == 5


def test_t7_no_patterns_clean_pool():
    """T7: No patterns when signals are independent and uncorrelated."""
    from intelligence import patterns

    mock_gemini_response = {
        "patterns_detected": [],
        "echo_chamber_risk_tickers": [],
    }

    pool_packets = [
        {"ticker": "AAPL", "flow": {"net_flow_bias": "BULLISH", "unusual_activity": True}},
        {"ticker": "JPM", "flow": {"net_flow_bias": "NEUTRAL", "unusual_activity": False}},
        {"ticker": "XOM", "flow": {"net_flow_bias": "BEARISH", "unusual_activity": False}},
    ]

    with patch("clients.gemini_client.detect_patterns", return_value=mock_gemini_response):
        result = patterns.detect("test-cycle-002", pool_packets)

    assert result is not None
    assert len(result.patterns_detected) == 0
    assert len(result.echo_chamber_risk_tickers) == 0


def test_t7_gemini_unavailable_fallback():
    """T7: Fallback pattern detection fires when Gemini is down — no crash."""
    from intelligence import patterns

    # 5 tickers with unusual activity — fallback should detect sector event
    pool_packets = [
        {"ticker": f"TICK{i}", "flow": {"net_flow_bias": "BULLISH", "unusual_activity": True},
         "gamma": {}, "vol": {}}
        for i in range(5)
    ]

    with patch("clients.gemini_client.detect_patterns", return_value=None):
        result = patterns.detect("test-cycle-003", pool_packets)

    # Must NOT crash
    assert result is not None
    # Fallback should detect the correlated unusual activity
    assert len(result.patterns_detected) >= 1
    assert result.patterns_detected[0].type == "SECTOR_FLOW_EVENT"


# ── Benzinga Sentiment Scoring ────────────────────────────────────────────────

def test_benzinga_sentiment_bullish():
    """Bullish keywords produce BULLISH sentiment."""
    from clients.benzinga_client import score_sentiment
    result = score_sentiment("NVDA Beats Earnings Estimates, Stock Surges", "Record profits reported")
    assert result == "BULLISH"


def test_benzinga_sentiment_bearish():
    """Bearish keywords produce BEARISH sentiment."""
    from clients.benzinga_client import score_sentiment
    result = score_sentiment("AAPL Misses Revenue Estimates, Downgraded", "Weak guidance concerns analysts")
    assert result == "BEARISH"


def test_benzinga_sentiment_neutral():
    """Neutral headlines produce NEUTRAL sentiment."""
    from clients.benzinga_client import score_sentiment
    result = score_sentiment("Apple Announces New Product Line", "Company updates product offerings")
    assert result == "NEUTRAL"
