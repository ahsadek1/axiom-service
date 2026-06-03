"""
failure.py — G6: AILS Failure Disclosure Contract.

Defines structured error responses for all AILS failure modes.
Callers (ORACLE/OMNI) must check response["status"] == "error" before using data.

Spec: specs/ails-failure-disclosure.md
"""
from typing import Optional


# ── Severity levels ───────────────────────────────────────────────────────────

CRITICAL     = "CRITICAL"       # DB down, retry may succeed
DEGRADED     = "DEGRADED"       # Partial failure, fallback used
MISSING_DATA = "MISSING_DATA"   # Structurally absent — retry will not help
TIMEOUT      = "TIMEOUT"        # Timed out, retry may succeed

# ── Error type registry ───────────────────────────────────────────────────────

ERROR_TYPES = {
    "TICKER_NOT_IN_BACKTEST": {
        "severity": MISSING_DATA, "recoverable": False,
        "description": "Ticker has no rows in backtest.db",
    },
    "BACKTEST_DB_UNAVAILABLE": {
        "severity": CRITICAL, "recoverable": True,
        "description": "backtest.db file missing or locked",
    },
    "AILS_DB_UNAVAILABLE": {
        "severity": CRITICAL, "recoverable": True,
        "description": "ails.db file missing or locked",
    },
    "REGIME_HISTORY_MISSING": {
        "severity": MISSING_DATA, "recoverable": False,
        "description": "No regime rows for requested date range",
    },
    "LIVE_DB_WRITE_FAIL": {
        "severity": CRITICAL, "recoverable": True,
        "description": "SQLite write error on outcome ingestion",
    },
    "BAYESIAN_UPDATE_ERROR": {
        "severity": DEGRADED, "recoverable": True,
        "description": "Math error during win-rate update",
    },
    "PATTERN_SEARCH_FAIL": {
        "severity": MISSING_DATA, "recoverable": False,
        "description": "Similarity search returned 0 results",
    },
    "AGENT_CALIBRATION_MISSING": {
        "severity": MISSING_DATA, "recoverable": False,
        "description": "No calibration rows for requested agent",
    },
    "DB_CONNECTION_NONE": {
        "severity": CRITICAL, "recoverable": True,
        "description": "DB connection is None — service may still be initializing",
    },
    "UNEXPECTED_ERROR": {
        "severity": CRITICAL, "recoverable": True,
        "description": "Unexpected internal error",
    },
}


def error_response(
    error_type: str,
    endpoint: str,
    reason: str,
    ticker: Optional[str] = None,
    fallback_used: bool = False,
    fallback_source: Optional[str] = None,
) -> dict:
    """
    Build a structured AILS failure response.

    Args:
        error_type: One of the defined error type strings above.
        endpoint: The endpoint path that failed (e.g. '/context/AAPL').
        reason: Human-readable description (max 200 chars, no raw tracebacks).
        ticker: Affected ticker or None for non-ticker errors.
        fallback_used: True if a degraded fallback was substituted.
        fallback_source: Description of the fallback used, or None.

    Returns:
        dict with status="error" and all required G6 fields.
    """
    meta = ERROR_TYPES.get(error_type, ERROR_TYPES["UNEXPECTED_ERROR"])
    return {
        "status":          "error",
        "error_type":      error_type,
        "ticker":          ticker,
        "endpoint":        endpoint,
        "reason":          str(reason)[:200],   # never expose raw tracebacks
        "severity":        meta["severity"],
        "recoverable":     meta["recoverable"],
        "fallback_used":   fallback_used,
        "fallback_source": fallback_source,
        "data":            None,
    }
