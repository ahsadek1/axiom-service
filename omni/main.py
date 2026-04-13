"""
main.py — OMNI Service FastAPI Entry Point

Receives concordance from Alpha and Prime buffers.
Runs Quad Intelligence synthesis. Routes GO to execution.
Notifies Ahmed via Telegram on every synthesis.

Endpoints:
  POST /concordance       — Receive concordance from Alpha or Prime buffer
  GET  /health            — Always 200
  GET  /status            — Recent syntheses + service state
  GET  /synthesis/{id}    — Full synthesis detail by row ID
"""

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

import pytz
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from config import BASE_POSITION_SIZE, load_settings
from axiom_client import assess_ticker, get_regime
from database import (
    get_recent_syntheses,
    get_synthesis_result,
    init_db,
    mark_execution_dispatched,
    save_synthesis_result,
)
from execution_router import calculate_position_size, route_to_execution
from quad_intelligence import run_all_brains
from synthesis import build_context, compute_verdict
from telegram import send_axiom_block_alert, send_synthesis_card

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("omni.main")

ET = pytz.timezone("America/New_York")

settings       = load_settings()
_state_lock    = threading.Lock()   # Pass B fix (V6): guards app_state counter mutations
app_state = {
    "settings":          settings,
    "start_time":        datetime.now(ET).isoformat(),
    "syntheses_today":   0,
    "go_verdicts_today": 0,
}


def verify_secret(secret_header: str) -> None:
    """
    Verify the caller's secret matches the configured NEXUS_WEBHOOK_SECRET.
    Uses constant-time comparison to prevent timing attacks (Cipher Finding 3).

    Raises:
        HTTPException: 403 if missing or invalid.
    """
    import secrets as _sec
    if not secret_header or not _sec.compare_digest(secret_header, settings.nexus_secret):
        raise HTTPException(status_code=403, detail="Forbidden")


def verify_concordance_auth(
    x_nexus_secret: str,
    x_nexus_prime_secret: str,
) -> None:
    """
    Validate inbound /concordance auth for BOTH Alpha and Prime Buffer callers.

    Cipher Finding 4 (INV-11 rotation fix): the original `active_secret or` pattern
    validated both headers against nexus_secret only. On secret rotation this silently
    breaks Prime trading. Each header is now validated against its own secret using
    constant-time comparison.

    Alpha Buffer sends X-Nexus-Secret  → validated against nexus_secret.
    Prime Buffer sends X-Nexus-Prime-Secret → validated against nexus_prime_secret.
    """
    import secrets as _sec
    alpha_ok = bool(x_nexus_secret and _sec.compare_digest(x_nexus_secret, settings.nexus_secret))
    prime_ok = bool(x_nexus_prime_secret and _sec.compare_digest(
        x_nexus_prime_secret, settings.nexus_prime_secret
    ))
    if not (alpha_ok or prime_ok):
        raise HTTPException(status_code=403, detail="Forbidden")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize DB on startup."""
    logger.info("OMNI service starting...")
    init_db(settings.omni_db_path)
    logger.info("OMNI service ready — Quad Intelligence active")
    yield
    logger.info("OMNI service stopped")


app = FastAPI(
    title       = "OMNI Synthesis Engine",
    description = "Nexus Trading System — Quad Intelligence synthesis and routing",
    version     = "3.0.0",
    lifespan    = lifespan,
)


# ── Request Model ─────────────────────────────────────────────────────────────

class ConcordancePayload(BaseModel):
    """Concordance payload from Alpha or Prime buffer."""

    ticker:          str
    direction:       str
    system:          str          # 'alpha' or 'prime'
    pathway:         str          # P1, P2, P3, P4
    weighted_score:  float
    agents_involved: list[str]
    scores:          dict[str, float]
    verdict:         str
    sizing_mult:     float
    window_id:       str
    echo_chamber:    bool = False
    notes:           list[str] = []

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("direction")
    @classmethod
    def normalize_direction(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("bullish", "bearish"):
            raise ValueError("direction must be 'bullish' or 'bearish'")
        return v

    @field_validator("system")
    @classmethod
    def normalize_system(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("alpha", "prime"):
            raise ValueError("system must be 'alpha' or 'prime'")
        return v

    @field_validator("pathway")
    @classmethod
    def validate_pathway(cls, v: str) -> str:
        v = v.upper().strip()
        if v not in ("P1", "P2", "P3", "P4"):
            raise ValueError("pathway must be P1, P2, P3, or P4")
        return v

    @field_validator("agents_involved")
    @classmethod
    def validate_agents(cls, v: list[str]) -> list[str]:
        """Reject unknown agent names to prevent rogue submissions biasing concordance.

        Cipher Finding 14 fix: normalize to lowercase before validation.
        Spec defines agents as lowercase; code had mixed-case set causing spec/code mismatch.
        """
        v = [a.lower() for a in v]
        VALID_AGENTS = {"cipher", "atlas", "sage"}
        unknown = [a for a in v if a not in VALID_AGENTS]
        if unknown:
            raise ValueError(f"Unknown agent(s): {unknown}. Valid: {sorted(VALID_AGENTS)}")
        return v

    @field_validator("sizing_mult")
    @classmethod
    def clamp_sizing_mult(cls, v: float) -> float:
        """Clamp sizing_mult to safe range — prevents zero/negative/extreme positions."""
        MIN, MAX = 0.1, 2.0
        if v < MIN or v > MAX:
            raise ValueError(f"sizing_mult {v} out of safe range [{MIN}, {MAX}]")
        return round(v, 4)

    @field_validator("weighted_score")
    @classmethod
    def validate_weighted_score(cls, v: float) -> float:
        """Reject nonsensical scores — must be 0–100."""
        if not (0.0 <= v <= 100.0):
            raise ValueError(f"weighted_score {v} must be between 0 and 100")
        return round(v, 4)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> JSONResponse:
    """Health check. Always returns 200. No auth required."""
    return JSONResponse({
        "status":             "healthy",
        "service":            "omni",
        "version":            "3.0.0",
        "syntheses_today":    app_state["syntheses_today"],
        "go_verdicts_today":  app_state["go_verdicts_today"],
        "uptime_since":       app_state["start_time"],
    })


@app.post("/concordance")
def receive_concordance(
    body: ConcordancePayload,
    x_nexus_secret:       str = Header(default=""),
    x_nexus_prime_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Receive a concordance signal and run Quad Intelligence synthesis.

    Accepts from Alpha Buffer (X-Nexus-Secret) and Prime Buffer (X-Nexus-Prime-Secret).

    Pass B fix (Finding 2): Prime Buffer sends X-Nexus-Prime-Secret but OMNI only
    read X-Nexus-Secret — every Prime concordance was rejected with 403. Fixed by
    accepting either header and validating against the appropriate secret.

    Full pipeline:
      1. Verify auth
      2. Fetch Axiom risk assessment
      3. Fetch current regime
      4. Build complete context for all 4 brains
      5. Run all 4 brains in parallel
      6. Compute verdict (vote counting + Axiom hard stop check)
      7. Persist synthesis result
      8. Route to execution (if GO/STRONG_GO) — fully autonomous, no human gate
      9. Notify Ahmed via Telegram (GO/STRONG_GO only — NO_GO is silent)

    Returns immediately with synthesis result.
    """
    # Cipher Finding 4 fix: validate each header against its own secret.
    # Replaces the original `active_secret = x or y; verify_secret(active_secret)` pattern
    # which validated both against nexus_secret — broken on secret rotation.
    verify_concordance_auth(x_nexus_secret, x_nexus_prime_secret)

    concordance = body.model_dump()
    logger.info(
        "Concordance received: %s/%s/%s | pathway=%s | score=%.1f",
        concordance["ticker"],
        concordance["direction"],
        concordance["system"],
        concordance["pathway"],
        concordance["weighted_score"],
    )

    # ── Step 1: Axiom risk assessment ─────────────────────────────────────────
    axiom_result = assess_ticker(
        settings.axiom_url,
        settings.axiom_secret,
        concordance["ticker"],
    )

    # ── Step 2: Current regime ────────────────────────────────────────────────
    regime = get_regime(settings.axiom_url, settings.axiom_secret)

    # ── Step 3: Build context ─────────────────────────────────────────────────
    context = build_context(concordance, axiom_result, regime)

    # ── Step 4: Quad Intelligence ─────────────────────────────────────────────
    brain_results = run_all_brains(
        context          = context,
        anthropic_api_key = settings.anthropic_api_key,
        openai_api_key   = settings.openai_api_key,
        gemini_api_key   = settings.gemini_api_key,
        deepseek_api_key = settings.deepseek_api_key,
    )

    # ── Step 5: Compute verdict ───────────────────────────────────────────────
    verdict = compute_verdict(
        brain_results      = brain_results,
        pathway            = concordance["pathway"],
        concordance_sizing = concordance["sizing_mult"],
        axiom_result       = axiom_result,
    )

    # ── Step 6: Calculate position size ──────────────────────────────────────
    position_size = calculate_position_size(
        base_size   = BASE_POSITION_SIZE,
        sizing_mult = verdict.sizing_mult,
        pathway     = concordance["pathway"],
    )

    # ── Step 7: Persist synthesis ─────────────────────────────────────────────
    synthesis_id = save_synthesis_result(
        db_path              = settings.omni_db_path,
        window_id            = concordance["window_id"],
        ticker               = concordance["ticker"],
        direction            = concordance["direction"],
        system               = concordance["system"],
        pathway              = concordance["pathway"],
        agent_weighted_score = concordance["weighted_score"],
        brain_results        = brain_results,
        votes_go             = verdict.votes_go,
        brains_responded     = verdict.brains_responded,
        echo_chamber_flagged = verdict.echo_chamber_flagged,
        verdict              = verdict.verdict,
        verdict_notes        = " | ".join(verdict.notes),
        axiom_result         = axiom_result,
    )

    with _state_lock:                          # Pass B fix (V6): thread-safe counter
        app_state["syntheses_today"] += 1

    # ── Step 8: Route to execution ────────────────────────────────────────────
    execution_ok = None
    if verdict.can_execute():
        # Pass B fix #1: use system-appropriate secret for execution routing.
        # Alpha Execution expects X-Nexus-Secret = nexus_secret.
        # Prime Execution expects X-Nexus-Prime-Secret = nexus_prime_secret.
        exec_auth = (
            settings.nexus_secret
            if concordance["system"] == "alpha"
            else settings.nexus_prime_secret
        )
        exec_ok, exec_resp = route_to_execution(
            system             = concordance["system"],
            alpha_exec_url     = settings.alpha_execution_url,
            prime_exec_url     = settings.prime_execution_url,
            auth_secret        = exec_auth,
            concordance        = concordance,
            synthesis_verdict  = verdict.to_dict(),
            position_size      = position_size,
        )
        execution_ok = exec_ok
        mark_execution_dispatched(
            settings.omni_db_path,
            synthesis_id,
            (settings.alpha_execution_url if concordance["system"] == "alpha"
             else settings.prime_execution_url),
            exec_resp,
        )
        if exec_ok:
            with _state_lock:                  # Pass B fix (V6): thread-safe counter
                app_state["go_verdicts_today"] += 1

    # ── Step 9: Telegram notifications ───────────────────────────────────────
    # Axiom hard-stop blocked → alert Ahmed (Cipher Finding 6 fix)
    # GO/STRONG_GO → full synthesis card
    # CONDITIONAL  → brief system health alert
    # NO_GO        → silent drop (unless axiom_blocked — see above)
    if verdict.axiom_blocked:
        send_axiom_block_alert(
            bot_token  = settings.telegram_bot_token,
            chat_id    = settings.ahmed_chat_id,
            ticker     = concordance["ticker"],
            hard_stops = getattr(verdict, "axiom_hard_stops", []),
            regime     = context.get("regime", {}).get("classification", "UNKNOWN"),
        )

    if verdict.verdict in ("GO", "STRONG_GO"):
        send_synthesis_card(
            bot_token     = settings.telegram_bot_token,
            chat_id       = settings.ahmed_chat_id,
            concordance   = concordance,
            verdict       = verdict,
            brain_results = brain_results,
            position_size = position_size,
            execution_ok  = execution_ok,
        )
    elif verdict.verdict == "CONDITIONAL":
        # CONDITIONAL = system degradation or borderline result. Alert Ahmed briefly.
        send_conditional_alert(
            bot_token   = settings.telegram_bot_token,
            chat_id     = settings.ahmed_chat_id,
            ticker      = concordance["ticker"],
            pathway     = concordance["pathway"],
            votes_go    = verdict.votes_go,
            brains_resp = verdict.brains_responded,
            notes       = verdict.notes,
        )

    logger.info(
        "Synthesis complete: %s/%s/%s | verdict=%s | votes=%d/4 | exec=%s",
        concordance["ticker"],
        concordance["direction"],
        concordance["system"],
        verdict.verdict,
        verdict.votes_go,
        execution_ok,
    )

    return JSONResponse({
        "synthesis_id":   synthesis_id,
        "ticker":         concordance["ticker"],
        "direction":      concordance["direction"],
        "system":         concordance["system"],
        "pathway":        concordance["pathway"],
        "verdict":        verdict.verdict,
        "votes_go":       verdict.votes_go,
        "brains_responded": verdict.brains_responded,
        "sizing_mult":    verdict.sizing_mult,
        "position_size":  position_size,
        "execution_ok":   execution_ok,
        "echo_chamber":   verdict.echo_chamber_flagged,
        "axiom_blocked":  verdict.axiom_blocked,
        "notes":          verdict.notes,
    })


@app.get("/status")
def status(x_nexus_secret: str = Header(default="")) -> JSONResponse:
    """Return recent syntheses and service state."""
    verify_secret(x_nexus_secret)
    recent = get_recent_syntheses(settings.omni_db_path, limit=10)
    return JSONResponse({
        "service":            "omni",
        "syntheses_today":    app_state["syntheses_today"],
        "go_verdicts_today":  app_state["go_verdicts_today"],
        "recent_syntheses":   recent,
        "checked_at":         datetime.now(ET).isoformat(),
    })


@app.get("/synthesis/{synthesis_id}")
def get_synthesis(
    synthesis_id:   int,
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Return full synthesis detail by row ID.

    Args:
        synthesis_id: Integer ID of the synthesis result.
    """
    verify_secret(x_nexus_secret)
    with __import__("database").get_conn(settings.omni_db_path) as conn:
        row = conn.execute(
            "SELECT * FROM synthesis_results WHERE id=?",
            (synthesis_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"Synthesis {synthesis_id} not found")

    return JSONResponse(dict(row))


# ── Internal helpers ──────────────────────────────────────────────────────────

def send_conditional_alert(
    bot_token:   str,
    chat_id:     str,
    ticker:      str,
    pathway:     str,
    votes_go:    int,
    brains_resp: int,
    notes:       list,
) -> None:
    """
    Send a brief CONDITIONAL system alert to Ahmed.
    CONDITIONAL = no trade executed, but system health is impaired or borderline.

    Args:
        bot_token:   Telegram bot token.
        chat_id:     Ahmed's chat ID.
        ticker:      Ticker that triggered the conditional.
        pathway:     Concordance pathway.
        votes_go:    Number of GO votes from brains.
        brains_resp: Number of brains that responded.
        notes:       Synthesis notes explaining the conditional.
    """
    import requests
    reason = notes[0] if notes else "borderline or degraded"
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "parse_mode": "HTML",
                "text": (
                    f"⚠️ <b>OMNI CONDITIONAL — {ticker}</b>\n"
                    f"Pathway: {pathway} | Brains: {brains_resp}/4 responded | GO votes: {votes_go}/4\n"
                    f"Reason: {reason}\n"
                    f"<i>No trade executed. Monitor quad intelligence health.</i>"
                ),
            },
            timeout=8,
        )
    except Exception:
        pass
