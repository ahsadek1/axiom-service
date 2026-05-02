"""
contracts.py — Data contract validation for all Nexus agents.
Spec: NEXUS_RESILIENCE_BASE v1.1 (Cipher, 2026-05-02)
Status: Zero conflicts — additive only, no existing code touched.

Rule: Raise at the data boundary. Never swallow silently.
DataContractError is always logged/surfaced by the caller — it is never caught
and suppressed. If a field is wrong, we fail loudly before it reaches scoring
or execution logic.
"""

from __future__ import annotations

import math


class DataContractError(Exception):
    """
    Raised when incoming data violates a field contract.

    Attributes:
        source  — where the data came from (e.g. "orats", "axiom", "alpha")
        field   — the specific field that failed (e.g. "iv_rank", "dte_at_entry")
        reason  — human-readable failure reason
        raw     — the original value that failed validation (for debugging)
    """

    def __init__(self, source: str, field: str, reason: str, raw=None):
        self.source = source
        self.field = field
        self.reason = reason
        self.raw = raw
        super().__init__(f"{source}.{field}: {reason} (raw={raw!r})")


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def require_float(
    value,
    source: str,
    field: str,
    min_val: float | None = None,
    max_val: float | None = None,
) -> float:
    """
    Validate and coerce *value* to float.

    Args:
        value    — raw input (may be None, str, int, float)
        source   — data source label (for error messages)
        field    — field name (for error messages)
        min_val  — optional inclusive lower bound
        max_val  — optional inclusive upper bound

    Returns:
        float

    Raises:
        DataContractError — on None, non-numeric, or out-of-range

    Example:
        ivr = require_float(raw.get("rip"), source="orats",
                            field="iv_rank", min_val=0.0, max_val=100.0)
    """
    if value is None:
        raise DataContractError(source, field, "missing")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise DataContractError(source, field, "not numeric", raw=value)
    if math.isnan(v) or math.isinf(v):
        raise DataContractError(source, field, "NaN/Inf not allowed", raw=value)
    if min_val is not None and v < min_val:
        raise DataContractError(source, field, f"below minimum {min_val}", raw=v)
    if max_val is not None and v > max_val:
        raise DataContractError(source, field, f"above maximum {max_val}", raw=v)
    return v


def require_str(
    value,
    source: str,
    field: str,
    allowed: list[str] | None = None,
) -> str:
    """
    Validate *value* as a non-None string, optionally against an allowlist.

    Args:
        value   — raw input
        source  — data source label
        field   — field name
        allowed — optional list of valid string values

    Returns:
        str

    Raises:
        DataContractError — on None, non-string, or not-in-allowed

    Example:
        strategy = require_str(raw.get("strategy"), source="alpha",
                               field="strategy",
                               allowed=["credit_spread", "debit_spread",
                                        "iron_condor"])
    """
    if value is None:
        raise DataContractError(source, field, "missing")
    if not isinstance(value, str):
        raise DataContractError(source, field, "not a string", raw=value)
    if allowed and value not in allowed:
        raise DataContractError(source, field, f"not in {allowed}", raw=value)
    return value


def require_int(
    value,
    source: str,
    field: str,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int:
    """
    Validate and coerce *value* to int.

    Accepts int or anything int() can parse (string digits, float-that-is-whole).
    Rejects floats that are not whole numbers to prevent silent precision loss.

    Args:
        value    — raw input
        source   — data source label
        field    — field name
        min_val  — optional inclusive lower bound
        max_val  — optional inclusive upper bound

    Returns:
        int

    Raises:
        DataContractError — on None, non-integer, or out-of-range

    Example:
        dte = require_int(raw.get("dte"), source="alpha",
                          field="dte_at_entry", min_val=21, max_val=60)
    """
    if value is None:
        raise DataContractError(source, field, "missing")
    # Reject floats with a fractional part — they are not integers.
    if isinstance(value, float) and not value.is_integer():
        raise DataContractError(source, field, "not an integer", raw=value)
    # C8: handle float strings like "21.0" from real APIs (ORATS, Polygon).
    # int("21.0") raises ValueError — route through float() first for dot-strings,
    # but reject fractional values like "21.5" (not a valid integer).
    try:
        if isinstance(value, str) and "." in value:
            as_float = float(value)
            if not as_float.is_integer():
                raise DataContractError(source, field, "not an integer", raw=value)
            v = int(as_float)
        else:
            v = int(value)
    except DataContractError:
        raise
    except (TypeError, ValueError):
        raise DataContractError(source, field, "not an integer", raw=value)
    if min_val is not None and v < min_val:
        raise DataContractError(source, field, f"below minimum {min_val}", raw=v)
    if max_val is not None and v > max_val:
        raise DataContractError(source, field, f"above maximum {max_val}", raw=v)
    return v
