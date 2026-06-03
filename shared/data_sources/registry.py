"""
registry.py — Master Data Source Registry
==========================================
Single source of truth for all external APIs.
Defines tiers, probe endpoints, fallback chains,
and health thresholds for every data source.

Ahmed directive May 2026:
  Every external dependency monitored.
  Every failure detected immediately.
  Every system gets notified.
  Every fallback deployed automatically.
  Everything restored automatically when fixed.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable


class SourceStatus(str, Enum):
    HEALTHY    = "HEALTHY"     # <2s, >95% success
    DEGRADED   = "DEGRADED"    # 2-10s or 80-95% success
    FAILED     = "FAILED"      # >10s or <80% or connection error
    RECOVERING = "RECOVERING"  # detected recovery, confirming
    UNKNOWN    = "UNKNOWN"     # not yet probed


class SourceTier(str, Enum):
    PRIMARY   = "PRIMARY"    # live preferred API
    SECONDARY = "SECONDARY"  # alternative live API
    EMERGENCY = "EMERGENCY"  # static/cached fallback


class SourceCategory(str, Enum):
    PRICE      = "PRICE"       # price bars, quotes
    OPTIONS_IV = "OPTIONS_IV"  # IV rank, skew, term structure
    OPTIONS_FLOW = "OPTIONS_FLOW"  # unusual options activity
    EARNINGS   = "EARNINGS"    # earnings calendar
    MACRO      = "MACRO"       # economic indicators
    EXECUTION  = "EXECUTION"   # order placement
    COMMS      = "COMMS"       # notifications


@dataclass
class ProbeConfig:
    """How to health-check a data source."""
    url:          str
    method:       str  = "GET"
    headers:      dict = field(default_factory=dict)
    params:       dict = field(default_factory=dict)
    timeout_s:    float = 5.0
    expect_key:   Optional[str] = None   # key that must exist in response
    expect_status: int = 200


@dataclass
class DataSource:
    """Definition of one data source tier."""
    source_id:    str          # e.g. "polygon_primary"
    name:         str          # human name
    category:     SourceCategory
    tier:         SourceTier
    probe:        ProbeConfig
    api_key_env:  Optional[str] = None   # env var name for API key

    # Thresholds
    degraded_latency_s:  float = 2.0
    failed_latency_s:    float = 10.0
    failed_error_rate:   float = 0.20   # 20% errors = FAILED

    # Recovery
    recover_consecutive: int = 3    # successes needed to enter RECOVERING
    healthy_consecutive: int = 5    # successes needed to return to HEALTHY

    # Fallback chain
    fallback_source_id: Optional[str] = None  # next source to use when this fails

    # Current state (managed by health monitor)
    status:       SourceStatus = SourceStatus.UNKNOWN
    active_tier:  SourceTier   = SourceTier.PRIMARY


import os

POLYGON_KEY  = os.getenv("POLYGON_API_KEY",  "ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl")
ORATS_TOKEN  = os.getenv("ORATS_TOKEN",      "4476e955-241a-4540-b114-ebbf1a3a3b87")
AV_KEY       = os.getenv("ALPHA_VANTAGE_KEY","5LPKGHYMW9ZK24KL")
ALPACA_KEY   = os.getenv("ALPACA_API_KEY",   "")
ALPACA_SEC   = os.getenv("ALPACA_SECRET_KEY","")
TG_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN","")
TG_CHAT      = os.getenv("AHMED_CHAT_ID",    "8573754783")


# ── Master registry ────────────────────────────────────────────────────────────
ALL_SOURCES: dict[str, DataSource] = {

    # ── PRICE DATA ─────────────────────────────────────────────────────────────

    "polygon_primary": DataSource(
        source_id   = "polygon_primary",
        name        = "Polygon.io (Primary)",
        category    = SourceCategory.PRICE,
        tier        = SourceTier.PRIMARY,
        api_key_env = "POLYGON_API_KEY",
        probe       = ProbeConfig(
            url        = f"https://api.polygon.io/v2/aggs/ticker/SPY/prev",
            params     = {"apiKey": POLYGON_KEY},
            timeout_s  = 8.0,
            expect_key = "results",
        ),
        fallback_source_id = "alpaca_data_secondary",
    ),

    "alpaca_data_secondary": DataSource(
        source_id   = "alpaca_data_secondary",
        name        = "Alpaca Data (Secondary Price)",
        category    = SourceCategory.PRICE,
        tier        = SourceTier.SECONDARY,
        api_key_env = "ALPACA_API_KEY",
        probe       = ProbeConfig(
            url     = "https://data.alpaca.markets/v2/stocks/SPY/trades/latest",
            headers = {
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SEC,
            },
            params     = {"feed": "iex"},
            timeout_s  = 6.0,
            expect_key = "trade",
        ),
        fallback_source_id = "price_cache_emergency",
    ),

    "price_cache_emergency": DataSource(
        source_id  = "price_cache_emergency",
        name       = "Price Cache (Emergency)",
        category   = SourceCategory.PRICE,
        tier       = SourceTier.EMERGENCY,
        probe      = ProbeConfig(
            url       = "http://localhost:8001/pool",
            headers   = {"X-Axiom-Secret": os.getenv("AXIOM_SECRET","")},
            timeout_s = 3.0,
        ),
    ),

    # ── OPTIONS IV DATA ────────────────────────────────────────────────────────

    "orats_primary": DataSource(
        source_id   = "orats_primary",
        name        = "ORATS (Primary IV)",
        category    = SourceCategory.OPTIONS_IV,
        tier        = SourceTier.PRIMARY,
        api_key_env = "ORATS_TOKEN",
        probe       = ProbeConfig(
            url    = "https://api.orats.io/datav2/tickers",
            params = {"token": ORATS_TOKEN, "ticker": "SPY"},
            timeout_s  = 8.0,
            expect_key = "data",
        ),
        fallback_source_id = "polygon_vol_proxy",
    ),

    "polygon_vol_proxy": DataSource(
        source_id  = "polygon_vol_proxy",
        name       = "Polygon Vol Proxy (Secondary IV)",
        category   = SourceCategory.OPTIONS_IV,
        tier       = SourceTier.SECONDARY,
        probe      = ProbeConfig(
            url    = f"https://api.polygon.io/v3/snapshot/options/SPY",
            params = {"apiKey": POLYGON_KEY, "limit": 1},
            timeout_s  = 8.0,
        ),
        fallback_source_id = "iv_static_emergency",
    ),

    "iv_static_emergency": DataSource(
        source_id = "iv_static_emergency",
        name      = "Static IV Table (Emergency)",
        category  = SourceCategory.OPTIONS_IV,
        tier      = SourceTier.EMERGENCY,
        probe     = ProbeConfig(
            url       = "http://localhost:8001/health",
            timeout_s = 2.0,
        ),
    ),

    # ── OPTIONS FLOW ───────────────────────────────────────────────────────────

    "unusual_whales_primary": DataSource(
        source_id   = "unusual_whales_primary",
        name        = "Polygon Volume Flow (Primary Flow)",
        category    = SourceCategory.OPTIONS_FLOW,
        tier        = SourceTier.PRIMARY,
        probe       = ProbeConfig(
            url    = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/SPY",
            params = {"apiKey": POLYGON_KEY},
            timeout_s = 8.0,
        ),
        fallback_source_id = "flow_neutral_emergency",
    ),

    "polygon_flow_proxy": DataSource(
        source_id  = "polygon_flow_proxy",
        name       = "Polygon Volume Proxy (Secondary Flow)",
        category   = SourceCategory.OPTIONS_FLOW,
        tier       = SourceTier.SECONDARY,
        probe      = ProbeConfig(
            url    = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/SPY",
            params = {"apiKey": POLYGON_KEY},
            timeout_s = 8.0,
        ),
        fallback_source_id = "flow_neutral_emergency",
    ),

    "flow_neutral_emergency": DataSource(
        source_id = "flow_neutral_emergency",
        name      = "Neutral Flow Score (Emergency)",
        category  = SourceCategory.OPTIONS_FLOW,
        tier      = SourceTier.EMERGENCY,
        probe     = ProbeConfig(
            url       = "http://localhost:8001/health",
            timeout_s = 2.0,
        ),
    ),

    # ── EARNINGS CALENDAR ──────────────────────────────────────────────────────

    "alpha_vantage_primary": DataSource(
        source_id   = "alpha_vantage_primary",
        name        = "Alpha Vantage (Primary Earnings)",
        category    = SourceCategory.EARNINGS,
        tier        = SourceTier.PRIMARY,
        api_key_env = "ALPHA_VANTAGE_KEY",
        probe       = ProbeConfig(
            url    = "https://www.alphavantage.co/query",
            params = {
                "function": "EARNINGS_CALENDAR",
                "horizon":  "3month",
                "apikey":   AV_KEY,
            },
            timeout_s = 15.0,
        ),
        fallback_source_id = "earnings_cache_secondary",
    ),

    "earnings_cache_secondary": DataSource(
        source_id  = "earnings_cache_secondary",
        name       = "Earnings DB Cache (Secondary)",
        category   = SourceCategory.EARNINGS,
        tier       = SourceTier.SECONDARY,
        probe      = ProbeConfig(
            url       = "http://localhost:8013/calendar",
            timeout_s = 3.0,
        ),
        fallback_source_id = "earnings_static_emergency",
    ),

    "earnings_static_emergency": DataSource(
        source_id = "earnings_static_emergency",
        name      = "Static Earnings DB (Emergency)",
        category  = SourceCategory.EARNINGS,
        tier      = SourceTier.EMERGENCY,
        probe     = ProbeConfig(
            url       = "http://localhost:8013/health",
            timeout_s = 2.0,
        ),
    ),

    # ── MACRO DATA ─────────────────────────────────────────────────────────────

    "axiom_macro_primary": DataSource(
        source_id  = "axiom_macro_primary",
        name       = "Axiom Macro/VIX (Primary)",
        category   = SourceCategory.MACRO,
        tier       = SourceTier.PRIMARY,
        probe      = ProbeConfig(
            url     = "http://localhost:8001/pool",
            headers = {"X-Axiom-Secret": "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2"},
            timeout_s  = 5.0,
            expect_key = "regime",
        ),
        fallback_source_id = "polygon_vix_secondary",
    ),

    "polygon_vix_secondary": DataSource(
        source_id  = "polygon_vix_secondary",
        name       = "Polygon VIX Proxy (Secondary)",
        category   = SourceCategory.MACRO,
        tier       = SourceTier.SECONDARY,
        probe      = ProbeConfig(
            url    = f"https://api.polygon.io/v2/aggs/ticker/VXX/prev",
            params = {"apiKey": POLYGON_KEY},
            timeout_s = 8.0,
        ),
        fallback_source_id = "macro_static_emergency",
    ),

    "macro_static_emergency": DataSource(
        source_id = "macro_static_emergency",
        name      = "Static Macro Defaults (Emergency)",
        category  = SourceCategory.MACRO,
        tier      = SourceTier.EMERGENCY,
        probe     = ProbeConfig(
            url       = "http://localhost:8001/health",
            timeout_s = 2.0,
        ),
    ),

    # ── EXECUTION ──────────────────────────────────────────────────────────────

    "alpaca_execution_primary": DataSource(
        source_id   = "alpaca_execution_primary",
        name        = "Alpaca Paper Execution (Primary)",
        category    = SourceCategory.EXECUTION,
        tier        = SourceTier.PRIMARY,
        api_key_env = "ALPACA_API_KEY",
        probe       = ProbeConfig(
            url     = "https://paper-api.alpaca.markets/v2/account",
            headers = {
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SEC,
            },
            timeout_s  = 8.0,
            expect_key = "status",
        ),
        # No fallback for execution — HALT if down
        fallback_source_id = None,
    ),

    # ── COMMUNICATIONS ─────────────────────────────────────────────────────────

    "telegram_primary": DataSource(
        source_id  = "telegram_primary",
        name       = "Telegram API (Primary Comms)",
        category   = SourceCategory.COMMS,
        tier       = SourceTier.PRIMARY,
        probe      = ProbeConfig(
            url       = f"https://api.telegram.org/bot{TG_TOKEN}/getMe",
            timeout_s = 5.0,
            expect_key = "ok",
        ),
        fallback_source_id = "log_file_secondary",
    ),

    "log_file_secondary": DataSource(
        source_id = "log_file_secondary",
        name      = "Log File (Secondary Comms)",
        category  = SourceCategory.COMMS,
        tier      = SourceTier.SECONDARY,
        probe     = ProbeConfig(
            url       = "http://localhost:8099/health-mesh",
            timeout_s = 2.0,
        ),
    ),
}


def get_source(source_id: str) -> Optional[DataSource]:
    return ALL_SOURCES.get(source_id)


def get_by_category(category: SourceCategory) -> list[DataSource]:
    return [s for s in ALL_SOURCES.values() if s.category == category]


def get_primary_sources() -> list[DataSource]:
    return [s for s in ALL_SOURCES.values() if s.tier == SourceTier.PRIMARY]


def get_active_source(category: SourceCategory) -> Optional[DataSource]:
    """Get the currently active (healthy) source for a category."""
    sources = sorted(
        get_by_category(category),
        key=lambda s: [SourceTier.PRIMARY, SourceTier.SECONDARY, SourceTier.EMERGENCY].index(s.tier)
    )
    for source in sources:
        if source.status in (SourceStatus.HEALTHY, SourceStatus.RECOVERING, SourceStatus.UNKNOWN):
            return source
    return sources[-1] if sources else None  # emergency fallback
