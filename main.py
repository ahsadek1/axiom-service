"""
Axiom Risk Intelligence Service
================================
OMNI's independent risk assessment layer.
Full 10-point risk framework powered by DeepSeek.

Deploy to Railway as a Python/FastAPI service.
Replaces the current NestJS skeleton at axiom-production-334c.up.railway.app

Endpoints:
  GET  /health         — Service health check
  POST /assess         — Full 10-point risk report for a ticker
  POST /premarket      — Pre-market risk brief (daily)
  GET  /limits         — Current hard limits + position gate status

Author: OMNI + Ahmed Sadek
Date: 2026-04-05
"""

import os
import json
import datetime
import requests
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
import uvicorn

app = FastAPI(title="Axiom Risk Intelligence + OMNI Webhook Gateway", version="1.1.0")

# ── In-memory pick queue (Alpha → OMNI webhook buffer) ────────────────────────
# Alpha POSTs qualified picks here. OMNI drains this queue every 60s.
# Provides instant < 60s latency vs polling Alpha's /pending directly.
from collections import deque
import threading

_pick_queue: deque = deque(maxlen=100)   # circular buffer, max 100 pending picks
_pick_lock = threading.Lock()

# ── Config ────────────────────────────────────────────────────────────────────
DEEPSEEK_KEY   = os.getenv("DEEPSEEK_KEY", "sk-b750bc3774144ebd95e8dee764ffd384")
NEXUS_SECRET   = os.getenv("NEXUS_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
OPENAI_KEY     = os.getenv("OPENAI_KEY", "")
ALPACA_KEY     = os.getenv("ALPACA_KEY", "PKPGM3BRNYPGCF5Z56IAUZCZJL")
ALPACA_SECRET  = os.getenv("ALPACA_SECRET", "5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs")
ALPACA_URL     = "https://paper-api.alpaca.markets"

# Hard limits — always enforced
MAX_POSITIONS     = 3
MAX_RISK_PER_TRADE = 1000.0
MIN_DTE           = 21
MAX_DTE           = 60
VIX_PAUSE_THRESHOLD = 35.0
CONSECUTIVE_LOSS_LIMIT = 3
MIN_IVR_CREDIT_SPREAD  = 15    # IVR < 15 = hard stop on credit spreads
MIN_IVR_CREDIT_CAUTION = 25    # IVR < 25 = soft flag, 0.85x size
MAX_IVR_DEBIT          = 25    # IVR > 25 = hard stop on debit spreads
MAX_IVR_DEBIT_CAUTION  = 20    # IVR > 20 = soft flag on debit


# ── Models ────────────────────────────────────────────────────────────────────

class AssessRequest(BaseModel):
    ticker: str
    direction: Optional[str] = "bullish"
    strategy: Optional[str] = None
    dte: Optional[int] = 45
    confidence: Optional[float] = None
    agent: Optional[str] = None
    # Optional market context (if caller provides it, we skip fetching)
    vix: Optional[float] = None
    iv_rank: Optional[float] = None
    earnings_date: Optional[str] = None
    earnings_days_away: Optional[int] = None
    rsi: Optional[float] = None
    adx: Optional[float] = None
    breadth_pct_200sma: Optional[float] = None
    pcr: Optional[float] = None
    put_skew: Optional[float] = None


class PremarketRequest(BaseModel):
    date: Optional[str] = None  # YYYY-MM-DD, defaults to today


class PickWebhookRequest(BaseModel):
    """Incoming pick from Alpha Railway when concordance/bypass qualifies."""
    ticker: str
    agent: Optional[str] = None
    agents: Optional[list] = None
    direction: Optional[str] = "bullish"
    strategy: Optional[str] = None
    confidence: Optional[float] = None
    arbiter_score: Optional[float] = None
    reasoning: Optional[str] = None
    submission_id: Optional[str] = None
    path: Optional[str] = "CONCORDANCE"
    # R1/R2 schema fields — required for execution (R1_R2_SCHEMA.md)
    round: Optional[int] = None          # 1 = R1 (pool only), 2 = R2 (concordance eligible)
    r1_score: Optional[float] = None
    r2_score: Optional[float] = None
    expiry: Optional[str] = None         # "YYYY-MM-DD" — required for execution
    strike: Optional[float] = None       # short strike price — required for execution
    width: Optional[float] = None        # spread width in $ — required for spreads
    option_type: Optional[str] = None    # "call" or "put"
    dte: Optional[int] = None
    # Options metrics
    ivr: Optional[float] = None
    iv_rank: Optional[float] = None
    atr_pct: Optional[float] = None
    volume_ratio: Optional[float] = None
    # Full submissions array (Alpha passes full context)
    submissions: Optional[list] = None


# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_secret(x_nexus_secret: Optional[str] = Header(None)):
    if x_nexus_secret != NEXUS_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Position gate check ───────────────────────────────────────────────────────

def get_position_gate() -> dict:
    """Check Alpaca for current open positions."""
    try:
        r = requests.get(
            f"{ALPACA_URL}/v2/positions",
            headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            timeout=5
        )
        positions = r.json() if r.status_code == 200 else []
        count = len(positions) if isinstance(positions, list) else 0
        return {
            "count": count,
            "max": MAX_POSITIONS,
            "gate": "CLOSED" if count >= MAX_POSITIONS else "OPEN",
            "remaining": MAX_POSITIONS - count
        }
    except Exception as e:
        return {"count": 0, "max": MAX_POSITIONS, "gate": "OPEN", "remaining": 3, "error": str(e)}


# ── Core Axiom 10-point assessment ───────────────────────────────────────────

def run_axiom_assessment(ticker: str, req: AssessRequest) -> dict:  # noqa: C901
    """
    Axiom's 10-layer risk framework — each layer independently scored.
    Primary: risk_engine.run_full_assessment() (10 independent data sources)
    Fallback: DeepSeek prompt-based assessment (if risk engine fails)

    2026-04-09 Fixes:
      FIX A: hard_stops now populated from critical_flags (was using auto_reject which is always False)
      FIX B: caller-supplied earnings_days_away + dte are pre-checked BEFORE risk engine call
              so test scenarios with explicit earnings/DTE values are correctly caught
    """
    # ── PRE-CHECK: caller-supplied hard-stop conditions ───────────────────────
    # These take precedence over risk engine's own data fetch.
    # Allows test scenarios and explicit override conditions to work correctly.
    pos_gate_pre = get_position_gate()
    pre_stops = []

    if pos_gate_pre["gate"] == "CLOSED":
        pre_stops.append(f"Position gate CLOSED — {pos_gate_pre['count']}/{MAX_POSITIONS} positions open")

    if req.dte is not None and req.dte < MIN_DTE:
        pre_stops.append(f"DTE {req.dte} < {MIN_DTE} minimum — gamma implosion zone. Hard stop.")

    if req.dte is not None and req.dte > MAX_DTE:
        pre_stops.append(f"DTE {req.dte} > {MAX_DTE} maximum — too far out. Hard stop.")

    if req.earnings_days_away is not None and req.earnings_days_away <= 5:
        pre_stops.append(
            f"Earnings in {req.earnings_days_away} day(s) — within 5-day hard stop window. No new positions."
        )

    if req.vix is not None and req.vix > VIX_PAUSE_THRESHOLD:
        pre_stops.append(f"VIX {req.vix} > {VIX_PAUSE_THRESHOLD} pause threshold")

    if pre_stops:
        return {
            "ticker":            ticker,
            "risk_score":        10,
            "sizing_suggestion": "avoid",
            "sizing_rec":        "avoid",
            "hard_stops":        pre_stops,
            "critical_flags":    [{"layer": 0, "name": "Pre-check", "reason": s} for s in pre_stops],
            "concern_1":         pre_stops[0],
            "concern_2":         pre_stops[1] if len(pre_stops) > 1 else "N/A",
            "concern_3":         pre_stops[2] if len(pre_stops) > 2 else "N/A",
            "report":            f"Hard stop triggered: {'; '.join(pre_stops)}. DO NOT execute.",
            "position_gate":     pos_gate_pre,
            "auto_reject":       False,
            "model":             "axiom-pre-check",
        }

    # ── PRIMARY: 10-layer independent engine ─────────────────────────────────
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(__file__))
        from risk_engine import run_full_assessment
        result = run_full_assessment(
            ticker       = ticker,
            strategy     = req.strategy or req.direction or "options",
            dte          = req.dte or 35,
            strike       = None,
            proposed_usd = min(MAX_RISK_PER_TRADE, 1000.0),
        )
        # Normalize output to match existing API contract
        # FIX A: populate hard_stops from critical_flags (auto_reject is always False — wrong source)
        result["model"]          = "axiom-risk-engine-v2"
        result["position_gate"]  = get_position_gate()
        result["hard_stops"]     = [f["reason"] for f in result.get("critical_flags", [])]
        return result
    except Exception as engine_err:
        print(f"Risk engine failed, falling back to DeepSeek: {engine_err}")

    # ── FALLBACK: DeepSeek prompt-based ──────────────────────────────────────
    pos_gate = get_position_gate()

    # Hard stop checks BEFORE calling DeepSeek
    hard_stops = []

    if pos_gate["gate"] == "CLOSED":
        hard_stops.append(f"Position gate CLOSED — {pos_gate['count']}/{MAX_POSITIONS} positions open")

    if req.earnings_days_away is not None and req.earnings_days_away <= 7:
        hard_stops.append(f"Earnings in {req.earnings_days_away} days — hard stop")

    if req.dte is not None and req.dte < MIN_DTE:
        hard_stops.append(f"DTE {req.dte} < {MIN_DTE} minimum")

    if req.dte is not None and req.dte > MAX_DTE:
        hard_stops.append(f"DTE {req.dte} > {MAX_DTE} maximum")

    if req.vix is not None and req.vix > VIX_PAUSE_THRESHOLD:
        hard_stops.append(f"VIX {req.vix} > {VIX_PAUSE_THRESHOLD} pause threshold")

    if hard_stops:
        return {
            "ticker": ticker,
            "risk_score": 10,
            "sizing_suggestion": "avoid",
            "hard_stops": hard_stops,
            "concern_1": hard_stops[0],
            "concern_2": hard_stops[1] if len(hard_stops) > 1 else "N/A",
            "concern_3": hard_stops[2] if len(hard_stops) > 2 else "N/A",
            "report": f"Hard stop triggered: {'; '.join(hard_stops)}. DO NOT execute.",
            "position_gate": pos_gate,
            "auto_reject": False,
        }

    # Build Axiom prompt
    prompt = f"""You are Axiom — Nexus Alpha's independent risk intelligence system.
Evaluate {ticker} through a pure risk lens. Ignore what screening agents said. See only the data.

TRADE DETAILS:
Ticker: {ticker}
Direction: {req.direction or 'N/A'}
Strategy: {req.strategy or 'N/A'}
DTE: {req.dte or 'N/A'}
Agent Confidence: {req.confidence or 'N/A'}
Submitting Agent: {req.agent or 'N/A'}

MARKET DATA:
VIX: {req.vix or 'N/A'}
IV Rank: {req.iv_rank or 'N/A'}
Earnings: {req.earnings_date or 'None'} ({req.earnings_days_away or 'N/A'} days away)
RSI: {req.rsi or 'N/A'}
ADX: {req.adx or 'N/A'}
Breadth (% above 200 SMA): {req.breadth_pct_200sma or 'N/A'}%
PCR: {req.pcr or 'N/A'}
Put Skew: {req.put_skew or 'N/A'}x
Open Positions: {pos_gate['count']}/{pos_gate['max']} ({pos_gate['gate']})
Max Risk Per Trade: ${MAX_RISK_PER_TRADE}

EVALUATE THESE 10 RISK DIMENSIONS:
1. Concentration Risk — sector/ticker exposure vs $1,000 max and 3-position limit
2. Liquidity Risk — likely options volume, OI quality for this ticker/strategy
3. Volatility Regime Alignment — IV environment fit for this strategy type
4. Earnings & Event Proximity — binary event risk within DTE window
5. Correlation/Beta — how much systematic market risk does this carry?
6. Gamma Risk Profile — near-expiry gamma exposure (is DTE safe?)
7. Dividend Risk — potential ex-div dates within the hold window
8. Macro Sensitivity — is this ticker/sector particularly sensitive to rates/inflation/trade?
9. Technical Breakdown Risk — is price at a point where a move against us accelerates?
10. Black Swan Buffer — any tail risk factors that could cause catastrophic loss?

Be an independent judge. Commit to a view. Do NOT be vague.

RESPOND IN THIS EXACT FORMAT — nothing else:
RISK_SCORE: [1-10, 10=maximum risk]
SIZING_SUGGESTION: [full / 0.75x / 0.5x / avoid]
CONCERN_1: [most critical risk factor, ≤80 chars]
CONCERN_2: [second risk factor, ≤80 chars]
CONCERN_3: [third risk factor, ≤80 chars]
REPORT: [3-4 sentences: objective risk assessment, commit to a view]"""

    try:
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 400,
                "temperature": 0.3,
            },
            timeout=25
        )
        text = r.json()["choices"][0]["message"]["content"].strip()

        def extract(key):
            for line in text.split("\n"):
                if line.startswith(f"{key}:"):
                    return line.split(":", 1)[1].strip()
            return "N/A"

        risk_raw = extract("RISK_SCORE")
        try:
            risk_score = int(risk_raw.split("/")[0].strip())
        except:
            risk_score = 5

        return {
            "ticker": ticker,
            "risk_score": risk_score,
            "sizing_suggestion": extract("SIZING_SUGGESTION"),
            "concern_1": extract("CONCERN_1"),
            "concern_2": extract("CONCERN_2"),
            "concern_3": extract("CONCERN_3"),
            "report": extract("REPORT"),
            "position_gate": pos_gate,
            "hard_stops": [],
            "auto_reject": False,
            "raw": text,
            "model": "deepseek-chat",
            "timestamp": datetime.datetime.utcnow().isoformat(),
        }

    except Exception as e:
        return {
            "ticker": ticker,
            "risk_score": 5,
            "sizing_suggestion": "full",
            "concern_1": f"Axiom assessment failed: {e}",
            "concern_2": "Manual review recommended",
            "concern_3": "N/A",
            "report": "Axiom risk assessment unavailable due to service error. Proceed with caution.",
            "position_gate": pos_gate,
            "hard_stops": [],
            "auto_reject": False,
            "error": str(e),
            "timestamp": datetime.datetime.utcnow().isoformat(),
        }


# ── Premarket brief ───────────────────────────────────────────────────────────

def run_premarket_brief(date: str) -> dict:
    """
    Axiom's daily pre-market risk brief.
    Focuses on: tail risks, macro events, what to watch out for today.
    Sent to all agents as GUIDING knowledge before market open.
    """
    prompt = f"""You are Axiom — Nexus Alpha's risk intelligence system.
Today is {date}. Produce the pre-market risk brief for the trading day ahead.

Focus on RISK only — what could go wrong today, not opportunities.
This brief goes to all screening agents as context before they screen.

Structure your brief as follows:
REGIME_RISK: [1-10 — overall risk level for today]
TOP_RISK_1: [biggest risk factor today, ≤100 chars]
TOP_RISK_2: [second risk factor, ≤100 chars]
TOP_RISK_3: [third risk factor, ≤100 chars]
STRATEGY_BIAS: [what strategy types to favor/avoid given today's risk profile, ≤150 chars]
SECTORS_WATCH: [sectors with elevated event risk today, ≤100 chars]
GUIDANCE: [2-3 sentences: concise risk guidance for today's session]"""

    try:
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 350,
                "temperature": 0.3,
            },
            timeout=25
        )
        text = r.json()["choices"][0]["message"]["content"].strip()

        def extract(key):
            for line in text.split("\n"):
                if line.startswith(f"{key}:"):
                    return line.split(":", 1)[1].strip()
            return "N/A"

        risk_raw = extract("REGIME_RISK")
        try:
            regime_risk = int(risk_raw.split("/")[0].strip())
        except:
            regime_risk = 5

        return {
            "date": date,
            "regime_risk": regime_risk,
            "top_risk_1": extract("TOP_RISK_1"),
            "top_risk_2": extract("TOP_RISK_2"),
            "top_risk_3": extract("TOP_RISK_3"),
            "strategy_bias": extract("STRATEGY_BIAS"),
            "sectors_watch": extract("SECTORS_WATCH"),
            "guidance": extract("GUIDANCE"),
            "raw": text,
            "timestamp": datetime.datetime.utcnow().isoformat(),
        }
    except Exception as e:
        return {"error": str(e), "date": date}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/pick")
def receive_pick(req: PickWebhookRequest, x_nexus_secret: Optional[str] = Header(None)):
    """
    OMNI Webhook Receiver.
    Alpha Railway POSTs here when a pick qualifies (concordance or score bypass).
    Pick is queued for OMNI's next poll cycle (≤60s latency).
    Also sends an immediate Telegram ping to wake OMNI faster.
    """
    verify_secret(x_nexus_secret)

    pick = req.dict()
    pick["received_at"] = datetime.datetime.utcnow().isoformat()
    pick["processed"] = False
    # Validate R2 contract fields — warn if missing (execution will fail without them)
    missing_contract_fields = []
    if not req.expiry:   missing_contract_fields.append("expiry")
    if not req.strike:   missing_contract_fields.append("strike")
    if not req.option_type: missing_contract_fields.append("option_type")
    if missing_contract_fields:
        pick["_contract_warning"] = f"Missing execution fields: {', '.join(missing_contract_fields)} — execution will require fallback ATM lookup"

    with _pick_lock:
        _pick_queue.append(pick)
        queue_depth = len(_pick_queue)

    # War room: post pick arrival to appropriate Telegram group immediately
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "synthesis"))
        from transparency_layer import post_pick_received, post_concordance
        extra = {
            "volume_ratio": req.dict().get("volume_ratio"),
            "ivr":          req.dict().get("ivr"),
            "atr_pct":      req.dict().get("atr_pct"),
        }
        post_pick_received(
            agent      = req.agent or "unknown",
            ticker     = req.ticker,
            strategy   = req.strategy or req.path or "options",
            confidence = req.confidence or 0,
            direction  = req.direction or "",
            strike     = req.strike,
            expiry     = req.expiry,
            extra      = {k: v for k, v in extra.items() if v is not None},
        )
        # If concordance path — also post concordance message
        if req.path == "CONCORDANCE" and req.agents:
            post_concordance(
                ticker        = req.ticker,
                strategy      = req.strategy or "options",
                agents        = req.agents if isinstance(req.agents, list) else [req.agent or "unknown"],
                arbiter_score = req.arbiter_score or req.confidence or 0,
                extra         = {"ivr": req.dict().get("ivr"), "regime": req.dict().get("regime")},
            )
    except Exception:
        pass  # Transparency layer never blocks the pipeline

    # Immediate Telegram ping to OMNI — don't wait for poller
    _ping_omni(req.ticker, req.path or "CONCORDANCE", req.agent or "unknown")

    return {
        "status": "queued",
        "ticker": req.ticker,
        "queue_depth": queue_depth,
        "message": "Pick queued for OMNI synthesis. War room notified. Telegram ping sent."
    }


@app.get("/pick/queue")
def get_pick_queue(x_nexus_secret: Optional[str] = Header(None)):
    """
    OMNI polls this endpoint to drain the webhook queue.
    Returns unprocessed picks and marks them as processed.
    """
    verify_secret(x_nexus_secret)

    with _pick_lock:
        unprocessed = [p for p in _pick_queue if not p.get("processed")]
        for p in unprocessed:
            p["processed"] = True

    return {
        "picks": unprocessed,
        "count": len(unprocessed),
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }


def _ping_omni(ticker: str, path: str, agent: str) -> None:
    """Send immediate Telegram alert to OMNI when a pick arrives."""
    TG_BOT_TOKEN  = os.getenv("TG_BOT_TOKEN", "8747601602:AAGTzRd3NJWq44Bvbzd5JvhtnO2edBUvjbc")
    AHMED_CHAT_ID = os.getenv("AHMED_CHAT_ID", "8573754783")
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": AHMED_CHAT_ID,
                "text": f"⚡ <b>OMNI PICK INCOMING</b> — {ticker}\nPath: {path} | Agent: {agent}\nSynthesis running...",
                "parse_mode": "HTML"
            },
            timeout=5
        )
    except Exception:
        pass


@app.get("/health")
def health():
    pos_gate = get_position_gate()
    return {
        "status": "healthy",
        "service": "axiom-risk-intelligence",
        "version": "1.0.0",
        "position_gate": pos_gate["gate"],
        "positions": f"{pos_gate['count']}/{pos_gate['max']}",
        "hard_limits": {
            "max_positions": MAX_POSITIONS,
            "max_risk_per_trade": MAX_RISK_PER_TRADE,
            "min_dte": MIN_DTE,
            "max_dte": MAX_DTE,
            "vix_pause_threshold": VIX_PAUSE_THRESHOLD,
        },
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }


@app.get("/limits")
def limits(x_nexus_secret: Optional[str] = Header(None)):
    verify_secret(x_nexus_secret)
    pos_gate = get_position_gate()
    return {
        "position_gate": pos_gate,
        "hard_limits": {
            "max_positions":           MAX_POSITIONS,
            "max_risk_per_trade_usd":  MAX_RISK_PER_TRADE,
            "min_dte":                 MIN_DTE,
            "max_dte":                 MAX_DTE,
            "vix_pause_threshold":     VIX_PAUSE_THRESHOLD,
            "consecutive_loss_limit":  CONSECUTIVE_LOSS_LIMIT,
            "min_ivr_credit_spread":   MIN_IVR_CREDIT_SPREAD,
            "min_ivr_credit_caution":  MIN_IVR_CREDIT_CAUTION,
            "max_ivr_debit":           MAX_IVR_DEBIT,
            "max_ivr_debit_caution":   MAX_IVR_DEBIT_CAUTION,
        },
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }


@app.post("/assess")
def assess(req: AssessRequest, x_nexus_secret: Optional[str] = Header(None)):
    """
    Full 10-point risk assessment for a single ticker.
    Called by OMNI during synthesis pipeline.
    """
    verify_secret(x_nexus_secret)
    result = run_axiom_assessment(req.ticker, req)
    return result


@app.post("/premarket")
def premarket(req: PremarketRequest, x_nexus_secret: Optional[str] = Header(None)):
    """
    Daily pre-market risk brief — Official 11-Section Axiom Template.
    Fires report generation in a background thread and returns 202 immediately.
    Report is sent to Telegram groups when ready (30-60s after call).
    """
    verify_secret(x_nexus_secret)

    import threading as _threading
    import sys as _sys, os as _os

    def _run_report():
        try:
            _sys.path.insert(0, _os.path.dirname(__file__))
            from premarket_template import run_premarket_report
            run_premarket_report(send_telegram=True)
        except Exception as _e:
            # Fallback brief on error
            try:
                _date = req.date or datetime.date.today().isoformat()
                run_premarket_brief(_date)
            except Exception:
                pass

    _threading.Thread(target=_run_report, daemon=True).start()
    return {
        "status": "accepted",
        "message": "Pre-market report generating in background — will be delivered to Telegram within 60s",
        "date": req.date or datetime.date.today().isoformat(),
    }






# ── /screen — lightweight pre-screen for scanning loop ───────────────────────

class ScreenRequest(BaseModel):
    ticker:    str
    direction: str = "bullish"
    mode:      str = "light"   # light | full (reserved)

@app.post("/screen")
def screen(req: ScreenRequest, x_nexus_secret: Optional[str] = Header(None)):
    """
    Lightweight Axiom pre-screen for the OMNI scanning loop.
    Called every R1 cycle to update axiom_score in the pool.
    Returns: score (0-100), flags (list[str]), critical (bool).
    
    Score interpretation:
      85-100: Clean — no structural issues
      65-84:  Moderate caution — minor flags
      40-64:  Caution — notable issues (pathway locked)
      0-39:   Structural fail (death trigger)
    """
    verify_secret(x_nexus_secret)
    
    try:
        # Build a minimal AssessRequest for the risk engine
        assess_req = AssessRequest(
            ticker    = req.ticker,
            strategy  = "Bull Put Spread" if req.direction == "bullish" else "Bear Call Spread",
            direction = req.direction,
            dte       = 35,   # sensible default
        )
        result = run_axiom_assessment(req.ticker, assess_req)
        
        sizing_mult   = float(result.get("sizing_mult", result.get("sizing_multiplier", 0.5)))
        risk_score    = float(result.get("risk_score", 5.0))   # 0-10, higher=worse
        crit_flags    = result.get("critical_flags", [])
        crit_count    = int(result.get("critical_flag_count", len(crit_flags)))
        hard_stops    = result.get("hard_stops", [])
        
        # Translate to 0-100 score (higher = cleaner / safer)
        if hard_stops:
            score = 15.0   # hard stop = near-death
        elif sizing_mult == 0.0:
            score = max(20.0, 40.0 - (risk_score * 2))
        elif sizing_mult <= 0.5:
            score = max(40.0, 60.0 - (risk_score * 2))
        elif sizing_mult <= 0.75:
            score = max(55.0, 72.0 - risk_score)
        else:
            # Full sizing — use risk_score as fine-tuner
            score = max(65.0, 90.0 - (risk_score * 3))
        
        # Collect flag strings
        flags = []
        for f in crit_flags:
            if isinstance(f, dict):
                flags.append(f"{f.get('name','?')}: {f.get('reason','')}")
            else:
                flags.append(str(f))
        flags.extend(hard_stops)
        
        return {
            "score":    round(score, 1),
            "flags":    flags[:5],   # cap at 5 flags
            "critical": crit_count > 0 or bool(hard_stops),
            "ticker":   req.ticker,
            "sizing_mult":   sizing_mult,
            "risk_score":    risk_score,
        }
    except Exception as e:
        # Fallback: neutral score so pool entry isn't blocked
        return {
            "score":    50.0,
            "flags":    [f"screen_error: {str(e)[:60]}"],
            "critical": False,
            "ticker":   req.ticker,
        }

# ── Startup — heartbeat emitter ───────────────────────────────────────────────

_startup_time = datetime.datetime.utcnow()

@app.on_event("startup")
def startup_heartbeat():
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
        from heartbeat_emitter import start_heartbeat_emitter
        start_heartbeat_emitter(
            service_name    = "Axiom",
            version         = "1.0.0",
            get_loop_active = lambda: True,
            get_extra       = lambda: {
                "position_gate":   get_position_gate().get("gate", "UNKNOWN"),
                "positions":       get_position_gate().get("count", 0),
                "uptime_hours":    round((datetime.datetime.utcnow() - _startup_time).total_seconds() / 3600, 2),
            }
        )
        print("[AXIOM] Heartbeat emitter started")
    except Exception as e:
        print(f"[AXIOM] Heartbeat emitter failed to start: {e} — continuing without")

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8001))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

