"""
resilience/fallback_thesis.py — Fallback Thesis Management (B5).
Spec: THESIS_RESILIENCE_SPEC v1.0 — Block 5

Writes fallback thesis on every successful weekly generation.
Reads fallback when weekly generation fails — provides NEUTRAL posture.
Hardcoded DEFENSIVE is the last resort when no fallback exists.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("thesis.resilience.fallback")

FALLBACK_PATH = Path(
    os.environ.get("THESIS_FALLBACK_PATH", "/Users/ahs/thesis/data/thesis_fallback.json")
)
FALLBACK_VALID_DAYS = 7  # fallback is valid for 7 days


@dataclass
class FallbackThesis:
    """
    Minimal fallback thesis for use when full generation fails.

    Attributes:
        posture:      TradingPosture label.
        generated_at: ISO timestamp of when the fallback was written.
        source:       "fallback" | "hardcoded_defensive"
        reason:       Why this fallback was triggered.
        valid_until:  ISO timestamp when this fallback expires.
    """
    posture:      str
    generated_at: str
    source:       str
    reason:       str
    valid_until:  str

    def is_expired(self) -> bool:
        """Return True if this fallback is past its valid_until date."""
        try:
            expiry = datetime.fromisoformat(self.valid_until)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) > expiry
        except Exception:
            return True  # conservative: treat unparseable as expired


def write_fallback(posture: str, generated_at: Optional[str] = None) -> None:
    """
    Persist a fallback thesis after successful weekly generation.

    Args:
        posture:      Current trading posture (e.g. "NEUTRAL", "AGGRESSIVE").
        generated_at: ISO timestamp (defaults to now).
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    valid_until = (now + timedelta(days=FALLBACK_VALID_DAYS)).isoformat()

    fallback = {
        "posture":      posture,
        "generated_at": generated_at or now.isoformat(),
        "source":       "fallback",
        "reason":       "Written after successful weekly generation",
        "valid_until":  valid_until,
    }

    try:
        FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        FALLBACK_PATH.write_text(json.dumps(fallback, indent=2))
        logger.info("write_fallback: saved posture=%s valid_until=%s", posture, valid_until)
    except Exception as e:
        logger.error("write_fallback: failed: %s", e)


def load_fallback() -> Optional[FallbackThesis]:
    """
    Load fallback thesis from disk.

    Returns:
        FallbackThesis if file exists and is not expired, None otherwise.
    """
    try:
        if not FALLBACK_PATH.exists():
            return None
        data = json.loads(FALLBACK_PATH.read_text())
        fb = FallbackThesis(**data)
        if fb.is_expired():
            logger.warning("load_fallback: fallback expired — returning None")
            return None
        return fb
    except Exception as e:
        logger.warning("load_fallback: failed: %s", e)
        return None


def get_posture_with_fallback(generation_fn) -> str:
    """
    Run generation_fn; on failure, return fallback posture.

    Falls back in order:
        1. generation_fn() result
        2. load_fallback() (last successful posture, if not expired)
        3. "DEFENSIVE" (hardcoded last resort)

    Args:
        generation_fn: Callable that returns posture string.

    Returns:
        Posture string.
    """
    try:
        return generation_fn()
    except Exception as e:
        logger.error("get_posture_with_fallback: generation failed: %s — checking fallback", e)

    fallback = load_fallback()
    if fallback:
        logger.warning("get_posture_with_fallback: using fallback posture=%s from %s",
                       fallback.posture, fallback.generated_at)
        return fallback.posture

    logger.warning("get_posture_with_fallback: no valid fallback — using DEFENSIVE")
    return "DEFENSIVE"
