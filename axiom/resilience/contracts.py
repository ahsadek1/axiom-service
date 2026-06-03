"""
contracts.py — Axiom-specific data contract validators.
Spec: AXIOM RESILIENCE LAYER 30% v1.0 (Cipher, 2026-05-02)

Rule: Every external API response that flows into a risk score MUST pass
through one of these validators before reaching risk_engine.py.
DataContractError is never silently swallowed — log or re-raise.

All validators raise DataContractError (from shared.resilience.contracts)
with a structured source/field/reason/raw payload. Callers get full
diagnostic context at the point of failure, not downstream in scoring.
"""

from __future__ import annotations

import re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.resilience.contracts import (
    DataContractError,
    require_float,
    require_int,
    require_str,
)

# 1–5 uppercase letters, no digits, no specials
_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")


# ---------------------------------------------------------------------------
# validate_polygon_quote
# ---------------------------------------------------------------------------

def validate_polygon_quote(data: dict, ticker: str) -> dict:
    """
    Validate a Polygon options quote dict before it enters scoring.

    Required fields:
        open_interest  — float >= 0  (null OI is a hard error — not zero-filled)
        volume         — float >= 0
        bid            — float >= 0
        ask            — float >= 0, <= 100_000

    Args:
        data   — raw dict from Polygon API or data_sources layer
        ticker — ticker symbol (for error context only)

    Returns:
        Validated dict with all four fields coerced to float.

    Raises:
        DataContractError — on any missing/invalid field.
    """
    source = f"polygon:{ticker}"
    return {
        **data,
        "open_interest": require_float(
            data.get("open_interest"), source, "open_interest", min_val=0.0
        ),
        "volume": require_float(
            data.get("volume"), source, "volume", min_val=0.0
        ),
        "bid": require_float(
            data.get("bid"), source, "bid", min_val=0.0
        ),
        "ask": require_float(
            data.get("ask"), source, "ask", min_val=0.0, max_val=100_000.0
        ),
    }


# ---------------------------------------------------------------------------
# validate_orats_summary
# ---------------------------------------------------------------------------

def validate_orats_summary(data: dict, ticker: str) -> dict:
    """
    Validate an ORATS summary dict before it enters IV rank scoring.

    Required fields:
        rip   — IVR/IV Rank, float [0, 100]. Null IVR is a HARD ERROR.
                Never silently substitute a default of 50 — that masks outages.
        iv30  — 30-day IV, float [0, 5.0].  Values above 500% are bad data.

    Args:
        data   — raw dict from ORATS API
        ticker — ticker symbol (for error context only)

    Returns:
        Validated dict with both fields coerced to float.

    Raises:
        DataContractError — on any missing/invalid field.
    """
    source = f"orats:{ticker}"
    return {
        **data,
        "rip": require_float(
            data.get("rip"), source, "rip", min_val=0.0, max_val=100.0
        ),
        "iv30": require_float(
            data.get("iv30"), source, "iv30", min_val=0.0, max_val=5.0
        ),
    }


# ---------------------------------------------------------------------------
# validate_alpaca_position
# ---------------------------------------------------------------------------

def validate_alpaca_position(pos: dict) -> dict:
    """
    Validate an Alpaca position dict (from /v2/positions or account snapshot).

    Required fields:
        market_value — float (any value — short positions are negative)
        symbol       — non-empty string

    Args:
        pos — raw position dict from Alpaca API

    Returns:
        Validated dict with market_value coerced to float.

    Raises:
        DataContractError — on missing market_value or empty/missing symbol.
    """
    source = "alpaca:position"

    symbol = pos.get("symbol")
    if not symbol:
        raise DataContractError(source, "symbol", "missing or empty", raw=symbol)
    validated_symbol = require_str(symbol, source, "symbol")

    return {
        **pos,
        "market_value": require_float(pos.get("market_value"), source, "market_value"),
        "symbol": validated_symbol,
    }


# ---------------------------------------------------------------------------
# validate_assess_request
# ---------------------------------------------------------------------------

def validate_assess_request(body: dict) -> dict:
    """
    Validate inbound /assess request parameters before they reach risk_engine.

    Required:
        ticker — 1–5 uppercase letters (e.g. "AAPL", "SPY", "GOOGL")

    Optional (validated only if present):
        dte           — int [0, 365]
        ivr           — float [0, 100]
        proposed_usd  — float [0, 1_000_000]

    Args:
        body — parsed request dict (from Pydantic model or raw dict)

    Returns:
        Validated dict. Optional fields present in input are coerced and included.
        Optional fields absent from input are not added.

    Raises:
        DataContractError — on any invalid field.
    """
    source = "assess:request"
    result = dict(body)

    # Required
    ticker = require_str(body.get("ticker"), source, "ticker")
    if not _TICKER_RE.match(ticker):
        raise DataContractError(
            source, "ticker",
            "must be 1-5 uppercase letters",
            raw=ticker,
        )
    result["ticker"] = ticker

    # Optional
    if body.get("dte") is not None:
        result["dte"] = require_int(body["dte"], source, "dte", min_val=0, max_val=365)

    if body.get("ivr") is not None:
        result["ivr"] = require_float(body["ivr"], source, "ivr", min_val=0.0, max_val=100.0)

    if body.get("proposed_usd") is not None:
        result["proposed_usd"] = require_float(
            body["proposed_usd"], source, "proposed_usd",
            min_val=0.0, max_val=1_000_000.0,
        )

    return result
