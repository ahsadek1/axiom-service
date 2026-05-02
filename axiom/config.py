"""
config.py — Axiom Service Configuration

Loads and validates all environment variables at startup.
Missing required variable = loud ValueError with clear message.
No hardcoded fallbacks. No silent degradation.
"""

import os
from dataclasses import dataclass


# ── Top-250 universe — ranked by AILS composite performance score ──────────────
# Source: backtest.db weighted_score = SUM(win_rate × sample_count × regime_weight)
# Covers S&P 500 + Nasdaq 100 + liquid ETFs. Regenerated April 12, 2026.
# Tier 1 filter will reduce this to ~100 anchor stocks at 9 AM and 1 PM.
SP500_TICKERS = [
    "SPY",  "TRGP", "BK",   "DIA",  "COST", "XLC",  "GLD",  "QQQ",  "GD",   "NI",
    "RSG",  "GE",   "TJX",  "RTX",  "XLK",  "XLI",  "WMT",  "HLT",  "WMB",  "WM",
    "ETN",  "CBOE", "AFL",  "HWM",  "GOOG", "HYG",  "PH",   "GOOGL","MPC",  "SO",
    "CNP",  "BSX",  "FAST", "XLP",  "IRM",  "HCA",  "WELL", "XLF",  "FFIV", "ET",
    "CBRE", "EPD",  "MLM",  "ORLY", "XLV",  "SPG",  "LIN",  "ETR",  "SPGI", "SMH",
    "AEE",  "CMC",  "AXP",  "PKG",  "VEA",  "AME",  "AGG",  "ACGL", "AZN",  "DUK",
    "EQIX", "JPM",  "AVGO", "PAA",  "FE",   "ECL",  "XLU",  "CB",   "AIG",  "WPM",
    "GS",   "IBKR", "APO",  "TOL",  "ICE",  "MCO",  "ABBV", "LLY",  "TRV",  "TDG",
    "XOM",  "MCD",  "ED",   "FR",   "FTI",  "ARES", "REG",  "KO",   "ISRG", "PGR",
    "LNT",  "MAR",  "IHG",  "IR",   "OKE",  "EFA",  "VTR",  "CVX",  "CAT",  "AAPL",
    "AEM",  "OTIS", "CACI", "VMC",  "PLD",  "CMS",  "STLD", "SFM",  "XLE",  "VRTX",
    "XEL",  "DLR",  "AZO",  "EA",   "JLL",  "XLY",  "AEP",  "MSFT", "PANW", "KMI",
    "CME",  "NEM",  "MA",   "HD",   "MNST", "SOXX", "PM",   "GWW",  "PCG",  "XLRE",
    "ROK",  "EXP",  "MS",   "SRE",  "FOX",  "KEYS", "CWST", "EXC",  "DELL", "RJF",
    "ATI",  "GILD", "FOXA", "AMZN", "USFD", "REGN", "LDOS", "BAC",  "IWM",  "NUE",
    "NVR",  "NVDA", "EMR",  "RS",   "IEF",  "PHM",  "EGP",  "DTE",  "EVRG", "ITW",
    "BLK",  "AON",  "LMT",  "IBM",  "BKR",  "MMC",  "EBAY", "TMUS", "PNW",  "CMG",
    "PRU",  "DRI",  "WDC",  "NVO",  "CPT",  "WEC",  "NOC",  "BKNG", "ULTA", "HII",
    "LRCX", "SHW",  "V",    "RCL",  "UDR",  "PEP",  "WFC",  "H",    "AKAM", "STX",
    "PSX",  "PG",   "TXT",  "CL",   "GRMN", "SLV",  "TMO",  "HSY",  "VLO",
    "NNN",  "IBB",  "JNJ",  "KR",   "DHI",  "NEE",  "STT",  "GDX",  "C",    "XLB",
    "SCHW", "FTNT", "COP",  "IEMG", "CSX",  "AVB",  "AIT",  "SAIC", "J",    "KLAC",
    "O",    "DD",   "LOW",  "ALL",  "FRT",  "EEM",  "ODFL", "D",    "CDNS", "AMAT",
    "MAA",  "PSA",  "CG",   "MDLZ", "MTB",  "AMGN", "NTRS", "KKR",  "KIM",  "MET",
    "ROP",  "SYY",  "SYF",  "CCK",  "MO",   "COF",  "SNPS", "BUD",  "TSN",  "PNC",
]

NASDAQ100_TICKERS = []  # Merged into SP500_TICKERS above (deduplicated)


# ── System Limits (Circuit Breaker Config) ──────────────────────────────────
# These are the canonical values that Axiom exposes via /limits.
# All services (Alpha, Prime, Guardian Angel) should read from here.
MAX_POSITIONS          = 3       # Max concurrent positions across all systems
MAX_RISK_PER_TRADE     = 1000.0  # Max $ risk per trade
MIN_DTE                = 21      # Minimum days to expiration for options
MAX_DTE                = 60      # Maximum days to expiration for options
VIX_PAUSE_THRESHOLD    = 35.0    # VIX above this → pause all new entries
MIN_IVR_CREDIT_SPREAD  = 30      # Minimum IV percentile for credit spreads (0-100)
MAX_IVR_DEBIT_SPREAD   = 70      # Maximum IV percentile for debit spreads (0-100)


@dataclass(frozen=True)
class Settings:
    """All Axiom service configuration. Immutable after startup."""

    # Auth
    axiom_secret: str
    nexus_secret: str          # outbound auth for agent webhooks

    # Data APIs
    polygon_api_key: str
    alpha_vantage_key: str
    fred_api_key: str

    # Telegram
    telegram_bot_token: str
    ahmed_chat_id: str

    # Agent webhooks
    cipher_webhook_url: str
    sage_webhook_url: str
    atlas_webhook_url: str

    # ORACLE Intelligence Hub
    oracle_url: str
    oracle_secret: str

    # Database
    axiom_db_path: str

    # Service
    port: int
    paper_mode: bool

    @property
    def agent_webhooks(self) -> dict[str, str]:
        """Return mapping of agent name to webhook URL."""
        return {
            "Cipher": self.cipher_webhook_url,
            "Sage":   self.sage_webhook_url,
            "Atlas":  self.atlas_webhook_url,
        }

    @property
    def stock_universe(self) -> list[str]:
        """Deduplicated S&P 500 + Nasdaq 100 universe."""
        return list(dict.fromkeys(SP500_TICKERS + NASDAQ100_TICKERS))


def load_settings() -> Settings:
    """
    Load and validate all required environment variables.

    Raises:
        ValueError: If any required environment variable is missing.
                    Lists ALL missing variables in one error message.
    """
    required = [
        "AXIOM_SECRET",
        "NEXUS_SECRET",
        "POLYGON_API_KEY",
        "ALPHA_VANTAGE_KEY",
        "FRED_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "AHMED_CHAT_ID",
        "CIPHER_WEBHOOK_URL",
        "SAGE_WEBHOOK_URL",
        "ATLAS_WEBHOOK_URL",
        "ORACLE_URL",
        "ORACLE_SECRET",
        "AXIOM_DB_PATH",
    ]

    missing = [var for var in required if not os.getenv(var)]
    if missing:
        raise ValueError(
            f"Axiom startup failed — missing required environment variables:\n"
            + "\n".join(f"  • {var}" for var in missing)
            + "\n\nSet all variables in your .env file and restart."
        )

    return Settings(
        axiom_secret        = os.environ["AXIOM_SECRET"],
        nexus_secret        = os.environ["NEXUS_SECRET"],
        polygon_api_key     = os.environ["POLYGON_API_KEY"],
        alpha_vantage_key   = os.environ["ALPHA_VANTAGE_KEY"],
        fred_api_key        = os.environ["FRED_API_KEY"],
        telegram_bot_token  = os.environ["TELEGRAM_BOT_TOKEN"],
        ahmed_chat_id       = os.environ["AHMED_CHAT_ID"],
        cipher_webhook_url  = os.environ["CIPHER_WEBHOOK_URL"],
        sage_webhook_url    = os.environ["SAGE_WEBHOOK_URL"],
        atlas_webhook_url   = os.environ["ATLAS_WEBHOOK_URL"],
        oracle_url          = os.environ["ORACLE_URL"],
        oracle_secret       = os.environ["ORACLE_SECRET"],
        axiom_db_path       = os.environ["AXIOM_DB_PATH"],
        port                = int(os.getenv("PORT", "8001")),
        paper_mode          = os.getenv("PAPER_MODE", "true").lower() == "true",
    )
