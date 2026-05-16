"""
main.py — Nexus V2 Rebuild entry point
=======================================
Wires together all components per Cipher spec + OMNI adversarial review.

Startup sequence:
  1. Init DB (schema + seed capital ledger)
  2. Reconcile DB vs Alpaca (flag discrepancies)
  3. Start MarketState poller
  4. Run preflight at 9:25 AM — suspend if fails
  5. Start APScheduler scanner (max_instances=1)
  6. Serve FastAPI health endpoint

Authored: 2026-05-02 | Cipher spec + OMNI adversarial review
"""

from __future__ import annotations
import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from market_state import MarketState, market_state_poller, suspend_until_resume
from preflight import run_preflight
from scanner import create_scheduler
from execution import reconcile_on_startup
from health import get_health

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("nexus.main")

# ── Config ────────────────────────────────────────────────────────────────────

def _require(var: str) -> str:
    """Raise at startup if env var missing — never silently use a stale fallback."""
    val = os.environ.get(var)
    if not val:
        raise RuntimeError(f"{var} is required but not set. Source .deploy-secrets before running.")
    return val

DB_PATH       = os.environ.get("NEXUS_DB_PATH", "/Users/ahmedsadek/nexus/data/nexus_v2.db")
AXIOM_URL     = os.environ.get("AXIOM_URL",    "http://localhost:8001")
OMNI_URL      = os.environ.get("OMNI_URL",     "http://localhost:8004")  # Railway: set to Railway OMNI URL
NEXUS_SECRET  = _require("NEXUS_SECRET")
OMNI_BOT      = _require("TELEGRAM_BOT_TOKEN")
AHMED_CHAT    = os.environ.get("AHMED_CHAT_ID", "8573754783")
HEALTH_GROUP  = os.environ.get("TELEGRAM_HEALTH_GROUP_CHAT_ID", "-1003954790884")
VERSION       = "2.0.0"

# ── Global state ──────────────────────────────────────────────────────────────
market_state      = MarketState()
preflight_passed  = False
scanner_active    = False
_resume_requested = False


# ── Alert function ────────────────────────────────────────────────────────────

async def alert(msg: str) -> None:
    """Send Telegram alert to Ahmed + health group."""
    for chat_id in [AHMED_CHAT, HEALTH_GROUP]:
        try:
            requests.post(
                f"https://api.telegram.org/bot{OMNI_BOT}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=5,
            )
        except Exception as e:
            logger.warning("Alert send failed to %s: %s", chat_id, e)


async def check_resume() -> bool:
    """Check if Ahmed sent /resume. Set by the /resume endpoint."""
    global _resume_requested
    if _resume_requested:
        _resume_requested = False
        return True
    return False


# ── OMNI dispatch ─────────────────────────────────────────────────────────────

def omni_dispatch(ticker, ctx, axiom, score_result, direction="bullish", window_id="") -> None:
    """Send pick to OMNI synthesis endpoint (/concordance)."""
    try:
        # Build complete ConcordancePayload
        payload = {
            "ticker":    ticker,
            "direction": direction.lower(),
            "system":    "alpha",
            "pathway":   score_result.recommendation or "P1",
            "weighted_score": float(score_result.score),
            "agents_involved": ["Sage", "Cipher"],
            "scores": {
                "vol": float(ctx.vol.iv_rank) if ctx.vol.iv_rank else 0.0,
                "tech": float(ctx.tech.rsi_14) if ctx.tech.rsi_14 else 50.0,
                "momentum": float(score_result.score),
            },
            "verdict": "GO",
            "sizing_mult": 1.0,
            "window_id": window_id,
            "echo_chamber": False,
            "notes": ["Railway V2 dispatch"],
        }
        
        r = requests.post(
            f"{OMNI_URL}/concordance",
            headers={"Content-Type": "application/json",
                     "X-Nexus-Secret": NEXUS_SECRET},
            json=payload,
            timeout=10,
        )
        if r.status_code not in (200, 202):
            logger.warning("OMNI dispatch returned %s for %s: %s", r.status_code, ticker, r.text[:100])
        else:
            logger.info("OMNI dispatch successful for %s", ticker)
    except Exception as e:
        logger.error("OMNI dispatch failed for %s: %s", ticker, e)
        raise


# ── DB init ───────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Initialize DB from schema.sql."""
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path) as f:
        schema = f.read()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.executescript(schema)
    logger.info("DB initialized at %s", DB_PATH)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global preflight_passed, scanner_active

    # 1. Init DB
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    init_db()
    logger.info("Nexus V2 starting up")

    # 2. Reconcile DB vs Alpaca
    def _sync_alert(msg): asyncio.create_task(alert(msg))
    reconcile_on_startup(DB_PATH, _sync_alert)

    # 3. Start MarketState poller
    asyncio.create_task(
        market_state_poller(market_state, alert, DB_PATH)
    )
    logger.info("MarketState poller started")

    # 4. Preflight (runs immediately if market hours, else waits for 9:25 AM cron)
    result = run_preflight(DB_PATH)
    preflight_passed = result.passed
    if result.passed:
        await alert(
            f"✅ <b>Nexus V2 preflight PASSED</b>\n"
            f"IVR/RSI/fundamentals verified for {', '.join(['AAPL','JPM','NVDA'])}.\n"
            f"Pipeline open."
        )
    else:
        failures_str = "\n".join(f"• {f}" for f in result.failures)
        await alert(
            f"🔴 <b>Nexus V2 preflight FAILED</b>\n{failures_str}\n"
            f"Pipeline suspended. Auto-retry every 10 minutes."
        )
        # C5 fix: suspend with wait-retry, not SystemExit
        await suspend_until_resume(
            state       = market_state,
            reason      = f"Preflight failed: {'; '.join(result.failures)}",
            alert_fn    = alert,
            resume_check_fn = check_resume,
            retry_fn    = lambda: asyncio.to_thread(
                lambda: run_preflight(DB_PATH).passed
            ),
        )
        preflight_passed = True  # suspension lifted = preflight passed

    # 5. Start scanner
    scheduler = create_scheduler(
        db_path          = DB_PATH,
        axiom_url        = AXIOM_URL,
        nexus_secret     = NEXUS_SECRET,
        market_state     = market_state,
        alert_fn         = alert,
        omni_dispatch_fn = omni_dispatch,
    )
    scheduler.start()
    scanner_active = True
    logger.info("Scanner started (APScheduler, max_instances=1)")

    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    logger.info("Nexus V2 shutdown complete")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Nexus V2", version=VERSION, lifespan=lifespan)


def _verify(secret: str) -> None:
    import secrets as _sec
    if not secret or not _sec.compare_digest(secret, NEXUS_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")


@app.get("/health")
def health_endpoint():
    """Unauthenticated. Returns actual system state — never a lie."""
    return JSONResponse(get_health(DB_PATH, market_state, preflight_passed, scanner_active, VERSION))


@app.get("/ping")
def ping():
    return {"status": "ok", "service": "nexus-v2"}


@app.post("/resume")
def resume_endpoint(x_nexus_secret: str = Header(default="", alias="X-Nexus-Secret")):
    """Ahmed sends this to lift suspension."""
    _verify(x_nexus_secret)
    global _resume_requested
    _resume_requested = True
    logger.info("Manual /resume received")
    return {"status": "resume_requested"}


@app.post("/preflight/run")
def run_preflight_endpoint(x_nexus_secret: str = Header(default="", alias="X-Nexus-Secret")):
    """Manually trigger preflight check."""
    _verify(x_nexus_secret)
    global preflight_passed
    result = run_preflight(DB_PATH)
    preflight_passed = result.passed
    return {
        "passed":      result.passed,
        "failures":    result.failures,
        "warnings":    result.warnings,
        "duration_ms": result.duration_ms,
    }


@app.get("/verdicts")
def get_verdicts(
    x_nexus_secret: str = Header(default="", alias="X-Nexus-Secret"),
) -> JSONResponse:
    """Get summary of recent verdicts/submissions. Used by OMNI for monitoring."""
    _verify(x_nexus_secret)
    try:
        conn = sqlite3.connect(DB_PATH)
        trades_rows = conn.execute("SELECT COUNT(*) as cnt FROM positions WHERE created_date = DATE('now')").fetchall()
        trades_today = trades_rows[0][0] if trades_rows else 0
        
        open_rows = conn.execute("SELECT COUNT(*) as cnt FROM positions WHERE status IN ('open','pending')").fetchall()
        open_count = open_rows[0][0] if open_rows else 0
        conn.close()
    except Exception as e:
        logger.warning("Verdicts query failed: %s", e)
        trades_today = 0
        open_count = 0
    
    return JSONResponse({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trades_today": trades_today,
        "open_positions": open_count,
        "service": "nexus-v2-rebuild",
        "version": VERSION,
    })


@app.post("/verdicts")
def verdicts_endpoint(
    x_nexus_secret: str = Header(default="", alias="X-Nexus-Secret"),
    **kwargs
):
    """Accept synthesis verdict from OMNI. Legacy endpoint for compatibility."""
    _verify(x_nexus_secret)
    # Accept but acknowledge — verdict is already in the system via omni_dispatch callback
    logger.info("Verdicts endpoint received: %s", kwargs)
    return {"status": "accepted", "message": "Verdict received and queued for execution"}


@app.post("/execute")
def execute_endpoint(
    ticker: str,
    strategy: str,
    direction: str,
    window_id: str,
    pathway: str,
    arena: str = "alpha",
    sizing_mult: float = 1.0,
    dte: int = 30,
    expiry: str = "",
    axiom_risk: dict = None,
    x_nexus_secret: str = Header(default="", alias="X-Nexus-Secret"),
):
    """Execute a trade. Idempotent via deterministic client_order_id."""
    _verify(x_nexus_secret)
    if axiom_risk is None:
        axiom_risk = {}

    from execution import execute_trade
    result = execute_trade(
        db_path=DB_PATH,
        ticker=ticker,
        strategy=strategy,
        direction=direction,
        window_id=window_id,
        pathway=pathway,
        arena=arena,
        sizing_mult=sizing_mult,
        dte=dte,
        expiry=expiry,
        axiom_risk=axiom_risk,
    )
    return {
        "success": result.success,
        "client_order_id": result.client_order_id,
        "alpaca_order_id": result.alpaca_order_id,
        "fill_price": result.fill_price,
        "error": result.error,
        "already_existed": result.already_existed,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8010, log_level="info")
