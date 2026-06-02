"""
THESIS — Market Intelligence Agent.

FastAPI service on port 8060.  Hosts all nine REST endpoints and manages
APScheduler cron jobs for weekly thesis generation, daily brief, and
Layer 3 real-time monitoring.

Startup sequence:
  1. Load .env
  2. Run CHRONICLE schema migration
  3. Initialise all clients
  4. Start APScheduler (weekly, daily, Layer 3 interval)
  5. Serve requests
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import asyncio
import sqlite3 as _sqlite3

import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from migration import run_migration
from models import (
    CurrentContextResponse,
    GenerateResponse,
    HealthResponse,
    MarketSnapshot,
    PerformanceMetrics,
    ThesisContext,
    ThesisEvent,
)
from clients.chronicle_client import ChronicleClient, ResilientChronicleClient
from clients.fallback_client import FallbackChronicleClient
from clients.oracle_client import OracleClient
from clients.brave_client import BraveClient
from clients.perplexity_client import PerplexityClient
from clients.news_client import ResilientNewsClient
from clients.sovereign_client import SovereignClient
from clients.telegram_client import TelegramClient
from clients.telegram_bot_handler import TelegramBotHandler
from thesis_engine import ThesisEngine
from layers.weekly import WeeklyThesisGenerator
from layers.daily import DailyBriefGenerator
from layers.monitor import RealTimeMonitor

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Globals (populated during lifespan)
# ---------------------------------------------------------------------------

_chronicle: Optional[ResilientChronicleClient] = None
_oracle: Optional[OracleClient] = None
_news: Optional[ResilientNewsClient] = None
_sovereign: Optional[SovereignClient] = None
_telegram: Optional[TelegramClient] = None
_engine: Optional[ThesisEngine] = None
_weekly_gen: Optional[WeeklyThesisGenerator] = None
_daily_gen: Optional[DailyBriefGenerator] = None
_monitor: Optional[RealTimeMonitor] = None
_scheduler: Optional[AsyncIOScheduler] = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Fix 2 — Startup gate helpers
# ---------------------------------------------------------------------------

def _validate_chronicle_ready_sync(db_path: str) -> bool:
    """Synchronously validate that CHRONICLE SQLite is accessible and both
    thesis tables exist and are writable.

    Performs a BEGIN / INSERT / ROLLBACK test on each table — no data written.

    Args:
        db_path: Path to chronicle.db.

    Returns:
        True if file exists and both tables pass the write test.
    """
    from pathlib import Path as _Path
    if not _Path(db_path).exists():
        logger.error("CHRONICLE startup gate: file not found at %s", db_path)
        return False
    try:
        conn = _sqlite3.connect(db_path, timeout=5)
        # Test thesis_context writable
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO thesis_context "
            "(layer, valid_from, valid_until, trading_posture, sizing_multiplier, "
            "macro_gate, risk_reward_gate, thesis_sentence, confidence_adjustment) "
            "VALUES ('_test', datetime('now'), datetime('now'), 'NEUTRAL', 1.0, "
            "'PASS', 'PASS', '_startup_validation', 0)"
        )
        conn.execute("ROLLBACK")
        # Test thesis_events writable
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO thesis_events (event_type, detected_at, description) "
            "VALUES ('_test', datetime('now'), '_startup_validation')"
        )
        conn.execute("ROLLBACK")
        conn.close()
        return True
    except _sqlite3.Error as exc:
        logger.error("CHRONICLE startup gate: validation failed — %s", exc)
        return False
    except Exception as exc:
        logger.error("CHRONICLE startup gate: unexpected error — %s", exc, exc_info=True)
        return False


def _check_table_writable_sync(db_path: str, table: str) -> bool:
    """Check a single CHRONICLE table is writable via BEGIN/INSERT/ROLLBACK.

    Used by the health endpoint (Fix 3) for SOVEREIGN preflight.

    Args:
        db_path: Path to chronicle.db.
        table: Table name — 'thesis_context' or 'thesis_events'.

    Returns:
        True if the table is writable.
    """
    from pathlib import Path as _Path
    if not _Path(db_path).exists():
        return False
    try:
        conn = _sqlite3.connect(db_path, timeout=5)
        conn.execute("BEGIN")
        if table == "thesis_context":
            conn.execute(
                "INSERT INTO thesis_context "
                "(layer, valid_from, valid_until, trading_posture, sizing_multiplier, "
                "macro_gate, risk_reward_gate, thesis_sentence, confidence_adjustment) "
                "VALUES ('_healthcheck', datetime('now'), datetime('now'), 'NEUTRAL', "
                "1.0, 'PASS', 'PASS', '_healthcheck', 0)"
            )
        elif table == "thesis_events":
            conn.execute(
                "INSERT INTO thesis_events (event_type, detected_at, description) "
                "VALUES ('_healthcheck', datetime('now'), '_healthcheck')"
            )
        else:
            conn.execute("ROLLBACK")
            conn.close()
            return False
        conn.execute("ROLLBACK")
        conn.close()
        return True
    except _sqlite3.Error:
        return False
    except Exception:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: start scheduler on startup, stop on shutdown."""
    global _chronicle, _oracle, _perplexity, _sovereign, _telegram
    global _engine, _weekly_gen, _daily_gen, _monitor, _scheduler
    global _telegram_bot, _polling_task

    # ---- Schema migration
    db_path = os.getenv("CHRONICLE_DB_PATH", "/Users/ahmedsadek/nexus/data/chronicle.db")
    run_migration(db_path)

    # ---- Clients
    _primary_chronicle = ChronicleClient(db_path=db_path)
    _fallback_chronicle = FallbackChronicleClient()
    _chronicle = ResilientChronicleClient(
        primary=_primary_chronicle,
        fallback=_fallback_chronicle,
    )
    _oracle = OracleClient(
        base_url=os.getenv("ORACLE_URL", "http://192.168.1.146:8007"),
        auth_token=os.getenv("ORACLE_AUTH_TOKEN", ""),
    )
    _news = ResilientNewsClient(
        perplexity=PerplexityClient(api_key=os.getenv("PERPLEXITY_API_KEY", "")),
        brave=BraveClient(api_key=os.getenv("BRAVE_API_KEY", "")),
    )
    _sovereign = SovereignClient(
        bus_url=os.getenv("SOVEREIGN_BUS_URL", "http://192.168.1.141:9999")
    )
    _telegram = TelegramClient(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.getenv("AHMED_CHAT_ID", ""),
    )
    _telegram_bot = TelegramBotHandler(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.getenv("AHMED_CHAT_ID", ""),
        http_base_url=f"http://localhost:{os.getenv('THESIS_PORT', '8060')}",
    )

    # ---- Engine + generators
    _engine = ThesisEngine(
        oracle=_oracle,
        brave=_news,
        chronicle=_chronicle,
        sovereign=_sovereign,
    )
    _ai_client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    _weekly_gen = WeeklyThesisGenerator(engine=_engine, anthropic_client=_ai_client)
    _daily_gen = DailyBriefGenerator(engine=_engine, anthropic_client=_ai_client)
    _monitor = RealTimeMonitor(
        chronicle=_chronicle,
        sovereign=_sovereign,
        telegram=_telegram,
    )

    # ---- Fix 2: CHRONICLE startup gate — must pass before APScheduler fires
    _scheduler = AsyncIOScheduler(timezone=str(_ET))
    attempt = 0
    while True:
        attempt += 1
        ready = await asyncio.to_thread(_validate_chronicle_ready_sync, db_path)
        if ready:
            logger.info(
                "CHRONICLE validated and tables writable — starting APScheduler (attempt %d)",
                attempt,
            )
            break
        logger.error(
            "CHRONICLE not ready — retrying in 60s (attempt %d). "
            "APScheduler will NOT start until CHRONICLE is healthy.",
            attempt,
        )
        await asyncio.sleep(60)

    # Layer 1: Weekly thesis — Sunday 6:00 PM ET
    _scheduler.add_job(
        _run_weekly_thesis,
        CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=_ET),
        id="weekly_thesis",
        replace_existing=True,
    )

    # Layer 2: Daily brief — Mon-Fri 7:00 AM ET
    _scheduler.add_job(
        _run_daily_brief,
        CronTrigger(day_of_week="mon-fri", hour=7, minute=0, timezone=_ET),
        id="daily_brief",
        replace_existing=True,
    )

    # Layer 3: Real-time monitor — every 5 minutes (market hours check inside job)
    _scheduler.add_job(
        _run_layer3_check,
        IntervalTrigger(minutes=5),
        id="layer3_monitor",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("THESIS service started on port %s", os.getenv("THESIS_PORT", "8060"))
    
    # Start Telegram bot polling in background
    _polling_task = asyncio.create_task(_telegram_bot.start_polling())
    logger.info("Telegram bot polling started")

    yield

    await _telegram_bot.stop_polling()
    logger.info("Telegram bot polling stopped")
    
    _scheduler.shutdown(wait=False)
    logger.info("THESIS service shutdown complete")


# ---------------------------------------------------------------------------
# Scheduled job wrappers
# ---------------------------------------------------------------------------

async def _run_weekly_thesis() -> None:
    """APScheduler job: generate weekly thesis."""
    if _weekly_gen is None:
        return
    try:
        await _weekly_gen.generate()
    except Exception as exc:
        logger.error("Scheduled weekly thesis failed: %s", exc, exc_info=True)


async def _run_daily_brief() -> None:
    """APScheduler job: generate daily brief."""
    if _daily_gen is None:
        return
    try:
        await _daily_gen.generate()
    except Exception as exc:
        logger.error("Scheduled daily brief failed: %s", exc, exc_info=True)


async def _run_layer3_check() -> None:
    """APScheduler job: Layer 3 real-time monitoring check (market hours only)."""
    if _monitor is None or _oracle is None:
        return
    if not _monitor._is_market_hours():
        return
    try:
        oracle_data = await _oracle.fetch_market_data()
        snapshot = MarketSnapshot(
            spy_price=oracle_data.spy_price,
            vix_level=oracle_data.vix_level,
            vix_change_1h=oracle_data.vix_change,
            timestamp=oracle_data.timestamp,
        )
        await _monitor.check(snapshot)
    except Exception as exc:
        logger.error("Layer 3 monitoring check failed: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="THESIS — Market Intelligence Agent",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/thesis/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Service health check.

    Returns:
        HealthResponse with liveness indicators for CHRONICLE and ORACLE.
        Fix 3: Includes table-level writability checks for SOVEREIGN preflight.
    """
    chronicle_ok = False
    oracle_ok = False
    has_weekly = False
    has_daily = False
    context_table_ok = False
    events_table_ok = False

    db_path = os.getenv("CHRONICLE_DB_PATH", "/Users/ahmedsadek/nexus/data/chronicle.db")

    if _chronicle is not None:
        try:
            weekly = _chronicle.get_current_thesis("weekly")
            daily = _chronicle.get_current_thesis("daily")
            chronicle_ok = True
            has_weekly = weekly is not None
            has_daily = daily is not None
        except Exception:
            pass

    # Fix 3: explicit table writability check (not just HTTP 200)
    try:
        context_table_ok = await asyncio.to_thread(
            _check_table_writable_sync, db_path, "thesis_context"
        )
        events_table_ok = await asyncio.to_thread(
            _check_table_writable_sync, db_path, "thesis_events"
        )
    except Exception as exc:
        logger.error("health: table writability check failed — %s", exc, exc_info=True)

    if _oracle is not None:
        oracle_ok = await _oracle.check_health()

    return HealthResponse(
        status="ok",
        chronicle_reachable=chronicle_ok,
        oracle_reachable=oracle_ok,
        has_weekly_thesis=has_weekly,
        has_daily_brief=has_daily,
        chronicle_tables_writable=context_table_ok and events_table_ok,
        chronicle_context_table_ok=context_table_ok,
        chronicle_events_table_ok=events_table_ok,
        scheduler_running=_scheduler is not None and _scheduler.running,
    )


@app.get("/thesis/current", response_model=ThesisContext)
async def get_current_thesis() -> ThesisContext:
    """Return the current weekly thesis.

    Returns:
        ThesisContext for the active weekly thesis.

    Raises:
        HTTPException 404 if no weekly thesis exists.
        HTTPException 503 if CHRONICLE is unavailable.
    """
    if _chronicle is None:
        raise HTTPException(status_code=503, detail="CHRONICLE not initialised")
    thesis = _chronicle.get_current_thesis("weekly")
    if thesis is None:
        raise HTTPException(status_code=404, detail="No weekly thesis available")
    return thesis


@app.get("/thesis/daily", response_model=ThesisContext)
async def get_daily_brief() -> ThesisContext:
    """Return today's daily brief.

    Returns:
        ThesisContext for today's daily brief.

    Raises:
        HTTPException 404 if no daily brief exists.
        HTTPException 503 if CHRONICLE is unavailable.
    """
    if _chronicle is None:
        raise HTTPException(status_code=503, detail="CHRONICLE not initialised")
    brief = _chronicle.get_current_thesis("daily")
    if brief is None:
        raise HTTPException(status_code=404, detail="No daily brief available")
    return brief


@app.get("/thesis/current-context", response_model=CurrentContextResponse)
async def get_current_context() -> CurrentContextResponse:
    """Fast OMNI integration endpoint — returns active thesis context.

    NEVER raises exceptions.  Returns safe neutral defaults if CHRONICLE
    is unavailable or no thesis exists.

    Fix 4: Sets is_fallback=True when returning defaults so OMNI can
    detect blind-flight conditions and alert SOVEREIGN.

    Returns:
        CurrentContextResponse suitable for direct OMNI consumption.
    """
    _default = CurrentContextResponse(is_fallback=True)

    if _chronicle is None:
        return _default

    try:
        thesis = _chronicle.get_current_thesis("daily") or \
                 _chronicle.get_current_thesis("weekly")
        if thesis is None:
            return _default

        age_hours = 0.0
        try:
            vf = datetime.fromisoformat(thesis.valid_from)
            now = datetime.now(tz=_ET)
            if vf.tzinfo is None:
                from zoneinfo import ZoneInfo
                vf = vf.replace(tzinfo=ZoneInfo("America/New_York"))
            age_hours = (now - vf).total_seconds() / 3600
        except Exception:
            pass

        return CurrentContextResponse(
            macro_gate=thesis.macro_gate,
            risk_reward_gate=thesis.risk_reward_gate,
            trading_posture=thesis.trading_posture,
            sizing_multiplier=thesis.sizing_multiplier,
            favored_sectors=thesis.favored_sectors,
            avoid_sectors=thesis.avoid_sectors,
            favored_strategies=thesis.favored_strategies,
            confidence_adjustment=thesis.confidence_adjustment,
            thesis_sentence=thesis.thesis_sentence,
            primary_authority=thesis.primary_authority,
            valid_until=thesis.valid_until,
            thesis_age_hours=round(age_hours, 2),
        )
    except Exception as exc:
        logger.error(
            "get_current_context() error: %s — returning safe default (is_fallback=True)", exc,
            exc_info=True,
        )
        _default.is_fallback = True
        return _default


@app.post("/thesis/generate/weekly", status_code=202)
async def generate_weekly() -> dict:
    """Manually trigger weekly thesis generation (idempotent).

    Spawns generation as a background task and returns 202 immediately
    so the caller never times out waiting for Anthropic/Perplexity calls.

    Returns:
        dict with status "accepted".
    """
    if _weekly_gen is None:
        raise HTTPException(status_code=503, detail="Weekly generator not initialised")

    async def _run() -> None:
        try:
            await _weekly_gen.generate()
        except Exception as exc:
            logger.error("generate_weekly() background task error: %s", exc, exc_info=True)

    asyncio.create_task(_run())
    return {"status": "accepted", "message": "Weekly thesis generation started in background"}


@app.post("/thesis/generate/daily", response_model=GenerateResponse)
async def generate_daily() -> GenerateResponse:
    """Manually trigger daily brief generation.

    Returns:
        GenerateResponse with brief summary.
    """
    if _daily_gen is None:
        raise HTTPException(status_code=503, detail="Daily generator not initialised")
    try:
        brief = await _daily_gen.generate()
        return GenerateResponse(
            status="ok",
            message="Daily brief generated",
            thesis_sentence=brief.thesis_sentence,
            macro_gate=brief.macro_gate,
            risk_reward_gate=brief.risk_reward_gate,
            trading_posture=brief.trading_posture,
            sizing_multiplier=brief.sizing_multiplier,
        )
    except Exception as exc:
        logger.error("generate_daily() error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/thesis/events", response_model=List[ThesisEvent])
async def get_events() -> List[ThesisEvent]:
    """Return thesis-breaking events from the last 24 hours.

    Returns:
        List of ThesisEvent objects, newest first.
    """
    if _chronicle is None:
        return []
    return _chronicle.get_recent_events(hours=24)


@app.post("/thesis/monitor")
async def monitor_snapshot(snapshot: MarketSnapshot) -> Dict[str, Any]:
    """Receive a market snapshot and check for thesis-breaking conditions.

    Args:
        snapshot: Current market data snapshot.

    Returns:
        Dict with "event" key: ThesisEvent dict if triggered, else null.
    """
    if _monitor is None:
        return {"event": None}
    try:
        event = await _monitor.check(snapshot)
        return {"event": event.model_dump() if event else None}
    except Exception as exc:
        logger.error("monitor_snapshot() error: %s", exc, exc_info=True)
        return {"event": None}


@app.get("/thesis/performance", response_model=PerformanceMetrics)
async def get_performance() -> PerformanceMetrics:
    """Return thesis accuracy metrics.

    Returns:
        PerformanceMetrics with available historical analysis.
    """
    if _chronicle is None:
        return PerformanceMetrics()
    try:
        history = _chronicle.get_all_thesis_history()
        return PerformanceMetrics(
            weeks_analyzed=len([t for t in history if t.layer == "weekly"]),
            note=(
                "Performance tracking requires historical comparison data. "
                f"Total thesis records: {len(history)}."
            ),
        )
    except Exception as exc:
        logger.error("get_performance() error: %s", exc, exc_info=True)
        return PerformanceMetrics()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.getenv("THESIS_HOST", "0.0.0.0"),
        port=int(os.getenv("THESIS_PORT", "8060")),
        reload=False,
    )
