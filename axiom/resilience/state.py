"""
state.py — Axiom-specific FreshValue wrappers.
Spec: AXIOM_30_SPEC v1.0 (Cipher, 2026-05-02)

Wraps shared.resilience.state.FreshValue with Axiom-specific named instances
for VIX and regime caches. Additive only — no existing code touched.

TTLs:
- VIX:    12 minutes (scheduler fetches every 5-15 min; 12 min = fetch interval + buffer)
- Regime: 20 minutes (regime updated at 8:45, 9:15, then hourly)
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from shared.resilience.state import FreshValue, StaleStateError  # noqa: F401 — re-export

from datetime import timedelta

VIX_MAX_AGE = timedelta(minutes=12)
REGIME_MAX_AGE = timedelta(minutes=20)


def make_vix_cache() -> FreshValue:
    """
    Create a VIX FreshValue with 12-min TTL.

    Call at startup:
        app_state["_vix_cache"] = make_vix_cache()

    On VIX update (scheduler + startup):
        app_state["_vix_cache"].update(vix_value)
        app_state["last_vix"] = vix_value   # keep legacy field for backward compat

    In /assess — advisory stale check (log only, does NOT replace existing gates):
        vix_cache = app_state.get("_vix_cache")
        if vix_cache:
            fresh_vix = vix_cache.get()   # None if stale
            if fresh_vix is None:
                logger.warning("VIX cache stale — using last_vix fallback")
    """
    return FreshValue(None, max_age=VIX_MAX_AGE, label="vix")


def make_regime_cache() -> FreshValue:
    """
    Create a regime FreshValue with 20-min TTL.

    Call at startup:
        app_state["_regime_cache"] = make_regime_cache()

    On regime update:
        app_state["_regime_cache"].update(regime_obj)
        app_state["regime"] = regime_obj   # keep legacy field for backward compat

    IMPORTANT: Do NOT replace the existing `regime is None` hard-stop check.
    The stale check is additive — log warning only, never downgrade safety gates.
    """
    return FreshValue(None, max_age=REGIME_MAX_AGE, label="regime")


class AxiomState:
    """
    Convenience wrapper that holds both VIX and regime caches.

    Usage:
        state = AxiomState()
        app_state["_axiom_state"] = state

        # On update
        state.update_vix(14.3)
        state.update_regime({"classification": "NORMAL"})

        # On read
        vix = state.vix.get()        # None if stale
        regime = state.regime.get()  # None if stale
    """

    def __init__(self):
        self.vix = make_vix_cache()
        self.regime = make_regime_cache()

    def update_vix(self, value) -> None:
        self.vix.update(value)

    def update_regime(self, value) -> None:
        self.regime.update(value)
