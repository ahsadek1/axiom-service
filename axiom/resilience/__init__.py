"""
Axiom 30% Resilience Layer — agent-specific hardening.
Spec: AXIOM RESILIENCE LAYER 30% v1.0 (Cipher, 2026-05-02)

Wires the shared NEXUS_RESILIENCE_BASE (70%) into Axiom's specific
data flows: Polygon quotes, ORATS summaries, Alpaca positions,
/assess request validation, VIX/regime caches, and health reporting.
"""

from .contracts import (
    validate_polygon_quote,
    validate_orats_summary,
    validate_alpaca_position,
    validate_assess_request,
)
from .alerts import AxiomAlerter
from .state import make_vix_cache, make_regime_cache
from .db import assess_db_write, immediate_transaction
from .health import AxiomHealthReport, AxiomSourceStatus


class AxiomState:
    """
    Namespace for Axiom state factory functions.
    Use make_vix_cache() and make_regime_cache() directly, or via
    AxiomState.make_vix() / AxiomState.make_regime() for clarity.
    """
    make_vix    = staticmethod(make_vix_cache)
    make_regime = staticmethod(make_regime_cache)


__all__ = [
    # contracts
    "validate_polygon_quote",
    "validate_orats_summary",
    "validate_alpaca_position",
    "validate_assess_request",
    # alerts
    "AxiomAlerter",
    # state
    "AxiomState",
    "make_vix_cache",
    "make_regime_cache",
    # db
    "assess_db_write",
    "immediate_transaction",
    # health
    "AxiomHealthReport",
    "AxiomSourceStatus",
]
