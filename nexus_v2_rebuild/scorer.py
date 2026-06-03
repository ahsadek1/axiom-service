"""
scorer.py — Adapter: TickerContext → shared scorer
====================================================
scanner.py calls compute_score_from_context(ctx: TickerContext, direction: str).
Shared scorer expects a dict.
This adapter converts TickerContext → dict and delegates to shared/scorer.py.

Authored: 2026-05-02 | OMNI integration
"""

from __future__ import annotations
import sys
import os
from typing import TYPE_CHECKING

# Add shared/ to path so we can import the shared scorer
_NEXUS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(_NEXUS_ROOT, "shared") not in sys.path:
    sys.path.insert(0, os.path.join(_NEXUS_ROOT, "shared"))

# Load shared/scorer.py via importlib — register in sys.modules so dataclass
# field resolution (which calls sys.modules.get(cls.__module__)) works correctly.
import importlib.util as _ilu
import sys as _sys
_SHARED_SCORER_PATH = os.path.join(_NEXUS_ROOT, "shared", "scorer.py")
_spec = _ilu.spec_from_file_location("_nexus_shared_scorer", _SHARED_SCORER_PATH)
_shared_scorer_mod = _ilu.module_from_spec(_spec)
_sys.modules["_nexus_shared_scorer"] = _shared_scorer_mod  # register BEFORE exec
_spec.loader.exec_module(_shared_scorer_mod)
_compute_score = _shared_scorer_mod.compute_score
ScoreResult = _shared_scorer_mod.ScoreResult

if TYPE_CHECKING:
    from data_contracts import TickerContext


def compute_score_from_context(ctx: "TickerContext", direction: str) -> ScoreResult:
    """
    Adapter: convert TickerContext dataclass → dict and call shared scorer.

    Args:
        ctx:       Fully populated TickerContext from data_fetchers
        direction: "bullish" or "bearish"

    Returns:
        ScoreResult with score (0-100), breakdown, gates_fired, recommendation
    """
    context_dict = {
        "vol": {
            "iv_rank":          ctx.vol.iv_rank,
            "iv_percentile":    ctx.vol.iv_percentile,
            "hv_30d":           ctx.vol.hv_30,    # hv_30 in contract, hv_30d in scorer
            "iv_hv_spread":     ctx.vol.iv_hv_spread,
            "contango":         ctx.vol.contango,
            "implied_move":     ctx.vol.implied_move,
            "is_fallback":      ctx.vol.is_fallback,
        },
        "price": {
            "rsi_14":           ctx.tech.rsi_14,
            "last_price":       ctx.tech.last_price,
            "price_vs_20d_ma":  ctx.tech.price_vs_20d_ma,
            "price_vs_50d_ma":  ctx.tech.price_vs_50d_ma,
            "volume_ratio":     ctx.tech.volume_ratio,
            "avg_volume_30d":   ctx.tech.avg_volume_30d,
        },
        "fundamental": {
            "revenue_growth_yoy": ctx.fundamental.revenue_growth_yoy,
            "earnings_in_days":   ctx.fundamental.days_to_earnings,  # mapped
            "margin_trend":       ctx.fundamental.margin_trend,
            "analyst_revision_bias": ctx.fundamental.analyst_revision_bias,
            "insider_net_bias":   ctx.fundamental.insider_net_bias,
        },
        "flow": {
            "put_call_ratio":          ctx.flow.put_call_ratio,
            "options_volume_vs_avg":   ctx.flow.options_volume_vs_avg,
            "net_flow_bias":           ctx.flow.net_flow_bias,
            "unusual_activity":        ctx.flow.unusual_activity,
        },
        "gamma": {
            "call_wall":        ctx.gamma.call_wall,
            "put_wall":         ctx.gamma.put_wall,
            "net_gex":          ctx.gamma.net_gex,
            "hiro_signal":      ctx.gamma.hiro_signal,
        },
    }
    return _compute_score(context_dict, direction)
