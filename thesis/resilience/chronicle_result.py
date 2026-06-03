"""
resilience/chronicle_result.py — CHRONICLE Sentinel Promotion (B4).
Spec: THESIS_RESILIENCE_SPEC v1.0 — Block 4

Distinguishes "not found" (ok=True, value=None) from "error" (ok=False).
Prevents runaway thesis regeneration when CHRONICLE is locked/corrupted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass
class ChronicleResult(Generic[T]):
    """
    Typed result wrapper for all ChronicleClient method returns.

    Attributes:
        value:  The query result. None if not found OR if ok=False.
        ok:     True = query succeeded (value may still be None = not found).
                False = query failed (DB error, locked, etc.).
        error:  Error message when ok=False.
    """
    value: Optional[T]
    ok:    bool
    error: Optional[str] = None

    @classmethod
    def found(cls, value: T) -> "ChronicleResult[T]":
        """Convenience: successful query with a value."""
        return cls(value=value, ok=True)

    @classmethod
    def not_found(cls) -> "ChronicleResult[T]":
        """Convenience: successful query, no record found."""
        return cls(value=None, ok=True)

    @classmethod
    def failed(cls, error: str) -> "ChronicleResult[T]":
        """Convenience: query failed (DB error)."""
        return cls(value=None, ok=False, error=error)
