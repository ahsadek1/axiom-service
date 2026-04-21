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
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.sovereign_comms import get_instructions, report

import pytz
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from config import BASE_POSITION_SIZE, MIN_BRAINS_REQUIRED, load_settings
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
from synthesis import build_context, compute_verdict, _maybe_alert_brain_degradation
import oracle_client
from telegram import send_axiom_block_alert, send_synthesis_card

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("omni.main")


def _check_min_brains_required(value: int) -> None:
    """
    Assert that MIN_BRAINS_REQUIRED is at or above the safe minimum of 3.

    Called at startup to catch misconfigured thresholds before the service
    accepts any traffic.

    Args:
        value: The MIN_BRAINS_REQUIRED value to validate.

    Raises:
        RuntimeError: If value < 3.
    """
    if value < 3:
        raise RuntimeError(
            f"MIN_BRAINS_REQUIRED={value} is below safe minimum of 3. "
            "This is a system safety parameter — do not lower it."
        )

ET = pytz.timezone("America/New_York")

settings       = load_settings()
_auto_execute  = os.getenv("NEXUS_AUTO_EXECUTE", "false").lower() == "true"
logger.info("OMNI: NEXUS_AUTO_EXECUTE=%s", _auto_execute)
_state_lock    = threading.Lock()   # Pass B fix (V6): guards app_state counter mutations
app_state = {
    "settings":          settings,
    "start_time":        datetime.now(ET).isoformat(),
    "syntheses_today":   0,
    "go_verdicts_today": 0,
    "p4_dispatched_windows": set(),  # INV-15: (ticker, window_id) pairs — max 1 P4 per ticker per window
    "synthesized_concordances": set(),  # Dedup: (ticker, window_id, direction, system, pathway) — max 1 synthesis per concordance pathway per window [GENESIS 2026-04-20]
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
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
    from shared.db_guard import assert_unique_db_path  # S4: collision guard
    assert_unique_db_path("omni", settings.omni_db_path)
    logger.info("OMNI service starting...")
    # G8 SYS-1: MIN_BRAINS_REQUIRED safety guard — must be ≥ 3 for system integrity
    _check_min_brains_required(MIN_BRAINS_REQUIRED)
    logger.info("OMNI safety check: MIN_BRAINS_REQUIRED=%d ✓", MIN_BRAINS_REQUIRED)
    # G9 SYS-2: Auth registry validation
    from shared.auth_registry import validate_service_auth_config, AuthConfigError
    try:
        validate_service_auth_config("omni")
        logger.info("Auth registry validation passed for OMNI")
    except AuthConfigError as e:
        logger.critical("Auth config invalid: %s", e)
    # G11 SYS-4: Validate all 4 brain API keys at startup
    from shared.api_key_validator import ApiKeyValidator, ValidationResult as _VR
    _validator = ApiKeyValidator()
    _brain_results = [
        _validator.validate_anthropic(settings.anthropic_api_key),
        _validator.validate_openai(settings.openai_api_key),
        _validator.validate_gemini(settings.gemini_api_key),
        _validator.validate_deepseek(settings.deepseek_api_key),
    ]
    for _r in _brain_results:
        if _r.status == "failed":
            logger.critical("API key probe: %s → FAILED (%s)", _r.api_name, _r.message)
        elif _r.status == "degraded":
            logger.warning("API key probe: %s → DEGRADED (%s)", _r.api_name, _r.message)
        else:
            logger.info("API key probe: %s → %s (%s)", _r.api_name, _r.status, _r.message)
    init_db(settings.omni_db_path)

    # ── Restart-safe state reconstruction ────────────────────────────────────
    # Pre-populate synthesized_concordances from today's DB records so a
    # Railway restart (or any restart mid-day) cannot re-synthesize concordances
    # that were already processed this session. Without this, NVDA/AVGO with
    # strong scanners can immediately re-qualify and re-fire after deploy.
    try:
        from zoneinfo import ZoneInfo as _ZI
        _et_date_today = datetime.now(_ZI("America/New_York")).strftime("%Y-%m-%d")
        with __import__("database").get_conn(settings.omni_db_path) as _startup_conn:
            _prior_rows = _startup_conn.execute(
                """
                SELECT ticker, window_id, direction, system, pathway
                FROM synthesis_results
                WHERE SUBSTR(window_id, 1, 10) = ?
                """,
                (_et_date_today,),
            ).fetchall()
        _recovered = 0
        with _state_lock:
            for _pr in _prior_rows:
                _rkey = (
                    _pr["ticker"],
                    _pr["window_id"],
                    _pr["direction"],
                    _pr["system"],
                    _pr["pathway"],
                )
                app_state["synthesized_concordances"].add(_rkey)
                _recovered += 1
        logger.info(
            "OMNI restart recovery: pre-loaded %d synthesized_concordances from DB (date=%s)",
            _recovered, _et_date_today,
        )
    except Exception as _re:
        logger.warning(
            "OMNI restart recovery failed: %s — starting with empty synthesized_concordances",
            _re,
        )
    # ─────────────────────────────────────────────────────────────────────────

    logger.info("OMNI service ready — Quad Intelligence active")
    report("omni", "status", {"event": "started"})
    _instr = get_instructions("omni")
    if _instr:
        logger.info("OMNI: %d instruction(s) from SOVEREIGN on startup", len(_instr))
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
    _pathway = concordance.get("pathway", "alpha")
    _ticker  = concordance["ticker"]
    _win_id  = concordance.get("window_id", "unknown")

    # INV-15: Max 1 P4 signal per ticker per 15-minute window
    if concordance.get("pathway") == "P4":
        _p4_key = (concordance.get("ticker", ""), _win_id)
        with _state_lock:
            if _p4_key in app_state["p4_dispatched_windows"]:
                logger.warning(
                    "INV-15: P4 duplicate blocked — ticker=%s window=%s",
                    concordance.get("ticker"), _win_id,
                )
                return JSONResponse(
                    status_code=429,
                    content={
                        "accepted": False,
                        "reason": f"P4 already dispatched for {concordance.get('ticker')} in window {_win_id}",
                    },
                )
            app_state["p4_dispatched_windows"].add(_p4_key)
            # Prune stale window keys (keep only current and last window)
            current_win = datetime.now(ET).strftime("%Y-%m-%d-%H%M")
            app_state["p4_dispatched_windows"] = {
                k for k in app_state["p4_dispatched_windows"]
                if k[1] >= current_win[:13]  # keep today's entries
            }
    _trace_id = f"{_win_id}:{_ticker}"

    # Concordance dedup: max 1 synthesis per (ticker, window, direction, system, pathway).
    # The Alpha Buffer fires concordance events each time a new agent joins (P3→P2→P1
    # upgrade). Without dedup, a 3-agent concordance triggers 3 separate OMNI syntheses.
    # The 3rd synthesis gets degraded LLM votes (echo chamber, tired context) and
    # frequently downgrades from CONDITIONAL to NO_GO. One synthesis per pathway level
    # is the correct design — pathway upgrades get new synthesis, same pathway does not.
    # [GENESIS 2026-04-20: fixes triple-synthesis degradation on HD/IEMG today]
    _conc_key = (
        _ticker,
        _win_id,
        concordance.get("direction", ""),
        concordance.get("system", "alpha"),
        _pathway,
    )
    with _state_lock:
        if _conc_key in app_state["synthesized_concordances"]:
            logger.info(
                "Concordance dedup — already synthesized %s/%s pathway=%s window=%s",
                _ticker, concordance.get("direction", ""), _pathway, _win_id,
            )
            return JSONResponse(
                status_code=200,
                content={
                    "accepted": False,
                    "reason": f"Concordance already synthesized for {_ticker}/{_pathway}/{_win_id}",
                },
            )
        app_state["synthesized_concordances"].add(_conc_key)
        # Prune: keep only current window's entries to prevent unbounded growth
        _cur_win_prefix = _win_id[:13]  # "YYYY-MM-DD-HH"
        app_state["synthesized_concordances"] = {
            k for k in app_state["synthesized_concordances"]
            if k[1][:13] >= _cur_win_prefix
        }

    logger.info(
        "Concordance received: %s/%s/%s | pathway=%s | score=%.1f",
        _ticker,
        concordance["direction"],
        concordance["system"],
        _pathway,
        concordance["weighted_score"],
    )

    # Pipeline Sentinel — OMNI synthesis starting
    try:
        from pipeline_client import trace_hop as _trace_hop
        _trace_hop(_trace_id, "omni_started", "omni", _ticker, _pathway)
    except Exception:
        pass

    # ── Step 1: Axiom risk assessment ─────────────────────────────────────────
    axiom_result = assess_ticker(
        settings.axiom_url,
        settings.axiom_secret,
        concordance["ticker"],
    )

    # ── Step 2: Current regime ────────────────────────────────────────────────
    regime = get_regime(settings.axiom_url, settings.axiom_secret)

    # ── Step 3: Fetch ORACLE intelligence for this ticker ─────────────────────
    # Brains require real market data to make informed GO/NO_GO decisions.
    # Without ORACLE context, all 4 brains correctly default to NO_GO.
    oracle_ctx = oracle_client.get_context(concordance["ticker"])
    if oracle_ctx is None:
        logger.warning("ORACLE context unavailable for %s — brains will operate with degraded context",
                       concordance["ticker"])

    # ── Step 4: Build context ─────────────────────────────────────────────────
    context = build_context(concordance, axiom_result, regime, oracle_ctx)

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

    # G8 SYS-1: Alert on brain degradation (< 4 brains responded)
    if verdict.brains_responded < 4:
        _maybe_alert_brain_degradation(
            ticker           = concordance["ticker"],
            brains_responded = verdict.brains_responded,
            brain_summary    = verdict.brain_summary,
            bot_token        = settings.telegram_bot_token,
            chat_id          = settings.ahmed_chat_id,
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

    # Pipeline Sentinel — OMNI synthesis completed
    try:
        from pipeline_client import trace_hop as _trace_hop
        _trace_hop(_trace_id, "omni_completed", "omni", _ticker, _pathway)
    except Exception:
        pass

    logger.info(
        "Synthesis complete: %s/%s/%s | verdict=%s | votes=%d/4 | exec=%s",
        concordance["ticker"],
        concordance["direction"],
        concordance["system"],
        verdict.verdict,
        verdict.votes_go,
        execution_ok,
    )
    _msg_type = "alert" if verdict.verdict in ("GO", "STRONG_GO") else "status"
    report("omni", _msg_type, {
        "event":     "synthesis_complete",
        "ticker":    concordance["ticker"],
        "direction": concordance["direction"],
        "pathway":   concordance["pathway"],
        "verdict":   verdict.verdict,
        "votes_go":  verdict.votes_go,
        "brains":    verdict.brains_responded,
        "exec_ok":   execution_ok,
    })

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
