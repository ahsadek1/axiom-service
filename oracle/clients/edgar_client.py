"""
ORACLE — SEC EDGAR Client
Provides: Form 4 insider trading data, company filings, ticker→CIK map.
FREE — no API key. Requires User-Agent header only.
Rate limit: 10 req/sec (SEC hard limit).
"""

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

import config

logger = logging.getLogger(__name__)

BASE_URL = "https://data.sec.gov"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
TIMEOUT = (5, 20)

_HEADERS = {"User-Agent": config.SEC_EDGAR_USER_AGENT}

# Simple per-call rate limiter (10 req/sec max)
_last_call_time: float = 0.0
_MIN_INTERVAL: float = 0.11  # ~9 calls/sec to stay safely under 10


def _rate_limit() -> None:
    """Enforce SEC's 10 req/sec hard limit."""
    global _last_call_time
    elapsed = time.monotonic() - _last_call_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_call_time = time.monotonic()


def _get_db_conn() -> sqlite3.Connection:
    """Open the oracle database for CIK map lookups."""
    conn = sqlite3.connect(config.ORACLE_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def refresh_ticker_cik_map() -> int:
    """
    Download the full ticker→CIK map from SEC and store in oracle.db.
    Called once daily at market open.

    Returns:
        Number of tickers stored.
    """
    _rate_limit()
    try:
        resp = requests.get(TICKERS_URL, headers=_HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            logger.error("EDGAR CIK map fetch failed: %s", resp.status_code)
            return 0

        data = resp.json()
        rows = []
        for entry in data.values():
            ticker = str(entry.get("ticker", "")).upper()
            cik = str(entry.get("cik_str", ""))
            name = str(entry.get("title", ""))
            if ticker and cik:
                rows.append((ticker, cik.zfill(10), name))

        with _get_db_conn() as conn:
            conn.executemany(
                """INSERT INTO ticker_cik_map (ticker, cik, company_name, updated_at)
                   VALUES (?, ?, ?, datetime('now'))
                   ON CONFLICT(ticker) DO UPDATE SET
                   cik=excluded.cik, company_name=excluded.company_name,
                   updated_at=excluded.updated_at""",
                rows
            )
        logger.info("EDGAR CIK map refreshed: %d tickers stored", len(rows))
        return len(rows)

    except requests.Timeout:
        logger.error("EDGAR CIK map timeout")
        return 0
    except requests.ConnectionError as e:
        logger.error("EDGAR CIK map connection error: %s", e)
        return 0


def get_cik(ticker: str) -> Optional[str]:
    """
    Look up CIK for a ticker from local cache.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        10-digit zero-padded CIK string, or None if not found.
    """
    try:
        with _get_db_conn() as conn:
            row = conn.execute(
                "SELECT cik FROM ticker_cik_map WHERE ticker=?",
                (ticker.upper(),)
            ).fetchone()
            return row[0] if row else None
    except sqlite3.Error as e:
        logger.error("CIK lookup error for %s: %s", ticker, e)
        return None


def get_insider_transactions(ticker: str, days: int = 90) -> List[Dict[str, Any]]:
    """
    Fetch Form 4 insider transactions for a ticker over the last N days.

    Args:
        ticker: Stock ticker symbol.
        days:   Lookback window in days (default 90).

    Returns:
        List of insider transaction dicts. Empty list on failure.
    """
    end_date = datetime.now(tz=timezone.utc)
    start_date = end_date - timedelta(days=days)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    _rate_limit()
    params = {
        "q": f'"{ticker.upper()}"',
        "forms": "4",
        "dateRange": "custom",
        "startdt": start_str,
        "enddt": end_str,
    }

    try:
        resp = requests.get(SEARCH_URL, params=params, headers=_HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            logger.error("EDGAR Form 4 search failed for %s: %s",
                         ticker, resp.status_code)
            return []

        hits = resp.json().get("hits", {}).get("hits", [])
        # SEC Form 4 transaction codes that indicate direction:
        # P = open-market purchase (buy), S = open-market sale (sell)
        # F/M/A/G/X/D = non-directional (exercise, grant, forfeiture, etc.)
        _BUY_CODES  = {"P"}
        _SELL_CODES = {"S"}

        transactions = []
        for hit in hits[:20]:  # cap at 20 most recent
            src = hit.get("_source", {})
            display_names = src.get("display_names", [])
            # Filter to insiders of the target company (not just any filer)
            relevant_names = [n for n in display_names
                              if ticker.upper() not in n.upper()]

            # EDGAR EFTS search returns a `transaction_codes` list field when
            # available. Fall back to None (direction unknown) if absent.
            # OMNI Pass 3 Finding 3: direction MUST be populated to avoid
            # treating all Form 4 filings (including insider selling) as buys.
            raw_codes = src.get("transaction_codes") or []
            if any(c in _BUY_CODES for c in raw_codes):
                direction = "buy"
            elif any(c in _SELL_CODES for c in raw_codes):
                direction = "sell"
            else:
                direction = None   # unknown — _compute_insider_bias treats as NEUTRAL

            transactions.append({
                "file_date":        src.get("file_date"),
                "filers":           relevant_names,
                "period_of_report": src.get("period_of_report"),
                "form_type":        src.get("form_type", "4"),
                "direction":        direction,   # "buy" | "sell" | None
            })
        return transactions

    except requests.Timeout:
        logger.error("EDGAR Form 4 timeout for %s", ticker)
        return []
    except requests.ConnectionError as e:
        logger.error("EDGAR connection error for %s: %s", ticker, e)
        return []
    except (ValueError, KeyError) as e:
        logger.error("EDGAR parse error for %s: %s", ticker, e)
        return []


def get_company_filings_summary(cik: str) -> Dict[str, Any]:
    """
    Fetch recent filings summary for a company by CIK.

    Args:
        cik: 10-digit zero-padded CIK string.

    Returns:
        Dict with recent Form 4 count and other metadata.
    """
    _rate_limit()
    url = f"{BASE_URL}/submissions/CIK{cik}.json"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            logger.error("EDGAR filings fetch failed for CIK %s: %s", cik, resp.status_code)
            return {}

        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])

        # Count Form 4s in last 90 days
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
        form4_count = sum(
            1 for f, d in zip(forms, dates)
            if f == "4" and d >= cutoff
        )

        return {
            "entity_name": data.get("name"),
            "ticker": data.get("tickers", [None])[0],
            "form4_count_90d": form4_count,
        }
    except requests.Timeout:
        logger.error("EDGAR filings timeout for CIK %s", cik)
        return {}
    except requests.ConnectionError as e:
        logger.error("EDGAR filings connection error for CIK %s: %s", cik, e)
        return {}
    except (ValueError, KeyError) as e:
        logger.error("EDGAR filings parse error for CIK %s: %s", cik, e)
        return {}
