"""
regime.py — Market Regime Classification

v3 (legacy): VIX-only classification via classify_regime(vix).
v4 (current): 4-factor composite via classify_regime_v2(macro_data).

Factors: VIX (25pts) + HY credit spread (25pts) + yield curve shape (25pts)
         + put/call ratio (25pts) = composite score 0–100 → 6 regime levels.

Falls back to classify_regime(vix) when ORACLE macro data is unavailable.
Single source of truth for regime logic — referenced by all services.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("axiom.regime")


@dataclass(frozen=True)
class Regime:
    """Immutable regime snapshot — v4 with composite score fields."""

    # ── Core (v3 — unchanged) ──────────────────────────────────────────────────
    vix: float
    classification: str      # LOW_VOL, NORMAL, ELEVATED, STRESS, HIGH_STRESS, CRISIS
    strategy_bias: str       # human-readable guidance
    alpha_credit_allowed: bool
    alpha_debit_allowed: bool
    prime_allowed: bool
    alpha_credit_daily_cap: Optional[int]   # None = no cap
    prime_daily_cap: Optional[int]          # None = no cap
    alpha_size_mult: float                  # sizing multiplier for Alpha
    is_estimated: bool                      # True if VIX was estimated (FRED down)

    # ── New (v4) ───────────────────────────────────────────────────────────────
    composite_score: int = 0                # 0–100 composite stress score
    hy_spread_bps: Optional[float] = None   # HY credit spread in basis points
    yield_curve_bps: Optional[float] = None # 10Y–2Y spread in basis points
    put_call_ratio: Optional[float] = None  # CBOE equity put/call ratio
    regime_source: str = "VIX_ONLY_FALLBACK"  # "COMPOSITE" or "VIX_ONLY_FALLBACK"

    def to_dict(self) -> dict:
        """Serialize regime to dict for API responses and DB storage."""
        return {
            # v3 fields (unchanged — backward compatible)
            "vix":                    self.vix,
            "classification":         self.classification,
            "strategy_bias":          self.strategy_bias,
            "alpha_credit_allowed":   self.alpha_credit_allowed,
            "alpha_debit_allowed":    self.alpha_debit_allowed,
            "prime_allowed":          self.prime_allowed,
            "alpha_credit_daily_cap": self.alpha_credit_daily_cap,
            "prime_daily_cap":        self.prime_daily_cap,
            "alpha_size_mult":        self.alpha_size_mult,
            "is_estimated":           self.is_estimated,
            # v4 fields (new)
            "composite_score":        self.composite_score,
            "hy_spread_bps":          self.hy_spread_bps,
            "yield_curve_bps":        self.yield_curve_bps,
            "put_call_ratio":         self.put_call_ratio,
            "regime_source":          self.regime_source,
        }


def classify_regime(vix: float, is_estimated: bool = False) -> Regime:
    """
    Classify market regime based on VIX level.

    Thresholds approved by Ahmed Sadek — April 10, 2026.
    See NEXUS_MASTER_ARCHITECTURE.md for full VIX regime table.

    Args:
        vix:          Current VIX level.
        is_estimated: True if VIX was estimated due to FRED being unavailable.

    Returns:
        Regime dataclass with all allowed flags set.
    """
    if vix < 12:
        return Regime(
            vix                    = vix,
            classification         = "LOW_VOL",
            strategy_bias          = "Low volatility — reduce size 25%, snap-back risk",
            alpha_credit_allowed   = True,
            alpha_debit_allowed    = True,
            prime_allowed          = True,
            alpha_credit_daily_cap = None,
            prime_daily_cap        = None,
            alpha_size_mult        = 0.75,
            is_estimated           = is_estimated,
        )

    elif vix < 20:
        return Regime(
            vix                    = vix,
            classification         = "NORMAL",
            strategy_bias          = "Balanced — credit and debit spreads both viable",
            alpha_credit_allowed   = True,
            alpha_debit_allowed    = True,
            prime_allowed          = True,
            alpha_credit_daily_cap = None,
            prime_daily_cap        = None,
            alpha_size_mult        = 1.0,
            is_estimated           = is_estimated,
        )

    elif vix < 25:
        return Regime(
            vix                    = vix,
            classification         = "ELEVATED",
            strategy_bias          = "Elevated volatility — all strategies viable, monitor closely",
            alpha_credit_allowed   = True,
            alpha_debit_allowed    = True,
            prime_allowed          = True,
            alpha_credit_daily_cap = None,
            prime_daily_cap        = None,
            alpha_size_mult        = 1.0,
            is_estimated           = is_estimated,
        )

    elif vix < 30:
        return Regime(
            vix                    = vix,
            classification         = "STRESS",
            strategy_bias          = "Market stress — credit spreads only, Prime capped at 3/day",
            alpha_credit_allowed   = True,
            alpha_debit_allowed    = False,     # PAUSED
            prime_allowed          = True,
            alpha_credit_daily_cap = None,
            prime_daily_cap        = 3,
            alpha_size_mult        = 1.0,
            is_estimated           = is_estimated,
        )

    elif vix < 35:
        return Regime(
            vix                    = vix,
            classification         = "HIGH_STRESS",
            strategy_bias          = "High stress — credit spreads capped 2/day, Prime paused",
            alpha_credit_allowed   = True,
            alpha_debit_allowed    = False,     # HALTED
            prime_allowed          = False,     # PAUSED
            alpha_credit_daily_cap = 2,
            prime_daily_cap        = 0,
            alpha_size_mult        = 0.75,
            is_estimated           = is_estimated,
        )

    else:  # vix >= 35
        return Regime(
            vix                    = vix,
            classification         = "CRISIS",
            strategy_bias          = "CRISIS — all new entries halted, manage existing only",
            alpha_credit_allowed   = False,     # HALTED
            alpha_debit_allowed    = False,     # HALTED
            prime_allowed          = False,     # HALTED
            alpha_credit_daily_cap = 0,
            prime_daily_cap        = 0,
            alpha_size_mult        = 0.0,
            is_estimated           = is_estimated,
        )


def classify_regime_v2(macro_data: Dict[str, Any]) -> Regime:
    """
    Classify market regime using 4-factor composite score (v4).

    Factors (0–25 pts each, total 0–100):
      - VIX level
      - HY/IG credit spread (FRED BAMLH0A0HYM2)
      - Yield curve shape (10Y – 2Y spread)
      - Put/call ratio (CBOE equity P/C)

    Composite score → 6 regime levels identical to v3.
    Falls back to classify_regime(vix) if macro_data is missing critical fields.

    Args:
        macro_data: Dict from ORACLE Macro Engine. Expected keys:
                    vix, hy_spread, yield_spread_bps, put_call_ratio (all optional).

    Returns:
        Regime dataclass with composite_score and all v4 fields populated.
    """
    if not macro_data:
        logger.warning("classify_regime_v2 received empty macro_data — using VIX-only fallback")
        fallback_vix = 20.0
        return classify_regime(fallback_vix, is_estimated=True)

    # ── Extract raw values ─────────────────────────────────────────────────────
    vix: float = float(macro_data.get("vix") or 20.0)
    hy_spread: Optional[float] = _to_float(macro_data.get("hy_spread_bps") or macro_data.get("hy_spread"))
    yield_curve: Optional[float] = _to_float(macro_data.get("yield_spread_bps") or macro_data.get("yield_curve_bps"))
    put_call: Optional[float] = _to_float(macro_data.get("put_call_ratio"))
    is_estimated: bool = bool(macro_data.get("is_estimated", False))

    # ── Sub-scores ────────────────────────────────────────────────────────────
    vix_pts = _score_vix(vix)
    hy_pts = _score_hy_spread(hy_spread) if hy_spread is not None else 6
    curve_pts = _score_yield_curve(yield_curve) if yield_curve is not None else 6
    pc_pts = _score_put_call(put_call) if put_call is not None else 6  # stub default: 6

    composite = vix_pts + hy_pts + curve_pts + pc_pts

    # ── Regime classification from composite ──────────────────────────────────
    classification, strategy_bias = _composite_to_regime(composite)

    # ── Build Regime using same allowed-flags logic as v3 ────────────────────
    base = _build_regime_flags(classification, vix, is_estimated)

    return Regime(
        vix=vix,
        classification=classification,
        strategy_bias=strategy_bias,
        alpha_credit_allowed=base["alpha_credit_allowed"],
        alpha_debit_allowed=base["alpha_debit_allowed"],
        prime_allowed=base["prime_allowed"],
        alpha_credit_daily_cap=base["alpha_credit_daily_cap"],
        prime_daily_cap=base["prime_daily_cap"],
        alpha_size_mult=base["alpha_size_mult"],
        is_estimated=is_estimated,
        composite_score=composite,
        hy_spread_bps=hy_spread,
        yield_curve_bps=yield_curve,
        put_call_ratio=put_call,
        regime_source="COMPOSITE",
    )


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _to_float(val: Any) -> Optional[float]:
    """Safely convert a value to float, returning None on failure."""
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _score_vix(vix: float) -> int:
    """VIX sub-score: 0–25 pts."""
    if vix < 12:   return 0
    if vix < 20:   return 6
    if vix < 25:   return 12
    if vix < 30:   return 17
    if vix < 35:   return 22
    return 25


def _score_hy_spread(bps: float) -> int:
    """HY credit spread sub-score: 0–25 pts."""
    if bps < 300:  return 0
    if bps < 400:  return 6
    if bps < 500:  return 12
    if bps < 600:  return 17
    if bps < 700:  return 22
    return 25


def _score_yield_curve(bps: float) -> int:
    """Yield curve (10Y–2Y) sub-score: 0–25 pts. Inversion = stress signal."""
    if bps > 100:   return 0
    if bps >= 50:   return 6
    if bps >= 0:    return 12
    if bps >= -50:  return 17
    if bps >= -100: return 22
    return 25


def _score_put_call(ratio: float) -> int:
    """Put/call ratio sub-score: 0–25 pts."""
    if ratio < 0.60:   return 6   # complacency is also a risk signal
    if ratio < 0.80:   return 0
    if ratio < 1.00:   return 6
    if ratio < 1.20:   return 12
    if ratio < 1.40:   return 17
    return 25


def _composite_to_regime(score: int) -> tuple:
    """
    Map composite score (0–100) to (classification, strategy_bias).

    Returns:
        Tuple of (classification_str, strategy_bias_str).
    """
    if score <= 15:
        return ("LOW_VOL", "Low volatility — reduce size 25%, snap-back risk")
    if score <= 30:
        return ("NORMAL", "Balanced — credit and debit spreads both viable")
    if score <= 45:
        return ("ELEVATED", "Elevated stress — all strategies viable, monitor closely")
    if score <= 60:
        return ("STRESS", "Market stress — credit spreads only, Prime capped at 3/day")
    if score <= 75:
        return ("HIGH_STRESS", "High stress — credit spreads capped 2/day, Prime paused")
    return ("CRISIS", "CRISIS — all new entries halted, manage existing only")


def _build_regime_flags(classification: str, vix: float, is_estimated: bool) -> dict:
    """
    Build the trading-allowed flags for a given classification.

    Mirrors the v3 classify_regime() flag logic in dict form.
    Used by classify_regime_v2() to avoid duplicating flag logic.

    Args:
        classification: Regime classification string.
        vix:            Current VIX (used for alpha_size_mult in LOW_VOL).
        is_estimated:   Whether VIX was estimated.

    Returns:
        Dict with all flag fields.
    """
    if classification == "LOW_VOL":
        return dict(alpha_credit_allowed=True, alpha_debit_allowed=True,
                    prime_allowed=True, alpha_credit_daily_cap=None,
                    prime_daily_cap=None, alpha_size_mult=0.75)
    if classification == "NORMAL":
        return dict(alpha_credit_allowed=True, alpha_debit_allowed=True,
                    prime_allowed=True, alpha_credit_daily_cap=None,
                    prime_daily_cap=None, alpha_size_mult=1.0)
    if classification == "ELEVATED":
        return dict(alpha_credit_allowed=True, alpha_debit_allowed=True,
                    prime_allowed=True, alpha_credit_daily_cap=None,
                    prime_daily_cap=None, alpha_size_mult=1.0)
    if classification == "STRESS":
        return dict(alpha_credit_allowed=True, alpha_debit_allowed=False,
                    prime_allowed=True, alpha_credit_daily_cap=None,
                    prime_daily_cap=3, alpha_size_mult=1.0)
    if classification == "HIGH_STRESS":
        return dict(alpha_credit_allowed=True, alpha_debit_allowed=False,
                    prime_allowed=False, alpha_credit_daily_cap=2,
                    prime_daily_cap=0, alpha_size_mult=0.75)
    # CRISIS
    return dict(alpha_credit_allowed=False, alpha_debit_allowed=False,
                prime_allowed=False, alpha_credit_daily_cap=0,
                prime_daily_cap=0, alpha_size_mult=0.0)


def regime_allows_any_trading(regime: Regime) -> bool:
    """
    Return True if at least one system is allowed to trade in this regime.

    Args:
        regime: Current market regime.

    Returns:
        True if any trading is permitted.
    """
    return regime.alpha_credit_allowed or regime.alpha_debit_allowed or regime.prime_allowed
