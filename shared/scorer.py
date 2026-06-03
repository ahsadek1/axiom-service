"""
scorer.py — Nexus Deterministic Scoring Engine
================================================
Pure arithmetic. No LLM. No opinion.

Given an Oracle context packet, produces a score (0-100) from 6 lookup tables.
Same data → same score. Every agent. Every model. Every time.

Architecture:
    1. Hard gates evaluated first. Any trigger caps the final score.
    2. Six dimensions computed from lookup tables.
    3. Final score = sum(dimensions), then hard gate cap applied.
    4. LLM receives the computed score + breakdown → writes reasoning only.

Usage:
    from shared.scorer import compute_score, determine_direction
    
    direction = determine_direction(context)
    result    = compute_score(context, direction)
    # result.score, result.breakdown, result.gates_fired, result.cap_applied
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# ── Data class for score result ────────────────────────────────────────────────

@dataclass
class ScoreResult:
    score: int                          # Final capped score (0-100)
    raw_score: int                      # Score before hard gate cap
    direction: str                      # "bullish" or "bearish"
    strategy_type: str = "CREDIT"       # CREDIT | DEBIT_CALL | DEBIT_PUT | LOW_IV
    breakdown: dict = field(default_factory=dict)   # Per-dimension scores
    gates_fired: list = field(default_factory=list) # Hard gates that triggered
    cap_applied: Optional[int] = None              # Cap value if any gate fired
    recommendation: str = ""                        # EXECUTE / WATCH / REJECT


# ══════════════════════════════════════════════════════════════════════════════
# STEP 0 — DIRECTION DETERMINATION (deterministic, no LLM)
# ══════════════════════════════════════════════════════════════════════════════

def determine_direction(context: dict) -> str:
    """
    Determine trade direction from hard data signals in priority order.
    Returns "bullish" or "bearish". Never None.
    
    Priority:
        1. Options flow (PCR) — strongest directional signal
        2. Net flow bias (from Oracle flow engine)
        3. Price vs 50d MA — medium-term trend
        4. RSI — momentum proxy
        5. Default: bullish (neutral stance)
    """
    flow    = context.get("flow", {}) or {}
    price   = context.get("price", {}) or {}

    pcr        = flow.get("put_call_ratio")
    flow_bias  = str(flow.get("net_flow_bias", "NEUTRAL")).upper()
    price_50   = price.get("price_vs_50d_ma")
    rsi        = price.get("rsi_14")

    # 1. PCR — strongest signal
    if pcr is not None:
        if pcr < 0.65:   return "bullish"
        if pcr > 1.35:   return "bearish"

    # 2. Net flow bias
    if flow_bias == "BULLISH":  return "bullish"
    if flow_bias == "BEARISH":  return "bearish"

    # 3. Price vs 50d MA
    if price_50 is not None:
        if price_50 > 2.0:   return "bullish"
        if price_50 < -2.0:  return "bearish"

    # 4. RSI
    if rsi is not None:
        if rsi > 55:  return "bullish"
        if rsi < 45:  return "bearish"

    return "bullish"  # Neutral default


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — HARD GATES
# Evaluated before scoring. Any trigger caps the final score.
# ══════════════════════════════════════════════════════════════════════════════

def _check_hard_gates(context: dict, direction: str) -> tuple[list[str], Optional[int]]:
    """
    Returns (gates_fired, cap_value).
    cap_value is the lowest cap from all fired gates (most restrictive wins).
    """
    fundamental = context.get("fundamental", {}) or {}
    vol         = context.get("vol", {}) or {}
    macro       = context.get("macro", {}) or {}
    gamma       = context.get("gamma", {}) or {}

    days_to_earnings = fundamental.get("days_to_earnings")
    ivr              = vol.get("iv_rank")
    vix              = macro.get("vix")
    pct_to_wall      = (gamma.get("pct_to_call_wall") if direction == "bullish"
                        else gamma.get("pct_to_put_wall"))

    gates_fired = []
    caps        = []

    # Gate 1: Earnings within 7 days — binary risk event
    if days_to_earnings is not None and days_to_earnings < 7:
        gates_fired.append(
            f"EARNINGS_IMMINENT: {days_to_earnings}d to earnings — hard cap 25"
        )
        caps.append(25)

    # Gate 2: Earnings within 14 days — elevated risk
    elif days_to_earnings is not None and days_to_earnings < 14:
        gates_fired.append(
            f"EARNINGS_NEAR: {days_to_earnings}d to earnings — soft cap 45"
        )
        caps.append(45)

    # Gate 3: IVR critically low — only blocks when IVR is truly illiquid (< 5).
    # Threshold lowered from < 10 to < 5 because IVR 5-15 is post-OPEX IV crush,
    # which is normal and workable for debit plays. The old < 10 threshold was
    # zeroing out all post-OPEX setups by capping at 30. IVR < 5 is genuinely
    # illiquid (options market thin, wide spreads, execution risk). [GENESIS 2026-04-20]
    if ivr is not None and ivr < 5:
        gates_fired.append(
            f"IVR_CRITICAL: IVR={ivr:.1f} — options illiquid, cap 30"
        )
        caps.append(30)

    # Gate 4: VIX extreme — market-wide risk
    if vix is not None and vix > 35:
        gates_fired.append(
            f"VIX_EXTREME: VIX={vix:.1f} — systemic risk, cap 35"
        )
        caps.append(35)

    # Gate 5: Gamma wall immediate — no room to run
    if pct_to_wall is not None and pct_to_wall < 0.75:
        gates_fired.append(
            f"GAMMA_WALL_IMMEDIATE: {pct_to_wall:.1f}% to wall — execution blocked, cap 35"
        )
        caps.append(35)

    cap = min(caps) if caps else None
    return gates_fired, cap


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1b — STRATEGY MODE DETERMINATION
# ══════════════════════════════════════════════════════════════════════════════

def _determine_strategy_mode(context: dict) -> str:
    """
    Determine strategy mode from IVR before scoring dimensions.

    Returns one of: "CREDIT" | "DEBIT" | "LOW_IV"

    DEBIT  (IVR > 80):  Buy directional options — IV is elevated, premium is
                         large, market is expressing strong directional conviction.
                         The old scorer penalized this as "vol risk" for credits.
    LOW_IV (IVR < 15):  Post-OPEX IV crush or genuinely flat environment.
                         Cheap options — debit plays work, but smaller edge.
                         Floor D1 at 5 to prevent the old 0-point cliff.
    CREDIT (15-80):     Standard credit spread territory — unchanged from v1.

    [GENESIS 2026-04-20]
    """
    ivr = (context.get("vol") or {}).get("iv_rank")
    if ivr is None:
        return "CREDIT"  # Unknown → conservative default
    if ivr > 80:
        return "DEBIT"
    if ivr < 15:
        return "LOW_IV"
    return "CREDIT"


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — SIX SCORING DIMENSIONS (lookup tables, pure arithmetic)
# ══════════════════════════════════════════════════════════════════════════════

def _d1_iv_structure(context: dict, strategy_mode: str = "CREDIT") -> int:
    """
    D1 — IV Structure (0-20 pts)
    Source: context["vol"]["iv_rank"]

    Strategy-aware IV scoring. Mode is determined by IVR level before this
    function is called (see _determine_strategy_mode).

    CREDIT mode (IVR 15-80): rewards the credit-spread sweet spot (35-70).
    DEBIT mode (IVR > 80): rewards high IV — favorable for buying directional
        options. The old scorer penalized high IVR as "vol risk" but for debit
        plays, elevated IV means larger premium on the options you're buying AND
        stronger directional conviction from the market.
    LOW_IV mode (IVR < 15): post-OPEX IV crush or genuinely flat vol. Floor at 5
        (was 0) — IV below 10 is undesirable but not zero-conviction. A score of
        0 was causing a 20-point cliff that zeroed out all post-OPEX setups.

    [GENESIS 2026-04-20: D1 strategy-aware rewrite. Previous single-mode
     scorer zeroed D1 for IVR>90 and IVR<10, eliminating high-IVR debit
     opportunities and collapsing all post-OPEX scores to 28-47 range.]
    """
    ivr = (context.get("vol") or {}).get("iv_rank")
    if ivr is None:  return 10  # Unknown — neutral

    if strategy_mode == "DEBIT":
        # High IVR = large premium to buy, strong directional signal — reward it
        if   ivr > 90:             return 20
        elif ivr > 80:             return 17
        elif ivr > 70:             return 14
        else:                      return 10  # Shouldn't reach here; fallback

    if strategy_mode == "LOW_IV":
        # Post-OPEX or genuinely compressed vol. Floor at 5 — still tradeable
        # for debit plays (cheap options), just not ideal.
        if   ivr >= 10:            return 8   # Near-low — passable
        elif ivr >= 5:             return 6
        else:                      return 5   # Floor: never 0 (was the cliff edge)

    # CREDIT mode (default): original logic, IVR 35-70 ideal
    if   40 <= ivr <= 70:  return 20  # Ideal credit zone
    elif 35 <= ivr < 40:   return 17  # Good
    elif 70 < ivr <= 80:   return 15  # Elevated — still workable
    elif 25 <= ivr < 35:   return 12  # Low-moderate
    elif 15 <= ivr < 25:   return 6   # Low — debit-only territory
    elif 10 <= ivr < 15:   return 3   # Very low — near block territory
    else:                   return 5   # < 10 — floor (post-OPEX protection)


def _d2_options_flow(context: dict, direction: str) -> int:
    """
    D2 — Options Flow (0-15 pts)
    Source: context["flow"]["put_call_ratio"] + options_volume_vs_avg
    
    Measures directional conviction from options market participants.
    PCR is scored relative to trade direction (low PCR = bullish conviction).
    """
    flow       = context.get("flow", {}) or {}
    pcr        = flow.get("put_call_ratio")
    vol_vs_avg = flow.get("options_volume_vs_avg", 1.0) or 1.0
    unusual    = flow.get("unusual_activity", False)

    if pcr is None:  return 7  # Unknown — neutral

    # PCR score relative to direction
    if direction == "bullish":
        if   pcr < 0.45:   pcr_pts = 12
        elif pcr < 0.60:   pcr_pts = 9
        elif pcr < 0.75:   pcr_pts = 6
        elif pcr < 0.90:   pcr_pts = 3
        else:               pcr_pts = 0  # Bearish flow vs bullish trade
    else:  # bearish
        if   pcr > 1.50:   pcr_pts = 12
        elif pcr > 1.30:   pcr_pts = 9
        elif pcr > 1.10:   pcr_pts = 6
        elif pcr > 0.90:   pcr_pts = 3
        else:               pcr_pts = 0  # Bullish flow vs bearish trade

    # Volume modifier (0-2 pts)
    vol_pts = 2 if vol_vs_avg >= 2.0 else (1 if vol_vs_avg >= 1.3 else 0)

    # Unusual activity bonus (0-1 pt) — confirms elevated interest
    unusual_pts = 1 if unusual else 0

    return min(15, pcr_pts + vol_pts + unusual_pts)


def _d3_technical_setup(context: dict, direction: str) -> int:
    """
    D3 — Technical Setup (0-20 pts)
    Source: context["price"][rsi_14, price_vs_20d_ma, price_vs_50d_ma, volume_ratio]
    Fallback: context["flow"][net_flow_bias] + context["vol"][put_call_skew]

    Measures price momentum and trend alignment.
    Scored directionally — a bearish setup rewards RSI < 45, below MAs.

    [GENESIS 2026-04-20: Added flow/skew proxy when ORACLE price deriveds are
     missing. Previously returned exactly 8/20 for all tickers with None price
     data (partial credit for unknowns). Now uses net_flow_bias and put_call_skew
     to disambiguate setup quality when RSI/MA are unavailable.]
    """
    price      = context.get("price", {}) or {}
    rsi        = price.get("rsi_14")
    vs_20      = price.get("price_vs_20d_ma")
    vs_50      = price.get("price_vs_50d_ma")
    vol_ratio  = price.get("volume_ratio", 1.0) or 1.0

    price_data_missing = (rsi is None and vs_20 is None and vs_50 is None)

    # RSI component (0-8 pts)
    if rsi is None:
        rsi_pts = 4
    elif direction == "bullish":
        if   55 <= rsi <= 68:  rsi_pts = 8   # Momentum without overbought
        elif 50 <= rsi < 55:   rsi_pts = 5
        elif 68 < rsi <= 75:   rsi_pts = 3   # Getting overbought
        elif 45 <= rsi < 50:   rsi_pts = 2
        else:                   rsi_pts = 0   # Oversold or severely overbought
    else:  # bearish
        if   32 <= rsi <= 45:  rsi_pts = 8   # Momentum without oversold
        elif 45 < rsi <= 50:   rsi_pts = 5
        elif 25 <= rsi < 32:   rsi_pts = 3
        elif 50 < rsi <= 55:   rsi_pts = 2
        else:                   rsi_pts = 0

    # MA alignment (0-8 pts) — 4 pts each MA
    ma_pts = 0
    if vs_20 is not None:
        if direction == "bullish" and vs_20 > 0:   ma_pts += 4
        if direction == "bearish" and vs_20 < 0:   ma_pts += 4
    else:
        ma_pts += 2  # Unknown — partial credit

    if vs_50 is not None:
        if direction == "bullish" and vs_50 > 0:   ma_pts += 4
        if direction == "bearish" and vs_50 < 0:   ma_pts += 4
    else:
        ma_pts += 2

    # Volume confirmation (0-4 pts)
    vol_pts = 4 if vol_ratio >= 1.5 else (2 if vol_ratio >= 1.1 else 0)

    raw_score = rsi_pts + ma_pts + vol_pts

    # Flow/skew proxy when ALL price deriveds are missing (ORACLE gap fix)
    # If RSI and both MAs are None, the raw score is stuck at 4+2+2+0 = 8.
    # Use net_flow_bias and put_call_skew to resolve actual setup quality.
    if price_data_missing:
        flow      = context.get("flow", {}) or {}
        vol_ctx   = context.get("vol", {}) or {}
        flow_bias = str(flow.get("net_flow_bias", "NEUTRAL")).upper()
        skew      = vol_ctx.get("put_call_skew")  # positive = put premium elevated

        # Only engage proxy scoring when at least one signal is directional.
        # flow_bias="NEUTRAL" and skew=None = no proxy data available (ORACLE cold cache).
        # In that case fall back to neutral (8) — same as old partial-credit behavior.
        # Penalizing to 4 when proxy data is absent was a regression. [GENESIS 2026-04-20]
        flow_directional = flow_bias in ("BULLISH", "BEARISH")
        skew_directional = skew is not None

        flow_confirms = (
            flow_directional and (
                (direction == "bullish" and flow_bias == "BULLISH") or
                (direction == "bearish" and flow_bias == "BEARISH")
            )
        )
        skew_confirms = (
            skew_directional and (
                (direction == "bearish" and skew > 0.05) or
                (direction == "bullish" and skew < -0.05)
            )
        )

        if not flow_directional and not skew_directional:
            raw_score = 7   # No proxy data at all (cold cache) — conservative neutral
        elif flow_confirms and skew_confirms:
            raw_score = 12  # Both proxies confirm direction — upgrade
        elif flow_confirms or skew_confirms:
            raw_score = 9   # One confirms — mild upgrade from neutral
        elif flow_directional or skew_directional:
            raw_score = 5   # Proxy data present but contradicts direction — mild penalty
        else:
            raw_score = 7   # Fallback neutral

    return min(20, raw_score)


def _d4_earnings_safety(context: dict) -> int:
    """
    D4 — Earnings Safety (0-15 pts)
    Source: context["fundamental"]["days_to_earnings"]
    
    Measures binary event risk. Hard gates handle extreme cases;
    this dimension scores the gradient of risk.
    """
    fund             = context.get("fundamental", {}) or {}
    days_to_earnings = fund.get("days_to_earnings")

    if days_to_earnings is None:  return 8  # Unknown — cautious neutral

    if   days_to_earnings >= 45:  return 15
    elif days_to_earnings >= 30:  return 12
    elif days_to_earnings >= 21:  return 8
    elif days_to_earnings >= 14:  return 4
    elif days_to_earnings >= 7:   return 1
    else:                          return 0   # < 7d also triggers hard gate


def _d5_fundamental_quality(context: dict) -> int:
    """
    D5 — Fundamental Quality (0-15 pts)
    Source: context["fundamental"][revenue_growth_yoy, margin_trend,
                                   analyst_revision_bias, insider_net_bias]
    
    Measures whether the business quality supports the trade thesis.
    """
    fund              = context.get("fundamental", {}) or {}
    revenue_growth    = fund.get("revenue_growth_yoy")
    margin_trend      = str(fund.get("margin_trend", "FLAT")).upper()
    analyst_revision  = str(fund.get("analyst_revision_bias", "NEUTRAL")).upper()
    insider_bias      = str(fund.get("insider_net_bias", "NEUTRAL")).upper()

    # Revenue growth (0-7 pts)
    if revenue_growth is None:
        rev_pts = 3
    elif revenue_growth >= 20:   rev_pts = 7
    elif revenue_growth >= 10:   rev_pts = 5
    elif revenue_growth >= 5:    rev_pts = 3
    elif revenue_growth >= 0:    rev_pts = 1
    else:                         rev_pts = 0  # Negative growth

    # Margin trend (0-4 pts)
    margin_map = {"EXPANDING": 4, "FLAT": 2, "STABLE": 2, "CONTRACTING": 0}
    margin_pts = margin_map.get(margin_trend, 2)

    # Analyst revision (0-2 pts)
    revision_map = {"POSITIVE": 2, "NEUTRAL": 1, "NEGATIVE": 0}
    analyst_pts = revision_map.get(analyst_revision, 1)

    # Insider bias (0-2 pts)
    insider_map = {"BULLISH": 2, "NEUTRAL": 1, "BEARISH": 0}
    insider_pts = insider_map.get(insider_bias, 1)

    return min(15, rev_pts + margin_pts + analyst_pts + insider_pts)


def _d6_gamma_safety(context: dict, direction: str) -> int:
    """
    D6 — Gamma Environment Safety (0-15 pts)
    Source: context["gamma"][net_gex, pct_to_call_wall, pct_to_put_wall]
    
    Measures dealer gamma positioning risk. Negative GEX amplifies moves;
    walls close to current price cap profit potential.
    """
    gamma         = context.get("gamma", {}) or {}
    net_gex       = str(gamma.get("net_gex", "NEUTRAL")).upper()
    pct_to_call   = gamma.get("pct_to_call_wall")
    pct_to_put    = gamma.get("pct_to_put_wall")

    # GEX base (0-7 pts)
    gex_map = {"POSITIVE": 7, "NEUTRAL": 4, "NEGATIVE": 0}
    gex_pts = gex_map.get(net_gex, 4)

    # Distance to relevant wall (0-8 pts)
    wall_dist = (pct_to_call if direction == "bullish" else pct_to_put) or 0

    if   wall_dist >= 6.0:  wall_pts = 8
    elif wall_dist >= 4.0:  wall_pts = 6
    elif wall_dist >= 2.5:  wall_pts = 4
    elif wall_dist >= 1.5:  wall_pts = 2
    elif wall_dist >= 0.75: wall_pts = 1
    else:                   wall_pts = 0  # Immediate wall — blocked

    return min(15, gex_pts + wall_pts)


# ══════════════════════════════════════════════════════════════════════════════
# MASTER COMPUTE FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def compute_score(context: dict, direction: str) -> ScoreResult:
    """
    Compute deterministic score from Oracle context packet.
    
    Args:
        context:   Full Oracle context dict (price, vol, flow, gamma,
                   macro, fundamental, historical keys)
        direction: "bullish" or "bearish" — from determine_direction()
    
    Returns:
        ScoreResult with score, breakdown, gates_fired, cap_applied
    """
    # Step 0 — Data completeness gate
    # If Oracle returned mostly empty data, scoring with defaults produces
    # a fake score (typically 58) that looks real but has no signal.
    # Reject scoring when key fields are missing to prevent phantom picks.
    _flow = context.get("flow", {}) or {}
    _vol  = context.get("vol", {}) or {}
    _fund = context.get("fundamental", {}) or {}
    _gamma = context.get("gamma", {}) or {}
    _price = context.get("price", {}) or {}
    _none_count = sum(1 for v in [
        _flow.get("put_call_ratio"),
        _vol.get("iv_rank"),
        _price.get("rsi_14"),
        _fund.get("revenue_growth_yoy"),
        _gamma.get("net_gex"),
        _price.get("price_vs_50d_ma"),
    ] if v is None)
    if _none_count >= 4:
        # Too many missing fields — return a sentinel score of 0 with REJECT
        # so callers can skip submission rather than score with defaults.
        return ScoreResult(
            score         = 0,
            raw_score     = 0,
            direction     = direction,
            strategy_type = "CREDIT",
            breakdown     = {"DATA_INSUFFICIENT": _none_count},
            gates_fired   = [f"DATA_GATE: {_none_count}/6 key fields None"],
            cap_applied   = 0,
            recommendation = "REJECT",
        )

    # Step 1 — Hard gates
    gates_fired, cap = _check_hard_gates(context, direction)

    # Step 1b — Strategy mode (CREDIT / DEBIT / LOW_IV)
    strategy_mode = _determine_strategy_mode(context)

    # Step 2 — Six dimensions
    d1 = _d1_iv_structure(context, strategy_mode)
    d2 = _d2_options_flow(context, direction)
    d3 = _d3_technical_setup(context, direction)
    d4 = _d4_earnings_safety(context)
    d5 = _d5_fundamental_quality(context)
    d6 = _d6_gamma_safety(context, direction)

    raw = d1 + d2 + d3 + d4 + d5 + d6

    # Step 3 — Apply cap if gates fired
    final = min(raw, cap) if cap is not None else raw
    final = max(0, min(100, final))  # Clamp to 0-100

    # Step 4 — Recommendation tier
    if final >= 80:      rec = "STRONG_EXECUTE"
    elif final >= 70:    rec = "EXECUTE"
    elif final >= 58:    rec = "WATCH"
    else:                rec = "REJECT"

    # Derive strategy_type for output
    if strategy_mode == "DEBIT":
        strategy_type = "DEBIT_CALL" if direction == "bullish" else "DEBIT_PUT"
    elif strategy_mode == "LOW_IV":
        strategy_type = "LOW_IV"
    else:
        strategy_type = "CREDIT"

    return ScoreResult(
        score         = final,
        raw_score     = raw,
        direction     = direction,
        strategy_type = strategy_type,
        breakdown     = {
            "D1_iv_structure":       d1,
            "D2_options_flow":       d2,
            "D3_technical_setup":    d3,
            "D4_earnings_safety":    d4,
            "D5_fundamental_quality":d5,
            "D6_gamma_safety":       d6,
        },
        gates_fired  = gates_fired,
        cap_applied  = cap,
        recommendation = rec,
    )


# ══════════════════════════════════════════════════════════════════════════════
# REASONING PROMPT BUILDER (for LLM — explanation only, not scoring)
# ══════════════════════════════════════════════════════════════════════════════

def build_reasoning_prompt(ticker: str, result: ScoreResult) -> str:
    """
    Build a tightly constrained prompt for the LLM.
    The LLM explains the score — it does NOT compute it.
    Output must be 1-2 sentences maximum.
    """
    bd = result.breakdown
    gate_text = (" GATES FIRED: " + "; ".join(result.gates_fired)) if result.gates_fired else ""

    return f"""The scoring engine has already computed a deterministic score for {ticker}.
Your ONLY job is to write 1-2 sentences explaining WHY this score was produced.
Do NOT change the score. Do NOT suggest a different score.

SCORE: {result.score}/100  DIRECTION: {result.direction}  RECOMMENDATION: {result.recommendation}

BREAKDOWN (computed from lookup tables, not your judgment):
  D1 IV Structure:        {bd['D1_iv_structure']}/20
  D2 Options Flow:        {bd['D2_options_flow']}/15
  D3 Technical Setup:     {bd['D3_technical_setup']}/20
  D4 Earnings Safety:     {bd['D4_earnings_safety']}/15
  D5 Fundamental Quality: {bd['D5_fundamental_quality']}/15
  D6 Gamma Safety:        {bd['D6_gamma_safety']}/15
  Raw total: {result.raw_score}/100{f"  →  Capped at {result.cap_applied}" if result.cap_applied else ""}
{gate_text}

Write 1-2 sentences explaining the key drivers of this score. Be specific about which dimensions were strong or weak.
Return ONLY the explanation text — no JSON, no score, no recommendation."""
