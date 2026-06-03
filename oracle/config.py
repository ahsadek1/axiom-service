"""
ORACLE Intelligence Hub — Configuration
Single source of truth for all environment variables.
Fails loudly at import time if any required variable is missing.
"""

import os

# ── Auth ──────────────────────────────────────────────────────────────────────
ORACLE_SECRET: str = os.environ["ORACLE_SECRET"]

# ── Service URLs ──────────────────────────────────────────────────────────────
AXIOM_URL: str = os.environ["AXIOM_URL"]
AILS_URL: str = os.environ["AILS_URL"]
AILS_SECRET: str = os.environ.get("AILS_SECRET", "")

# ── Data Platform APIs ────────────────────────────────────────────────────────
POLYGON_API_KEY: str = os.environ["POLYGON_API_KEY"]
ORATS_API_KEY: str = os.environ["ORATS_API_KEY"]
MARKET_CHAMELEON_KEY: str = os.environ.get("MARKET_CHAMELEON_KEY", "")  # No public API — Polygon math fallback
UNUSUAL_WHALES_KEY: str = os.environ.get("UNUSUAL_WHALES_KEY", "")     # Key pending — stub active
SPOTGAMMA_KEY: str = os.environ.get("SPOTGAMMA_KEY", "")               # No public API — Polygon math fallback
TRADING_ECONOMICS_KEY: str = os.environ["TRADING_ECONOMICS_KEY"]
FRED_API_KEY: str = os.environ["FRED_API_KEY"]
ALPHA_VANTAGE_KEY: str = os.environ["ALPHA_VANTAGE_KEY"]
SEC_EDGAR_USER_AGENT: str = os.environ["SEC_EDGAR_USER_AGENT"]

# ── AI Intelligence Layer ─────────────────────────────────────────────────────
DEEPSEEK_API_KEY: str = os.environ["DEEPSEEK_API_KEY"]
GEMINI_API_KEY: str = os.environ["GEMINI_API_KEY"]

# ── News (Benzinga Cloud API) ─────────────────────────────────────────────────
BENZINGA_API_KEY: str = os.environ["BENZINGA_API_KEY"]
BENZINGA_NEWS_URL: str = "https://api.benzinga.com/api/v2/news"
NEWS_TTL: int = 300  # 5 minutes — news is time-sensitive

# ── Database ──────────────────────────────────────────────────────────────────
ORACLE_DB_PATH: str = os.environ["ORACLE_DB_PATH"]

# ── Rate Limits (requests per minute per platform) ────────────────────────────
POLYGON_RATE_LIMIT: int = int(os.environ["POLYGON_RATE_LIMIT"])
ORATS_RATE_LIMIT: int = int(os.environ["ORATS_RATE_LIMIT"])
UNUSUAL_WHALES_RATE_LIMIT: int = int(os.environ["UNUSUAL_WHALES_RATE_LIMIT"])
SPOTGAMMA_RATE_LIMIT: int = int(os.environ["SPOTGAMMA_RATE_LIMIT"])
MARKET_CHAMELEON_RATE_LIMIT: int = int(os.environ["MARKET_CHAMELEON_RATE_LIMIT"])
TRADING_ECONOMICS_RATE_LIMIT: int = int(os.environ["TRADING_ECONOMICS_RATE_LIMIT"])
EDGAR_RATE_LIMIT: int = int(os.environ["EDGAR_RATE_LIMIT"])

# ── Cache TTLs (seconds) ──────────────────────────────────────────────────────
PRICE_TTL_MARKET_HOURS: int = 30
PRICE_TTL_AFTER_CLOSE: int = 86400
VOL_TTL: int = 900
FLOW_TTL_MARKET_HOURS: int = 300
FLOW_TTL_AFTER_CLOSE: int = 3600
GAMMA_TTL_LEVELS: int = 900
GAMMA_TTL_HIRO: int = 300
MACRO_TTL_STANDARD: int = 21600
MACRO_TTL_EVENT_DAY: int = 3600
FUNDAMENTAL_TTL: int = 86400
HISTORICAL_TTL: int = 86400
PRELIMINARY_CARD_TTL: int = 14400  # 4 hours
FULL_CARD_TTL: int = 900           # 15 minutes

# ── EDGAR ─────────────────────────────────────────────────────────────────────
EDGAR_CIK_MAP_URL: str = "https://www.sec.gov/files/company_tickers.json"
EDGAR_MAX_RATE_PER_SECOND: int = 10  # SEC hard limit

# ── FRED Series IDs ───────────────────────────────────────────────────────────
FRED_SERIES: dict = {
    "VIX": "VIXCLS",
    "FFR": "FEDFUNDS",
    "YIELD_10Y": "DGS10",
    "YIELD_2Y": "DGS2",
    "HY_SPREAD": "BAMLH0A0HYM2EY",
}

# ── Polygon ───────────────────────────────────────────────────────────────────
POLYGON_SNAPSHOT_URL: str = (
    "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
)

# ── DeepSeek ──────────────────────────────────────────────────────────────────
DEEPSEEK_API_URL: str = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL: str = "deepseek-chat"
DEEPSEEK_TEMPERATURE: float = 0.1
DEEPSEEK_MAX_TOKENS: int = 400

# ── Gemini ────────────────────────────────────────────────────────────────────
GEMINI_MODEL: str = "gemini-1.5-pro"

# ── Regime classification values ──────────────────────────────────────────────
VALID_REGIMES: frozenset = frozenset(
    {"LOW_VOL", "NORMAL", "ELEVATED", "STRESS", "HIGH_STRESS", "CRISIS"}
)

# ── Valid engine names ────────────────────────────────────────────────────────
VALID_ENGINES: frozenset = frozenset(
    {"price", "vol", "flow", "gamma", "macro", "fundamental", "historical"}
)

# ── Data freshness flag values ────────────────────────────────────────────────
FRESHNESS_LIVE: str = "LIVE"
FRESHNESS_STALE: str = "STALE"
FRESHNESS_DEGRADED: str = "DEGRADED"   # Cipher Pass 3 P3-8: partial stub (one source real, one stub)
FRESHNESS_UNAVAILABLE: str = "UNAVAILABLE"

# ── App server ────────────────────────────────────────────────────────────────
ORACLE_PORT: int = 8007
ORACLE_HOST: str = "0.0.0.0"
