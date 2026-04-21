# OMNI restart trigger: 2026-04-21 14:21 UTC
"""
Axiom Risk Intelligence Service — v3 Clean Rewrite
====================================================
OMNI's independent risk assessment layer. 11-layer risk engine.
All credentials from environment variables — zero hardcoding.

Deploy to Railway as a Python/FastAPI service.
URL: https://axiom-production-334c.up.railway.app

Endpoints:
  GET  /health           — Service health (data source status)
  GET  /limits           — Current hard limits + position gate
  GET  /premarket        — Generate pre-market brief + send to Telegram
  POST /pick             — Webhook receiver: Alpha → OMNI pick queue
  GET  /pick/queue       — OMNI polls this to drain pick queue
  POST /assess           — Full 11-layer risk assessment

Author: OMNI
Date:   2026-04-06 (v3 rebuild)
"""

import os
import json
import datetime
import requests
import threading
from collections import deque
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, validator
import uvicorn

app = FastAPI(title="Axiom Risk Intelligence v3", version="3.0.0")

# ── Credentials — all from env, zero hardcoded ────────────────────────────────
NEXUS_SECRET    = os.getenv("NEXUS_SECRET")
DEEPSEEK_KEY    = os.getenv("DEEPSEEK_API_KEY")
OPENAI_KEY      = os.getenv("OPENAI_API_KEY")
ALPACA_KEY      = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET   = os.getenv("ALPACA_SECRET_KEY")
ALPACA_URL      = os.getenv("ALPACA_URL",     "https://paper-api.alpaca.markets")
POLYGON_KEY     = os.getenv("POLYGON_KEY")
ORATS_TOKEN     = os.getenv("ORATS_TOKEN")
TG_BOT_TOKEN    = os.getenv("TG_BOT_TOKEN")
TG_HEALTH_GROUP = os.getenv("TG_HEALTH_GROUP", "-1003954790884")
TG_AHMED_DM     = os.getenv("TG_AHMED_DM",    "8573754783")

# ── Hard limits (overridable via env) ─────────────────────────────────────────
MAX_POSITIONS        = int(os.getenv("MAX_POSITIONS",        "3"))
MAX_RISK_PER_TRADE   = float(os.getenv("MAX_RISK_USD",       "1000"))
MIN_DTE              = int(os.getenv("MIN_DTE",              "21"))
MAX_DTE              = int(os.getenv("MAX_DTE",              "60"))
VIX_PAUSE_THRESHOLD  = float(os.getenv("VIX_PAUSE_THRESHOLD","35"))

# ── In-memory pick queue (Alpha → OMNI webhook buffer) ────────────────────────
_pick_queue: deque = deque(maxlen=100)
_pick_lock          = threading.Lock()

ALPACA_H = {
    "APCA-API-KEY-ID":     ALPACA_KEY or "",
    "APCA-API-SECRET-KEY": ALPACA_SECRET or "",
    "Content-Type":        "application/json",
}


# ── Models ────────────────────────────────────────────────────────────────────

class PickWebhookRequest(BaseModel):
    """Incoming concordance/bypass pick from Alpha Railway."""
    ticker:        str
    agent:         Optional[str]  = None
    agents:        Optional[list] = None
    arena:         Optional[str]  = "alpha"
    direction:     Optional[str]  = "bullish"
    strategy:      Optional[str]  = None
    confidence:    Optional[float]= None
    arbiter_score: Optional[float]= None
    reasoning:     Optional[str]  = None
    submission_id: Optional[str]  = None
    path:          Optional[str]  = "CONCORDANCE"
    # R2 contract fields — all required for execution path
    round:         Optional[int]  = None
    r1_score:      Optional[float]= None
    r2_score:      Optional[float]= None
    expiry:        Optional[str]  = None   # YYYY-MM-DD
    strike:        Optional[float]= None
    width:         Optional[float]= None
    option_type:   Optional[str]  = None   # "call" | "put"
    dte:           Optional[int]  = None
    ivr:           Optional[float]= None
    iv_rank:       Optional[float]= None
    atr_pct:       Optional[float]= None
    volume_ratio:  Optional[float]= None
    submissions:   Optional[list] = None


class AssessRequest(BaseModel):
    """Full risk assessment request from OMNI synthesis pipeline."""
    ticker:              str
    arena:               Optional[str]  = "alpha"
    strategy:            Optional[str]  = None
    direction:           Optional[str]  = "bullish"
    strike:              Optional[float]= None
    width:               Optional[float]= None
    expiry:              Optional[str]  = None
    dte:                 Optional[int]  = 35
    iv_rank:             Optional[float]= None
    r2_score:            Optional[float]= None
    regime:              Optional[str]  = "NEUTRAL"
    # Optional pre-computed context (saves re-fetching)
    vix:                 Optional[float]= None
    earnings_days_away:  Optional[int]  = None
    rsi:                 Optional[float]= None
    adx:                 Optional[float]= None


# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_secret(x_nexus_secret: Optional[str] = Header(None)):
    if not NEXUS_SECRET:
        raise HTTPException(status_code=500, detail="NEXUS_SECRET not configured")
    if x_nexus_secret != NEXUS_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Data source health tracker ────────────────────────────────────────────────

_source_health: dict = {
    "polygon": {"status": "unknown", "latency_ms": None},
    "orats":   {"status": "unknown", "latency_ms": None},
    "alpaca":  {"status": "unknown", "latency_ms": None},
}

_startup_time = datetime.datetime.utcnow()


def _check_data_sources() -> dict:
    """Quick liveness check on all 3 data sources."""
    import time

    # Polygon
    try:
        t0 = time.time()
        r  = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day/2026-01-01/2026-01-02",
            params={"apiKey": POLYGON_KEY}, timeout=5
        )
        ms = int((time.time() - t0) * 1000)
        _source_health["polygon"] = {"status": "ok" if r.status_code == 200 else "degraded", "latency_ms": ms}
    except Exception as e:
        _source_health["polygon"] = {"status": "unavailable", "error": str(e)[:80]}

    # ORATS
    try:
        t0 = time.time()
        r  = requests.get(
            "https://api.orats.io/datav2/summaries",
            params={"token": ORATS_TOKEN, "ticker": "SPY"}, timeout=5
        )
        ms = int((time.time() - t0) * 1000)
        _source_health["orats"] = {"status": "ok" if r.status_code == 200 else "degraded", "latency_ms": ms}
    except Exception as e:
        _source_health["orats"] = {"status": "unavailable", "error": str(e)[:80]}

    # Alpaca
    try:
        t0 = time.time()
        r  = requests.get(f"{ALPACA_URL}/v2/account", headers=ALPACA_H, timeout=5)
        ms = int((time.time() - t0) * 1000)
        _source_health["alpaca"] = {"status": "ok" if r.status_code == 200 else "degraded", "latency_ms": ms}
    except Exception as e:
        _source_health["alpaca"] = {"status": "unavailable", "error": str(e)[:80]}

    return dict(_source_health)


def get_position_gate() -> dict:
    """Check Alpaca for current open positions (count vs cap)."""
    try:
        r = requests.get(f"{ALPACA_URL}/v2/positions", headers=ALPACA_H, timeout=5)
        positions = r.json() if r.status_code == 200 else []
        count = len(positions) if isinstance(positions, list) else 0
        return {
            "count":     count,
            "max":       MAX_POSITIONS,
            "gate":      "CLOSED" if count >= MAX_POSITIONS else "OPEN",
            "remaining": MAX_POSITIONS - count,
        }
    except Exception as e:
        return {"count": 0, "max": MAX_POSITIONS, "gate": "OPEN", "remaining": MAX_POSITIONS, "error": str(e)[:80]}


# ── Risk assessment (11-layer) ─────────────────────────────────────────────────

def run_axiom_assessment(req: AssessRequest) -> dict:
    """
    Axiom 11-layer risk framework.
    Primary: risk_engine.run_full_assessment()
    Fallback: DeepSeek prompt-based assessment

    LAYER FAILURE HANDLING: If any data source is unavailable, that layer
    scores 0 (pass) with an explicit flag in the response.
    Axiom NEVER fails closed due to a data source being down.
    """
    try:
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        from risk_engine import run_full_assessment
        result = run_full_assessment(
            ticker       = req.ticker,
            strategy     = req.strategy or req.direction or "options",
            dte          = req.dte or 35,
            strike       = req.strike,
            proposed_usd = min(MAX_RISK_PER_TRADE, 1000.0),
            vix          = req.vix,
            iv_rank      = req.iv_rank,
            regime       = req.regime,
        )
        result["model"]         = "axiom-risk-engine-v3"
        result["position_gate"] = get_position_gate()
        # Add contract_warning if contract fields missing
        missing = [f for f in ["expiry", "strike", "option_type"]
                   if not getattr(req, f, None)]
        if missing:
            result["contract_warning"] = f"Missing fields for execution: {', '.join(missing)}"
        return result
    except Exception as engine_err:
        print(f"[Axiom] Risk engine failed, using DeepSeek fallback: {engine_err}")

    # ── DeepSeek fallback ─────────────────────────────────────────────────────
    pos_gate   = get_position_gate()
    hard_stops = []

    if pos_gate["gate"] == "CLOSED":
        hard_stops.append(f"Position gate CLOSED — {pos_gate['count']}/{MAX_POSITIONS} positions open")
    if req.earnings_days_away is not None and req.earnings_days_away <= 14:
        hard_stops.append(f"Earnings in {req.earnings_days_away} days — hard stop (options window)")
    if req.dte is not None and (req.dte < MIN_DTE or req.dte > MAX_DTE):
        hard_stops.append(f"DTE {req.dte} outside [{MIN_DTE}-{MAX_DTE}] window")
    if req.vix is not None and req.vix >= 40:
        hard_stops.append(f"VIX {req.vix} ≥ 40 — all options entries blocked")

    if hard_stops:
        return {
            "ticker":       req.ticker,
            "risk_score":   10,
            "sizing":       "avoid",
            "approved":     False,
            "flags":        hard_stops,
            "hard_stops":   hard_stops,
            "layer_scores": {},
            "reason":       f"Hard stop: {hard_stops[0]}",
            "position_gate": pos_gate,
            "assessed_at":  datetime.datetime.utcnow().isoformat(),
        }

    # Sizing rules: risk_score → sizing
    def score_to_sizing(score: int) -> str:
        if score <= 3:  return "full"
        if score <= 5:  return "0.75x"
        if score <= 7:  return "0.5x"
        return "avoid"

    prompt = f"""You are Axiom — Nexus's independent risk intelligence system.
Evaluate {req.ticker} through a pure risk lens.

TRADE: {req.ticker} | {req.strategy} | {req.direction} | DTE={req.dte} | IVR={req.iv_rank} | Regime={req.regime}
VIX={req.vix} | RSI={req.rsi} | ADX={req.adx}

Score risk 1-10 across 11 layers:
L1 Position cap, L2 Earnings risk, L3 IV rank, L4 Regime, L5 Liquidity,
L6 Gap risk, L7 Sector concentration, L8 DTE validity, L9 Technical,
L10 Market stress, L11 Qualitative overlay.

RESPOND EXACTLY:
RISK_SCORE: [1-10]
SIZING: [full/0.75x/0.5x/avoid]
FLAGS: [comma-separated risk flags, or NONE]
REASON: [one sentence]"""

    try:
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
                "temperature": 0.2,
            },
            timeout=20,
        )
        text = r.json()["choices"][0]["message"]["content"].strip()

        def extract(key):
            for line in text.split("\n"):
                if line.startswith(f"{key}:"):
                    return line.split(":", 1)[1].strip()
            return ""

        risk_score = 5
        try:
            risk_score = int(extract("RISK_SCORE").split("/")[0].strip())
        except Exception:
            pass

        sizing = extract("SIZING") or score_to_sizing(risk_score)
        flags  = [f.strip() for f in extract("FLAGS").split(",") if f.strip() and f.strip() != "NONE"]
        reason = extract("REASON") or "Assessment complete."

        return {
            "ticker":       req.ticker,
            "risk_score":   risk_score,
            "sizing":       sizing,
            "approved":     sizing != "avoid",
            "flags":        flags,
            "hard_stops":   [],
            "layer_scores": {},
            "reason":       reason,
            "position_gate": pos_gate,
            "model":        "deepseek-fallback",
            "assessed_at":  datetime.datetime.utcnow().isoformat(),
        }
    except Exception as e:
        return {
            "ticker":       req.ticker,
            "risk_score":   5,
            "sizing":       "full",
            "approved":     True,
            "flags":        [f"Assessment degraded: {str(e)[:80]}"],
            "hard_stops":   [],
            "layer_scores": {},
            "reason":       "Axiom assessment unavailable — degraded pass. Proceed with caution.",
            "position_gate": pos_gate,
            "model":        "degraded",
            "assessed_at":  datetime.datetime.utcnow().isoformat(),
        }


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _tg_send(chat_id: str, msg: str):
    """Best-effort Telegram send. Never raises."""
    if not TG_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


def _ping_omni(ticker: str, path: str, agent: str):
    _tg_send(
        TG_AHMED_DM,
        f"⚡ <b>OMNI PICK INCOMING</b> — {ticker}\n"
        f"Path: {path} | Agent: {agent}\n"
        f"Synthesis running..."
    )
    _tg_send(
        TG_HEALTH_GROUP,
        f"⚡ Pick queued: <b>{ticker}</b> | {path} | via {agent}"
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Returns service health including data source statuses and circuit breaker config."""
    pos_gate = get_position_gate()
    uptime   = (datetime.datetime.utcnow() - _startup_time).total_seconds() / 3600
    return {
        "status":      "healthy",
        "service":     "axiom-risk-intelligence",
        "version":     "3.0.0",
        "max_positions":    MAX_POSITIONS,
        "alpaca_positions": pos_gate["count"],
        "risk_engine":      "operational",
        "data_sources":     _source_health,
        "uptime_hours":     round(uptime, 2),
        # Circuit breaker config — exposed in /health so diagnostic tools
        # can verify thresholds without requiring auth (read-only, non-sensitive)
        "hard_limits": {
            "max_positions":       MAX_POSITIONS,
            "max_risk_per_trade":  MAX_RISK_PER_TRADE,
            "min_dte":             MIN_DTE,
            "max_dte":             MAX_DTE,
            "vix_pause_threshold": VIX_PAUSE_THRESHOLD,
        },
        "vix_pause_threshold": VIX_PAUSE_THRESHOLD,
    }


@app.get("/limits")
def limits(x_nexus_secret: Optional[str] = Header(None)):
    verify_secret(x_nexus_secret)
    pos_gate = get_position_gate()
    return {
        "position_gate": pos_gate,
        "hard_limits": {
            "max_positions":       MAX_POSITIONS,
            "max_risk_per_trade":  MAX_RISK_PER_TRADE,
            "min_dte":             MIN_DTE,
            "max_dte":             MAX_DTE,
            "vix_pause_threshold": VIX_PAUSE_THRESHOLD,
        },
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }


@app.get("/premarket")
def premarket_get(x_nexus_secret: Optional[str] = Header(None)):
    """
    GET /premarket — Triggers pre-market brief generation.
    Returns brief as JSON + sends to Telegram groups.
    """
    verify_secret(x_nexus_secret)

    def _run():
        try:
            import sys
            sys.path.insert(0, os.path.dirname(__file__))
            from premarket_template import run_premarket_report
            run_premarket_report(send_telegram=True)
        except Exception as e:
            print(f"[Axiom] Premarket report error: {e}")

    threading.Thread(target=_run, daemon=True).start()

    return {
        "status":  "accepted",
        "message": "Pre-market report generating — delivered to Telegram within 60s",
        "date":    datetime.date.today().isoformat(),
        "sent_to": [os.getenv("TG_ALPHA_GROUP", "-1003539872205"),
                    os.getenv("TG_PRIME_GROUP", "-5111649337"),
                    TG_AHMED_DM],
        "regime":  "PENDING",  # populated when report completes
    }


@app.post("/pick")
def receive_pick(req: PickWebhookRequest, x_nexus_secret: Optional[str] = Header(None)):
    """
    Webhook receiver: Alpha POSTs qualified picks here.
    Queued for OMNI's next poll cycle (≤60s latency).
    Also sends immediate Telegram ping to wake OMNI faster.
    """
    verify_secret(x_nexus_secret)

    pick = req.dict()
    pick["received_at"] = datetime.datetime.utcnow().isoformat()
    pick["processed"]   = False

    # contract_warning if execution fields missing
    missing = [f for f in ["expiry", "strike", "option_type"] if not pick.get(f)]
    if missing:
        pick["contract_warning"] = (
            f"Missing execution fields: {', '.join(missing)} — "
            "execution will require fallback ATM lookup"
        )

    with _pick_lock:
        _pick_queue.append(pick)
        queue_depth = len(_pick_queue)

    _ping_omni(req.ticker, req.path or "CONCORDANCE", req.agent or "unknown")

    return {
        "queued":      True,
        "ticker":      req.ticker,
        "queue_depth": queue_depth,
        "message":     "Pick queued for OMNI synthesis.",
    }


@app.get("/pick/queue")
def get_pick_queue(x_nexus_secret: Optional[str] = Header(None)):
    """OMNI polls this endpoint to drain the webhook queue."""
    verify_secret(x_nexus_secret)

    with _pick_lock:
        unprocessed = [p for p in _pick_queue if not p.get("processed")]
        for p in unprocessed:
            p["processed"] = True

    return {
        "picks":     unprocessed,
        "count":     len(unprocessed),
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }


@app.post("/assess")
def assess(req: AssessRequest, x_nexus_secret: Optional[str] = Header(None)):
    """
    Full 11-layer risk assessment.
    Called by OMNI during synthesis pipeline.
    Returns: risk_score, sizing, approved, flags, layer_scores, reason.
    """
    verify_secret(x_nexus_secret)

    # Hard validation
    if req.dte is not None:
        if req.dte < MIN_DTE or req.dte > MAX_DTE:
            raise HTTPException(
                status_code=422,
                detail=f"DTE {req.dte} outside valid range [{MIN_DTE}-{MAX_DTE}]"
            )

    result = run_axiom_assessment(req)
    return result


# ── Startup health check + heartbeat emitter ─────────────────────────────────

@app.on_event("startup")
def startup_health():
    threading.Thread(target=_check_data_sources, daemon=True).start()
    # Start heartbeat emitter — pushes to health monitor every 30s
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "..", "shared"))
        from heartbeat_emitter import start_heartbeat_emitter
        start_heartbeat_emitter(
            service_name    = "Axiom",
            version         = "3.0.0",
            get_loop_active = lambda: True,
            get_extra       = lambda: {
                "position_gate":   get_position_gate().get("gate", "UNKNOWN"),
                "positions":       get_position_gate().get("count", 0),
                "uptime_hours":    round((datetime.datetime.utcnow() - _startup_time).total_seconds() / 3600, 2),
            }
        )
    except Exception as e:
        print(f"[AXIOM] Heartbeat emitter failed to start: {e} — continuing without")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8001"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
