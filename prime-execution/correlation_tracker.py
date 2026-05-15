"""
correlation_tracker.py — Cross-Position Correlation Gate

Computes Pearson correlation between an incoming ticker and all open
position tickers. Returns a gate decision (APPROVE / REDUCE / BLOCK)
with a size adjustment multiplier, and logs every evaluation to the DB.

Decision table:
  max_corr < 0.60   → APPROVE at 1.0x (full size)
  0.60 <= max_corr < 0.75 → APPROVE at 1.0x (log only)
  0.75 <= max_corr < 0.85 → REDUCE  at 0.75x (25% cut)
  0.85 <= max_corr < 0.95 → REDUCE  at 0.50x (50% cut)
  max_corr >= 0.95  → BLOCK  at 0.0x — alert Ahmed via Telegram

Return history: 60-day daily returns from Polygon.io.
Failure modes:
  - Ticker return history unavailable → use 0.5 proxy, flag ESTIMATED
  - No open positions → APPROVE full size immediately
  - Computation error → APPROVE at 0.5x conservatively, log error

Mandated by Ahmed Sadek. Built by GENESIS 2026-05-07.
"""

import json
import logging
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Generator, List, Optional

import requests

logger = logging.getLogger("nexus.correlation_tracker")

# ── Constants ─────────────────────────────────────────────────────────────────

CORRELATION_DAYS: int = 60         # lookback period for return history

# Decision thresholds — inclusive lower bound per band
_THRESH_REDUCE_LIGHT: float = 0.75   # ≥0.75 → 25% cut
_THRESH_REDUCE_HEAVY: float = 0.85   # ≥0.85 → 50% cut
_THRESH_BLOCK:        float = 0.95   # ≥0.95 → BLOCK

# Fallback correlation when history unavailable (conservative proxy)
_ESTIMATED_CORR: float = 0.50


# ── Data Types ────────────────────────────────────────────────────────────────

@dataclass
class CorrelationResult:
    """Gate decision returned by evaluate_correlation_gate().

    Attributes:
        gate_decision:      'APPROVE', 'REDUCE', or 'BLOCK'.
        size_adjustment:    Multiplier for position size (0.0–1.0).
        max_correlation:    Highest pairwise correlation found (or proxy).
        existing_tickers:   Tickers compared against.
        correlation_matrix: {ticker: corr_value} mapping; may be empty.
        estimated:          True if any return history was missing (proxy used).
        error:              Non-empty string if a computation error occurred.
    """
    gate_decision:     str
    size_adjustment:   float
    max_correlation:   float
    existing_tickers:  List[str]
    correlation_matrix: dict
    estimated:         bool = False
    error:             str  = ""


# ── DB helpers ────────────────────────────────────────────────────────────────

@contextmanager
def _get_conn(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """Context-managed SQLite connection with WAL and row_factory."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_correlation_schema(db_path: str) -> None:
    """Create correlation_records table if it does not exist.

    Args:
        db_path: Path to the SQLite database file.
    """
    with _get_conn(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS correlation_records (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluation_ts    TEXT    DEFAULT (datetime('now')),
                new_ticker       TEXT    NOT NULL,
                existing_tickers TEXT    NOT NULL,
                max_correlation  REAL    NOT NULL,
                gate_decision    TEXT    NOT NULL
                    CHECK (gate_decision IN ('APPROVE','REDUCE','BLOCK')),
                size_adjustment  REAL    NOT NULL DEFAULT 1.0,
                correlation_matrix TEXT  NOT NULL
            )
        """)
    logger.info("correlation_records schema ready at %s", db_path)


def _log_correlation_record(
    db_path:            str,
    new_ticker:         str,
    existing_tickers:   List[str],
    max_correlation:    float,
    gate_decision:      str,
    size_adjustment:    float,
    correlation_matrix: dict,
) -> None:
    """Insert a correlation evaluation record into the DB.

    Args:
        db_path:            Path to SQLite DB.
        new_ticker:         Incoming ticker being evaluated.
        existing_tickers:   Open position tickers evaluated against.
        max_correlation:    Highest correlation found.
        gate_decision:      'APPROVE', 'REDUCE', or 'BLOCK'.
        size_adjustment:    Size multiplier applied.
        correlation_matrix: Dict mapping ticker → correlation value.
    """
    try:
        with _get_conn(db_path) as conn:
            conn.execute(
                """
                INSERT INTO correlation_records
                    (new_ticker, existing_tickers, max_correlation,
                     gate_decision, size_adjustment, correlation_matrix)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    new_ticker,
                    json.dumps(existing_tickers),
                    round(max_correlation, 6),
                    gate_decision,
                    size_adjustment,
                    json.dumps(correlation_matrix),
                ),
            )
    except Exception as exc:
        logger.error("correlation_records INSERT failed (non-fatal): %s", exc)


# ── Core math ─────────────────────────────────────────────────────────────────

def compute_correlation(
    returns_a: List[float],
    returns_b: List[float],
) -> float:
    """Compute Pearson correlation coefficient between two return series.

    Truncates both series to the shorter length. Returns 0.0 if either
    series is constant (zero standard deviation) or fewer than 2 points.

    Args:
        returns_a: Daily return series for ticker A.
        returns_b: Daily return series for ticker B.

    Returns:
        Pearson r in [-1.0, 1.0]. Returns 0.0 on degenerate input.
    """
    n = min(len(returns_a), len(returns_b))
    if n < 2:
        logger.warning("compute_correlation: fewer than 2 aligned data points — returning 0.0")
        return 0.0

    a = returns_a[-n:]
    b = returns_b[-n:]

    mean_a = sum(a) / n
    mean_b = sum(b) / n
    dev_a  = [x - mean_a for x in a]
    dev_b  = [x - mean_b for x in b]

    numerator   = sum(da * db for da, db in zip(dev_a, dev_b))
    sum_sq_a    = sum(da * da for da in dev_a)
    sum_sq_b    = sum(db * db for db in dev_b)

    if sum_sq_a < 1e-15 or sum_sq_b < 1e-15:
        logger.warning("compute_correlation: zero variance in one series — returning 0.0")
        return 0.0

    denominator = math.sqrt(sum_sq_a * sum_sq_b)
    r = numerator / denominator
    # Clamp to [-1, 1] for floating-point edge cases
    return max(-1.0, min(1.0, r))


# ── Polygon data fetch ────────────────────────────────────────────────────────

def fetch_return_history(
    ticker:          str,
    polygon_api_key: str,
    days:            int = CORRELATION_DAYS,
) -> List[float]:
    """Fetch daily returns from Polygon.io for the last `days` trading days.

    Uses the /v2/aggs/ticker endpoint (daily OHLCV bars). Computes
    (close_today / close_yesterday) - 1 for each bar pair.

    Args:
        ticker:          Stock ticker symbol (e.g. 'AAPL').
        polygon_api_key: Polygon.io API key.
        days:            Number of calendar days of history to fetch
                         (fetches days+10 to ensure at least `days` trading bars).

    Returns:
        List of daily return floats (e.g. [0.012, -0.005, ...]). Returns an
        empty list if the API call fails or no data is available.

    Raises:
        Never — all exceptions are caught and logged; returns [] on any failure.
    """
    if not polygon_api_key:
        logger.warning("fetch_return_history: no Polygon API key — skipping %s", ticker)
        return []

    from datetime import date, timedelta
    end_date   = date.today()
    start_date = end_date - timedelta(days=days + 10)  # extra buffer for weekends/holidays

    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker.upper()}/range/1/day"
        f"/{start_date}/{end_date}"
        f"?adjusted=true&sort=asc&limit={days + 20}&apiKey={polygon_api_key}"
    )

    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            logger.warning(
                "fetch_return_history: Polygon returned %d for %s",
                resp.status_code, ticker,
            )
            return []

        data    = resp.json()
        results = data.get("results", [])
        if not results:
            logger.warning("fetch_return_history: no bars returned for %s", ticker)
            return []

        closes  = [bar["c"] for bar in results if "c" in bar]
        if len(closes) < 2:
            logger.warning("fetch_return_history: insufficient close prices for %s", ticker)
            return []

        # Daily returns: (today / yesterday) - 1
        returns = [
            (closes[i] / closes[i - 1]) - 1.0
            for i in range(1, len(closes))
        ]
        logger.info("fetch_return_history: %s → %d daily returns", ticker, len(returns))
        return returns

    except requests.exceptions.Timeout:
        logger.warning("fetch_return_history: timeout for %s", ticker)
        return []
    except Exception as exc:
        logger.warning("fetch_return_history: error for %s: %s", ticker, exc)
        return []


# ── Telegram alert ────────────────────────────────────────────────────────────

def _send_block_alert(
    new_ticker:         str,
    max_correlation:    float,
    correlated_ticker:  str,
    telegram_bot_token: str,
    telegram_chat_id:   str,
) -> None:
    """Send Telegram DM to Ahmed when BLOCK is triggered (corr ≥ 0.95).

    Args:
        new_ticker:         The incoming ticker that was blocked.
        max_correlation:    The correlation value that triggered the block.
        correlated_ticker:  The existing position ticker that was highly correlated.
        telegram_bot_token: Telegram bot token.
        telegram_chat_id:   Ahmed's Telegram chat ID.
    """
    if not telegram_bot_token or not telegram_chat_id:
        logger.warning("_send_block_alert: missing telegram credentials — skipping alert")
        return

    message = (
        f"🚫 CORRELATION BLOCK\n"
        f"Ticker: {new_ticker} BLOCKED\n"
        f"Reason: {max_correlation:.3f} correlation with open position {correlated_ticker}\n"
        f"Threshold: ≥0.95 → BLOCK\n"
        f"Action: Trade skipped — portfolio concentration too high"
    )

    url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": telegram_chat_id, "text": message},
            timeout=8,
        )
        if resp.status_code != 200:
            logger.warning("_send_block_alert: Telegram returned %d", resp.status_code)
    except Exception as exc:
        logger.warning("_send_block_alert: failed to send Telegram alert: %s", exc)


# ── Gate decision logic ────────────────────────────────────────────────────────

def _decide(max_corr: float) -> tuple[str, float]:
    """Return (gate_decision, size_adjustment) for a max correlation value.

    Args:
        max_corr: Maximum pairwise Pearson correlation found.

    Returns:
        Tuple of (gate_decision, size_adjustment).
    """
    if max_corr >= _THRESH_BLOCK:
        return "BLOCK", 0.0
    if max_corr >= _THRESH_REDUCE_HEAVY:
        return "REDUCE", 0.50
    if max_corr >= _THRESH_REDUCE_LIGHT:
        return "REDUCE", 0.75
    return "APPROVE", 1.0


# ── Main gate function ────────────────────────────────────────────────────────

def evaluate_correlation_gate(
    new_ticker:            str,
    direction:             str,
    open_position_tickers: List[str],
    db_path:               str,
    polygon_api_key:       str,
    telegram_bot_token:    str,
    telegram_chat_id:      str,
) -> CorrelationResult:
    """Evaluate correlation between new_ticker and all open positions.

    Returns a CorrelationResult with gate_decision and size_adjustment.

    Decision table:
        max_corr < 0.60          → APPROVE 1.0x (full size)
        0.60 <= max_corr < 0.75  → APPROVE 1.0x (log only)
        0.75 <= max_corr < 0.85  → REDUCE  0.75x
        0.85 <= max_corr < 0.95  → REDUCE  0.50x
        max_corr >= 0.95         → BLOCK   0.0x — alert Ahmed

    Failure modes:
        - Return history unavailable → 0.5 proxy, estimated=True
        - No open positions → APPROVE full size immediately
        - Computation error → APPROVE 0.5x conservatively, error field set

    Args:
        new_ticker:            Incoming ticker to evaluate.
        direction:             Trade direction ('bullish' or 'bearish').
        open_position_tickers: List of tickers for existing open positions.
        db_path:               SQLite DB path for logging correlation records.
        polygon_api_key:       Polygon.io API key.
        telegram_bot_token:    Telegram bot token (for BLOCK alerts).
        telegram_chat_id:      Ahmed's chat ID (for BLOCK alerts).

    Returns:
        CorrelationResult dataclass with all evaluation details.
    """
    ticker_upper = new_ticker.upper()
    filtered     = [t for t in open_position_tickers if t.upper() != ticker_upper]

    # Fast path: no open positions → APPROVE immediately, no Polygon needed
    if not filtered:
        logger.info(
            "correlation_gate: %s — no open positions to compare → APPROVE 1.0x",
            ticker_upper,
        )
        result = CorrelationResult(
            gate_decision      = "APPROVE",
            size_adjustment    = 1.0,
            max_correlation    = 0.0,
            existing_tickers   = [],
            correlation_matrix = {},
        )
        _log_correlation_record(
            db_path, ticker_upper, [], 0.0, "APPROVE", 1.0, {}
        )
        return result

    # Fetch return history for the incoming ticker
    try:
        returns_new = fetch_return_history(ticker_upper, polygon_api_key)
        if not returns_new:
            logger.warning(
                "correlation_gate: %s — return history unavailable, using 0.5 proxy",
                ticker_upper,
            )
            # Conservative proxy: assume 0.5 correlation with all open positions
            gate, adj = _decide(_ESTIMATED_CORR)
            corr_matrix = {t: _ESTIMATED_CORR for t in filtered}
            _log_correlation_record(
                db_path, ticker_upper, filtered, _ESTIMATED_CORR, gate, adj, corr_matrix
            )
            return CorrelationResult(
                gate_decision      = gate,
                size_adjustment    = adj,
                max_correlation    = _ESTIMATED_CORR,
                existing_tickers   = filtered,
                correlation_matrix = corr_matrix,
                estimated          = True,
            )

        # Compute pairwise correlations against all open positions
        corr_matrix: dict = {}
        any_estimated       = False
        max_corr            = 0.0
        max_corr_ticker     = ""

        for existing_ticker in filtered:
            returns_existing = fetch_return_history(existing_ticker, polygon_api_key)

            if not returns_existing:
                logger.warning(
                    "correlation_gate: %s — no history for open position %s, using 0.5 proxy",
                    ticker_upper, existing_ticker,
                )
                corr_val  = _ESTIMATED_CORR
                any_estimated = True
            else:
                corr_val = compute_correlation(returns_new, returns_existing)

            corr_abs = abs(corr_val)   # use absolute value — negative corr is also a risk
            corr_matrix[existing_ticker] = round(corr_val, 6)

            if corr_abs > max_corr:
                max_corr        = corr_abs
                max_corr_ticker = existing_ticker

        gate, adj = _decide(max_corr)

        logger.info(
            "correlation_gate: %s (dir=%s) max_corr=%.3f vs %s → %s %.2fx",
            ticker_upper, direction, max_corr, max_corr_ticker, gate, adj,
        )

        # BLOCK: alert Ahmed via Telegram immediately
        if gate == "BLOCK":
            logger.warning(
                "correlation_gate: BLOCK triggered for %s — max_corr=%.3f with %s",
                ticker_upper, max_corr, max_corr_ticker,
            )
            _send_block_alert(
                ticker_upper, max_corr, max_corr_ticker,
                telegram_bot_token, telegram_chat_id,
            )
        elif max_corr >= 0.60:
            logger.info(
                "correlation_gate: %s elevated correlation (%.3f) with %s — logged",
                ticker_upper, max_corr, max_corr_ticker,
            )

        _log_correlation_record(
            db_path, ticker_upper, filtered, max_corr, gate, adj, corr_matrix
        )

        return CorrelationResult(
            gate_decision      = gate,
            size_adjustment    = adj,
            max_correlation    = max_corr,
            existing_tickers   = filtered,
            correlation_matrix = corr_matrix,
            estimated          = any_estimated,
        )

    except Exception as exc:
        # Catch-all: never let a correlation failure block the execution pipeline
        logger.error(
            "correlation_gate: unexpected error for %s: %s — APPROVE at 0.5x conservatively",
            ticker_upper, exc,
        )
        return CorrelationResult(
            gate_decision      = "APPROVE",
            size_adjustment    = 0.50,
            max_correlation    = 0.0,
            existing_tickers   = filtered,
            correlation_matrix = {},
            error              = str(exc)[:300],
        )
