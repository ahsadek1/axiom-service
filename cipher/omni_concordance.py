"""
omni_concordance.py — Deterministic Concordance Engine
Nexus V1 | Replaces OMNI's continuous polling loop

This service runs as a LaunchAgent on the Mac Mini.
It orchestrates all three scorers, applies concordance rules,
and routes qualifying picks to execution — no continuous LLM session required.

CONCORDANCE PATHWAYS:
  P1: 3/3 agents agree, all ≥ 55   → 1.00x size, auto GO
  P2: 2/3 agents agree, each ≥ 60  → 0.75x size, auto GO
  P3: 1 agent ≥ 75                  → 0.50x size, LLM review
  P4: No concordance                → 0.25x size, LLM review (or skip)

EXECUTION:
  Alpha picks → POST http://localhost:8002/submit  (V2 Alpha Buffer)
              → POST Railway Alpha endpoint         (V1 fire-and-forget)
  Prime picks → POST http://localhost:8003/submit  (V2 Prime Buffer)
              → POST Railway Prime endpoint         (V1 fire-and-forget)

AILS PAYLOAD:
  Every decision (GO/NO-GO) logged with full factor breakdown
  for adaptive learning and weight calibration.

POSITION SIZING (proof-of-concept):
  $250 per trade. No daily cap — maximize learning throughput.
  Hard blocks: VIX > 40, Alpaca margin breach only.
"""

import os
import logging
import time
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# Import scoring engines
from cipher_scorer import score_pool as cipher_score_pool, CipherResult
from sage_scorer   import score_pool as sage_score_pool,   SageResult
from atlas_scorer  import score_pool as atlas_score_pool,  AtlasResult

logger = logging.getLogger("omni.concordance")

# ── Configuration ──────────────────────────────────────────────────────────────

NEXUS_SECRET      = os.getenv("NEXUS_SECRET",
    "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
ALPHA_PRIME_SECRET = os.getenv("NEXUS_PRIME_SECRET", NEXUS_SECRET)

ALPHA_BUFFER_URL  = os.getenv("ALPHA_BUFFER_URL", "http://localhost:8002")
PRIME_BUFFER_URL  = os.getenv("PRIME_BUFFER_URL", "http://localhost:8003")

RAILWAY_ALPHA_URL = os.getenv("RAILWAY_WEBHOOK_URL",
    "https://worker-production-2060.up.railway.app/submit")
RAILWAY_PRIME_URL = os.getenv("RAILWAY_PRIME_URL",
    "https://nexus-prime-bot-production.up.railway.app/prime/submit")

AXIOM_URL         = os.getenv("AXIOM_URL", "http://localhost:8001")
AXIOM_SECRET      = os.getenv("AXIOM_SECRET", NEXUS_SECRET)

# Concordance thresholds (POC: low floors, maximum learning throughput)
P1_MIN_SCORE      = 55.0   # All 3 agents
P2_MIN_SCORE      = 60.0   # 2 of 3 agents (each must meet this)
P3_MIN_SCORE      = 75.0   # Solo agent threshold

# Position sizing (POC: small per trade, no daily cap)
POSITION_SIZE_USD = 250.0

# Hard blocks
VIX_HARD_BLOCK    = 40.0

# Poll interval
POLL_INTERVAL_S   = 900    # 15 minutes (aligns with Axiom pool windows)

# LLM pathways (P3/P4) — invoke OMNI OpenClaw agent
OMNI_OPENCLAW_URL = os.getenv("OMNI_OPENCLAW_URL", "")  # Set if LLM review needed


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class TickerConsensus:
    """Concordance result for a single ticker across all three agents."""
    ticker:           str
    window_id:        str          = ""
    cipher:           Optional[CipherResult] = None
    sage:             Optional[SageResult]   = None
    atlas:            Optional[AtlasResult]  = None

    # Concordance determination
    concordance_path: str          = "P4"    # P1/P2/P3/P4
    agents_agreeing:  int          = 0
    direction:        str          = "bullish"

    # Composite scores
    alpha_composite:  float        = 0.0
    prime_composite:  float        = 0.0
    size_multiplier:  float        = 0.25

    # Decision
    alpha_go:         bool         = False
    prime_go:         bool         = False
    llm_required:     bool         = False
    skip:             bool         = False

    # Execution
    alpha_submitted:  bool         = False
    prime_submitted:  bool         = False

    # Metadata
    decided_at:       str          = field(default_factory=lambda: datetime.utcnow().isoformat())


# ── Pool Fetching ──────────────────────────────────────────────────────────────

def _get_current_pool() -> tuple:
    """
    Fetch current pool from Axiom.
    Returns (window_id, tickers) or ("", []) on failure.
    """
    try:
        r = requests.get(
            f"{AXIOM_URL}/pool",
            headers={"X-Axiom-Secret": AXIOM_SECRET},
            timeout=8,
        )
        if r.status_code == 200:
            data      = r.json()
            tickers   = data.get("pool", data.get("tickers", []))
            window_id = data.get("window_id", datetime.utcnow().strftime("%Y-%m-%d-%H%M"))
            return window_id, tickers
    except Exception as e:
        logger.warning("Axiom pool fetch failed: %s", e)

    # Fallback: try /health which includes pool context
    try:
        r = requests.get(f"{AXIOM_URL}/health", timeout=5)
        if r.status_code == 200:
            data = r.json()
            # Extract pool from health if available
            pool = data.get("pool", [])
            window_id = datetime.utcnow().strftime("%Y-%m-%d-%H%M")
            return window_id, pool
    except Exception:
        pass

    return "", []


def _is_market_hours() -> bool:
    """Check if current time is within market hours (ET)."""
    import pytz
    et   = pytz.timezone("America/New_York")
    now  = datetime.now(et)
    open_ = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_ <= now <= close and now.weekday() < 5


# ── Concordance Logic ──────────────────────────────────────────────────────────

def _determine_direction_consensus(
    cipher: Optional[CipherResult],
    sage:   Optional[SageResult],
    atlas:  Optional[AtlasResult],
) -> str:
    """
    Determine consensus direction from all three agents.
    Majority wins. Tie → bullish default.
    """
    votes = []
    if cipher: votes.append(cipher.direction)
    if sage:   votes.append(sage.direction)
    if atlas:  votes.append(atlas.direction)

    bullish = votes.count("bullish")
    bearish = votes.count("bearish")
    return "bullish" if bullish >= bearish else "bearish"


def _composite_score(
    cipher_score: float,
    sage_score:   float,
    atlas_score:  float,
    n_agents:     int,
) -> float:
    """
    Weighted composite score.
    Cipher 45%, Sage 25%, Atlas 30% (matching V2 agent weights).
    """
    if n_agents == 0:
        return 0.0
    if n_agents == 3:
        return round(cipher_score * 0.45 + sage_score * 0.25 + atlas_score * 0.30, 1)
    elif n_agents == 2:
        # Normalize weights for available agents
        total = cipher_score + sage_score + atlas_score  # one will be 0
        return round(total / n_agents, 1)
    else:
        return round(max(cipher_score, sage_score, atlas_score), 1)


def evaluate_ticker(
    ticker:  str,
    cipher:  Optional[CipherResult],
    sage:    Optional[SageResult],
    atlas:   Optional[AtlasResult],
    window_id: str = "",
    vix:     float = 20.0,
) -> TickerConsensus:
    """
    Apply concordance rules to determine GO/NO-GO for a ticker.

    Concordance pathways:
      P1: 3/3 agents submit same ticker, all ≥ P1_MIN_SCORE  → auto GO 1.00x
      P2: 2/3 agents, each ≥ P2_MIN_SCORE                   → auto GO 0.75x
      P3: 1 agent ≥ P3_MIN_SCORE                             → LLM review 0.50x
      P4: No qualifying agent                                 → skip
    """
    consensus = TickerConsensus(ticker=ticker, window_id=window_id)

    # ── Hard block checks ──
    if vix >= VIX_HARD_BLOCK:
        consensus.skip = True
        logger.warning("%s BLOCKED: VIX %.1f >= %.1f", ticker, vix, VIX_HARD_BLOCK)
        return consensus

    # ── Determine consensus direction ──
    consensus.direction = _determine_direction_consensus(cipher, sage, atlas)
    direction           = consensus.direction

    # ── Get per-agent scores aligned with direction ──
    def agent_alpha_score(r, d):
        """Get alpha score for an agent, 0 if direction mismatch or no submission."""
        if r is None or not r.submit_alpha: return 0.0
        if r.direction != d: return 0.0   # Direction mismatch — don't count
        return r.alpha_score

    def agent_prime_score(r, d):
        if r is None or not r.submit_prime: return 0.0
        if r.direction != d: return 0.0
        return r.prime_score

    c_alpha = agent_alpha_score(cipher, direction)
    s_alpha = agent_alpha_score(sage,   direction)
    a_alpha = agent_alpha_score(atlas,  direction)

    c_prime = agent_prime_score(cipher, direction)
    s_prime = agent_prime_score(sage,   direction)
    a_prime = agent_prime_score(atlas,  direction)

    # ── Count qualifying agents per pathway ──
    # P1/P2 qualify if score meets threshold AND direction agrees
    alpha_qualifiers = [
        sc for sc in [c_alpha, s_alpha, a_alpha]
        if sc >= P2_MIN_SCORE
    ]
    prime_qualifiers = [
        sc for sc in [c_prime, s_prime, a_prime]
        if sc >= P2_MIN_SCORE
    ]

    alpha_p1_qualifiers = [sc for sc in [c_alpha, s_alpha, a_alpha] if sc >= P1_MIN_SCORE]
    prime_p1_qualifiers = [sc for sc in [c_prime, s_prime, a_prime] if sc >= P1_MIN_SCORE]

    # ── Determine concordance path (use alpha as primary signal) ──
    n_agree = len(alpha_qualifiers)
    consensus.agents_agreeing = n_agree

    if n_agree == 3 and all(sc >= P1_MIN_SCORE for sc in [c_alpha, s_alpha, a_alpha] if sc > 0):
        consensus.concordance_path = "P1"
        consensus.size_multiplier  = 1.00
        consensus.alpha_go         = True
        consensus.prime_go         = len(prime_p1_qualifiers) >= 2

    elif n_agree >= 2:
        consensus.concordance_path = "P2"
        consensus.size_multiplier  = 0.75
        consensus.alpha_go         = True
        consensus.prime_go         = len(prime_qualifiers) >= 2

    elif max(c_alpha, s_alpha, a_alpha) >= P3_MIN_SCORE:
        consensus.concordance_path = "P3"
        consensus.size_multiplier  = 0.50
        consensus.llm_required     = True
        # P3: still GO for POC (maximise learning), flag for LLM annotation
        consensus.alpha_go = True
        consensus.prime_go = max(c_prime, s_prime, a_prime) >= P3_MIN_SCORE

    else:
        consensus.concordance_path = "P4"
        consensus.size_multiplier  = 0.0
        consensus.skip             = True  # No signal strong enough

    # ── Composite scores ──
    n = sum(1 for sc in [c_alpha, s_alpha, a_alpha] if sc > 0)
    consensus.alpha_composite = _composite_score(c_alpha, s_alpha, a_alpha, n)
    consensus.prime_composite = _composite_score(c_prime, s_prime, a_prime, n)

    logger.info(
        "%s [%s] Path=%s Agents=%d/3 AComp=%.1f PComp=%.1f Size=%.2fx Alpha=%s Prime=%s",
        ticker, direction, consensus.concordance_path, n_agree,
        consensus.alpha_composite, consensus.prime_composite,
        consensus.size_multiplier,
        "GO" if consensus.alpha_go else "NO",
        "GO" if consensus.prime_go else "NO",
    )

    return consensus


# ── Execution ──────────────────────────────────────────────────────────────────

def _submit_pick(
    ticker:    str,
    direction: str,
    score:     float,
    reasoning: str,
    arena:     str,   # 'alpha' or 'prime'
    window_id: str,
    path:      str,
) -> bool:
    """
    Submit a qualifying pick to the appropriate buffer.
    Always tries V2 local buffer first, then Railway V1 fire-and-forget.
    """
    payload = {
        "agent":     "OMNI-Concordance",
        "ticker":    ticker,
        "direction": direction,
        "score":     score,
        "reasoning": reasoning,
        "window_id": window_id,
        "concordance_path": path,
        "position_size_usd": POSITION_SIZE_USD,
    }
    headers = {"X-Nexus-Secret": NEXUS_SECRET, "Content-Type": "application/json"}

    if arena == "alpha":
        local_url   = f"{ALPHA_BUFFER_URL}/submit"
        railway_url = RAILWAY_ALPHA_URL
    else:
        local_url   = f"{PRIME_BUFFER_URL}/submit"
        railway_url = RAILWAY_PRIME_URL
        headers["X-Nexus-Secret"] = ALPHA_PRIME_SECRET

    # ── V2 Local Buffer ──
    local_ok = False
    try:
        r = requests.post(local_url, json=payload, headers=headers, timeout=10)
        if r.status_code in (200, 201, 409):
            local_ok = True
            logger.info("V2 %s buffer accepted %s/%s [%s]", arena.upper(), ticker, direction, path)
        else:
            logger.warning("V2 %s buffer %d: %s", arena, r.status_code, r.text[:100])
    except Exception as e:
        logger.warning("V2 %s buffer error: %s", arena, e)

    # ── V1 Railway (fire-and-forget) ──
    def _railway_fire():
        try:
            r = requests.post(railway_url, json=payload, headers=headers, timeout=15)
            logger.info("V1 Railway %s: HTTP %d", arena, r.status_code)
        except Exception as e:
            logger.debug("V1 Railway fire-and-forget failed: %s", e)

    t = threading.Thread(target=_railway_fire, daemon=True, name=f"railway-{ticker}-{arena}")
    t.start()

    return local_ok


def _log_to_ails(consensus: TickerConsensus) -> None:
    """Log full decision payload to AILS for learning."""
    payload = {
        "ticker":           consensus.ticker,
        "window_id":        consensus.window_id,
        "direction":        consensus.direction,
        "concordance_path": consensus.concordance_path,
        "agents_agreeing":  consensus.agents_agreeing,
        "alpha_composite":  consensus.alpha_composite,
        "prime_composite":  consensus.prime_composite,
        "size_multiplier":  consensus.size_multiplier,
        "alpha_go":         consensus.alpha_go,
        "prime_go":         consensus.prime_go,
        "alpha_submitted":  consensus.alpha_submitted,
        "prime_submitted":  consensus.prime_submitted,
        "llm_required":     consensus.llm_required,
        "factors": {
            "cipher": {
                "alpha": consensus.cipher.alpha_score if consensus.cipher else None,
                "prime": consensus.cipher.prime_score if consensus.cipher else None,
                "factors": {
                    "trend":     consensus.cipher.factors.trend     if consensus.cipher else None,
                    "momentum":  consensus.cipher.factors.momentum  if consensus.cipher else None,
                    "structure": consensus.cipher.factors.structure if consensus.cipher else None,
                    "volume":    consensus.cipher.factors.volume    if consensus.cipher else None,
                    "rel_str":   consensus.cipher.factors.rel_str   if consensus.cipher else None,
                } if consensus.cipher else None,
            },
            "sage": {
                "alpha": consensus.sage.alpha_score if consensus.sage else None,
                "prime": consensus.sage.prime_score if consensus.sage else None,
                "vix":   consensus.sage.vix_level   if consensus.sage else None,
            },
            "atlas": {
                "alpha": consensus.atlas.alpha_score if consensus.atlas else None,
                "prime": consensus.atlas.prime_score if consensus.atlas else None,
                "ivr":   consensus.atlas.ivr_raw     if consensus.atlas else None,
            },
        },
        "decided_at": consensus.decided_at,
    }

    # Log to AILS endpoint
    ails_url = os.getenv("AILS_URL", "http://localhost:8008")
    try:
        requests.post(
            f"{ails_url}/record",
            json=payload,
            headers={"X-Nexus-Secret": NEXUS_SECRET},
            timeout=5,
        )
    except Exception:
        pass  # AILS logging is non-blocking

    # Also write to local JSON log for backup
    log_path = os.path.expanduser("~/nexus/logs/omni_concordance/decisions.jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    try:
        with open(log_path, "a") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception as e:
        logger.debug("Local AILS log error: %s", e)


# ── Main Orchestration Loop ────────────────────────────────────────────────────

def run_cycle(pool: list, window_id: str) -> dict:
    """
    Run one full concordance cycle for a pool of tickers.
    Scores all three agents in parallel, then evaluates concordance.

    Args:
        pool:      List of tickers from Axiom
        window_id: Current Axiom window ID

    Returns:
        Summary dict with GO counts and decisions
    """
    if not pool:
        logger.info("[%s] Empty pool — skipping cycle", window_id)
        return {"window_id": window_id, "alpha_go": 0, "prime_go": 0, "tickers": 0}

    logger.info("[%s] Starting concordance cycle | %d tickers", window_id, len(pool))

    # ── Score all three agents in parallel ──
    cipher_results = {}
    sage_results   = {}
    atlas_results  = {}

    def run_cipher():
        results = cipher_score_pool(pool, window_id=window_id)
        return {r.ticker: r for r in results}

    def run_sage():
        results = sage_score_pool(pool, window_id=window_id)
        return {r.ticker: r for r in results}

    def run_atlas():
        results = atlas_score_pool(pool, window_id=window_id)
        return {r.ticker: r for r in results}

    with ThreadPoolExecutor(max_workers=3) as executor:
        future_cipher = executor.submit(run_cipher)
        future_sage   = executor.submit(run_sage)
        future_atlas  = executor.submit(run_atlas)

        cipher_results = future_cipher.result()
        sage_results   = future_sage.result()
        atlas_results  = future_atlas.result()

    # Get VIX for hard block check
    vix = 20.0
    if sage_results:
        first_sage = next(iter(sage_results.values()))
        vix = first_sage.vix_level or 20.0

    logger.info("[%s] All agents scored | VIX=%.1f", window_id, vix)

    # ── Evaluate concordance for each ticker ──
    alpha_go_count = 0
    prime_go_count = 0
    decisions      = []

    for ticker in pool:
        cipher = cipher_results.get(ticker)
        sage   = sage_results.get(ticker)
        atlas  = atlas_results.get(ticker)

        consensus = evaluate_ticker(
            ticker=ticker,
            cipher=cipher,
            sage=sage,
            atlas=atlas,
            window_id=window_id,
            vix=vix,
        )

        # Attach raw results for AILS
        consensus.cipher = cipher
        consensus.sage   = sage
        consensus.atlas  = atlas

        if not consensus.skip:
            reasoning = (
                f"OMNI [{consensus.concordance_path}] {ticker} {consensus.direction} "
                f"| Cipher={cipher.alpha_score:.0f if cipher else 'N/A'} "
                f"Sage={sage.alpha_score:.0f if sage else 'N/A'} "
                f"Atlas={atlas.alpha_score:.0f if atlas else 'N/A'} "
                f"| Composite={consensus.alpha_composite:.1f}"
            )

            if consensus.alpha_go:
                consensus.alpha_submitted = _submit_pick(
                    ticker=ticker, direction=consensus.direction,
                    score=consensus.alpha_composite,
                    reasoning=reasoning, arena="alpha",
                    window_id=window_id, path=consensus.concordance_path,
                )
                if consensus.alpha_submitted:
                    alpha_go_count += 1

            if consensus.prime_go:
                consensus.prime_submitted = _submit_pick(
                    ticker=ticker, direction=consensus.direction,
                    score=consensus.prime_composite,
                    reasoning=reasoning, arena="prime",
                    window_id=window_id, path=consensus.concordance_path,
                )
                if consensus.prime_submitted:
                    prime_go_count += 1

        # Log every decision to AILS (GO and NO-GO — both are learning events)
        _log_to_ails(consensus)
        decisions.append(consensus)

    summary = {
        "window_id":  window_id,
        "tickers":    len(pool),
        "alpha_go":   alpha_go_count,
        "prime_go":   prime_go_count,
        "p1_count":   sum(1 for d in decisions if d.concordance_path == "P1"),
        "p2_count":   sum(1 for d in decisions if d.concordance_path == "P2"),
        "p3_count":   sum(1 for d in decisions if d.concordance_path == "P3"),
        "skipped":    sum(1 for d in decisions if d.skip),
        "vix":        vix,
    }

    logger.info(
        "[%s] Cycle complete | Alpha GO: %d | Prime GO: %d | P1: %d P2: %d P3: %d | Skip: %d",
        window_id, alpha_go_count, prime_go_count,
        summary["p1_count"], summary["p2_count"], summary["p3_count"], summary["skipped"],
    )

    return summary


def run_service() -> None:
    """
    Main service loop.
    Polls Axiom every 15 minutes during market hours.
    Replaces OMNI's continuous crashing LLM session.
    """
    logger.info("OMNI Concordance Engine starting")
    logger.info("Alpha Buffer: %s", ALPHA_BUFFER_URL)
    logger.info("Prime Buffer: %s", PRIME_BUFFER_URL)
    logger.info("Poll interval: %ds | Position size: $%.0f | VIX hard block: %.0f",
                POLL_INTERVAL_S, POSITION_SIZE_USD, VIX_HARD_BLOCK)

    while True:
        try:
            if not _is_market_hours():
                logger.debug("Outside market hours — sleeping %ds", POLL_INTERVAL_S)
                time.sleep(POLL_INTERVAL_S)
                continue

            window_id, pool = _get_current_pool()

            if pool:
                run_cycle(pool, window_id)
            else:
                logger.warning("Empty or unavailable pool from Axiom")

        except Exception as e:
            logger.error("Cycle error: %s", e, exc_info=True)

        time.sleep(POLL_INTERVAL_S)


# ── Test Harness ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    print("\n" + "=" * 70)
    print("OMNI CONCORDANCE ENGINE — TEST RUN")
    print("=" * 70)

    # Simulate a pool push
    test_pool     = ["NVDA","AAPL","GOOGL","TSLA","AMAT",
                     "GLD","BKNG","AXP","ETN","VLO"]
    test_window   = f"TEST-{datetime.utcnow().strftime('%Y-%m-%d-%H%M')}"

    summary = run_cycle(test_pool, test_window)

    print("\n" + "=" * 70)
    print("CONCORDANCE SUMMARY")
    print("=" * 70)
    print(f"Window:    {summary['window_id']}")
    print(f"Tickers:   {summary['tickers']}")
    print(f"VIX:       {summary['vix']:.1f}")
    print(f"Alpha GO:  {summary['alpha_go']}")
    print(f"Prime GO:  {summary['prime_go']}")
    print(f"P1 (3/3):  {summary['p1_count']}")
    print(f"P2 (2/3):  {summary['p2_count']}")
    print(f"P3 (solo): {summary['p3_count']}")
    print(f"Skipped:   {summary['skipped']}")
    print("=" * 70)
    print("\nAILS decisions logged to ~/nexus/logs/omni_concordance/decisions.jsonl")
