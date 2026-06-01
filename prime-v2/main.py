"""
main.py — Prime V2 Adaptive Swing Trading Engine
=================================================
FastAPI service. Port 8012.

Schedule:
  07:00 AM ET — Build earnings calendar (daily)
  08:45 AM ET — Morning opportunity scan + weekly pipeline briefing
  09:35 AM ET — Execute pre-earnings entries (market just opened)
  10:05 AM ET — Execute post-earnings entries (gap settled)
  10:30 AM ET — Execute momentum entries
  Every 5 min — Exit monitor
  Every 30 min — Refresh momentum scan
  04:20 PM ET — Daily performance report
  Friday 04:45 PM ET — Weekly calibration report
"""
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.responses import JSONResponse

sys.path.insert(0, "/Users/ahmedsadek/nexus")
sys.path.insert(0, "/Users/ahmedsadek/nexus/prime-v2")

from scanner.earnings_calendar import build_calendar, get_upcoming_events
from scanner.momentum_scanner import scan_momentum
from scanner.opportunity_ranker import get_todays_queue, get_weekly_pipeline
from engine.position_sizer import calculate_position_size, get_open_position_count
from engine.trade_executor import execute_trade, init_positions_db, get_current_price
from engine.exit_engine import run_exit_scan
from learning.performance_tracker import send_daily_report, send_weekly_calibration

import requests as _requests

log = logging.getLogger("prime_v2.main")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)

_ET         = ZoneInfo("America/New_York")
_scheduler  = BackgroundScheduler(timezone=_ET)
TG_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN","")
TG_CHAT     = os.getenv("AHMED_CHAT_ID","")

# State
_last_queue:    list = []
_last_calendar: list = []
_trades_today:  int  = 0


def _notify(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        _requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg[:4000], "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

def job_build_calendar() -> None:
    """7:00 AM — Build earnings calendar for next 14 days."""
    global _last_calendar
    log.info("Building earnings calendar...")
    _last_calendar = build_calendar()
    tier_a = sum(1 for e in _last_calendar if e["tier"]=="TIER_A")
    tier_b = sum(1 for e in _last_calendar if e["tier"]=="TIER_B")
    log.info("Calendar: %d events — A:%d B:%d", len(_last_calendar), tier_a, tier_b)


def job_morning_scan() -> None:
    """8:45 AM — Morning opportunity scan and weekly pipeline briefing."""
    global _last_queue, _trades_today
    _trades_today = 0
    log.info("Morning opportunity scan...")

    _last_queue = get_todays_queue()
    pipeline    = get_weekly_pipeline()

    tier_a   = sum(1 for o in _last_queue if o["tier"]=="TIER_A")
    tier_b   = sum(1 for o in _last_queue if o["tier"]=="TIER_B")
    doubles  = sum(1 for o in _last_queue if o.get("double_signal"))

    # Morning briefing
    lines = [
        "<b>PRIME V2 MORNING BRIEFING</b>",
        f"Today's queue: {len(_last_queue)} opportunities",
        f"Tier A: {tier_a} | Tier B: {tier_b} | Double signals: {doubles}",
        "",
        "<b>Top 5 Today:</b>",
    ]
    for o in _last_queue[:5]:
        double = " ★" if o.get("double_signal") else ""
        lines.append(
            f"  {o['tier']} {o['ticker']:6} {o['strategy'][:12]:12} "
            f"conv={o['conviction']:.0f} size=${o['conviction']*100:.0f}{double}"
        )

    if pipeline:
        lines += ["", "<b>This Week (earnings):</b>"]
        for p in pipeline[:5]:
            double = " ★" if p.get("double_signal") else ""
            lines.append(
                f"  {p['tier']} {p['ticker']:6} "
                f"reports {p['report_date']} "
                f"avg_move={p.get('avg_move_pct') or 0:.1f}%{double}"
            )

    _notify("\n".join(lines))
    log.info("Morning scan: %d opportunities", len(_last_queue))


def job_execute_pre_earnings() -> None:
    """9:35 AM — Execute pre-earnings entries."""
    global _trades_today
    queue = [o for o in _last_queue if o.get("strategy")=="PRE_EARNINGS"]
    log.info("Pre-earnings execution: %d candidates", len(queue))
    _execute_queue(queue, "PRE_EARNINGS")


def job_execute_post_earnings() -> None:
    """10:05 AM — Execute post-earnings entries (gap settled)."""
    global _trades_today
    queue = [o for o in _last_queue if o.get("strategy")=="POST_EARNINGS"]
    log.info("Post-earnings execution: %d candidates", len(queue))
    _execute_queue(queue, "POST_EARNINGS")


def job_execute_momentum() -> None:
    """10:30 AM — Execute momentum entries."""
    global _trades_today
    queue = [o for o in _last_queue
             if o.get("strategy") in ("MOMENTUM_BREAKOUT","MOMENTUM_BREAKDOWN")]
    log.info("Momentum execution: %d candidates", len(queue))
    _execute_queue(queue, "MOMENTUM")


def job_refresh_momentum() -> None:
    """Every 30 min — Refresh momentum scan for new setups."""
    global _last_queue
    now = datetime.now(_ET)
    if now.hour < 10 or now.hour >= 15:
        return
    log.info("Refreshing momentum scan...")
    momentum = scan_momentum()
    # Update queue with fresh momentum data
    existing_tickers = {o["ticker"] for o in _last_queue}
    for setup in momentum:
        if setup["ticker"] not in existing_tickers:
            from scanner.opportunity_ranker import score_opportunity, get_size_multiplier
            setup["double_signal"] = False
            setup["avg_move_pct"]  = setup.get("change_5d",0)
            setup["conviction"]    = score_opportunity(setup)
            setup["size_mult"]     = get_size_multiplier(setup["conviction"])
            setup["strategy"]      = "MOMENTUM_BREAKOUT"
            _last_queue.append(setup)
            log.info("New momentum setup added: %s tier=%s", setup["ticker"], setup["tier"])


def _execute_queue(queue: list, label: str) -> None:
    """Execute trades from a queue segment."""
    global _trades_today
    executed = 0

    for opportunity in queue:
        if _trades_today >= 10:  # daily trade limit
            log.info("Daily trade limit reached")
            break

        ticker     = opportunity["ticker"]
        conviction = opportunity.get("conviction",70)
        tier       = opportunity.get("tier","TIER_B")

        # Skip low conviction
        if conviction < 45:
            log.info("Skip %s: conviction %.0f too low", ticker, conviction)
            continue

        # Size the position
        size_result = calculate_position_size(conviction, tier, agent_count=2)
        if not size_result["approved"]:
            log.info("Skip %s: %s", ticker, size_result.get("reason",""))
            continue

        # Execute
        position = execute_trade(opportunity, size_result)
        if position:
            executed      += 1
            _trades_today += 1
            log.info("Executed: %s $%.0f", ticker, size_result["position_size_usd"])

    log.info("%s execution complete: %d trades", label, executed)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    init_positions_db()

    # Build calendar immediately on startup
    job_build_calendar()

    # Schedule jobs
    _scheduler.add_job(job_build_calendar,        CronTrigger(hour=7,  minute=0,  timezone=_ET), id="calendar")
    _scheduler.add_job(job_morning_scan,           CronTrigger(hour=8,  minute=45, timezone=_ET), id="morning_scan")
    _scheduler.add_job(job_execute_pre_earnings,   CronTrigger(hour=9,  minute=35, timezone=_ET), id="exec_pre")
    _scheduler.add_job(job_execute_post_earnings,  CronTrigger(hour=10, minute=5,  timezone=_ET), id="exec_post")
    _scheduler.add_job(job_execute_momentum,       CronTrigger(hour=10, minute=30, timezone=_ET), id="exec_momentum")
    _scheduler.add_job(job_refresh_momentum,       IntervalTrigger(minutes=30),                    id="refresh_momentum", max_instances=1)
    _scheduler.add_job(run_exit_scan,              IntervalTrigger(minutes=5),                     id="exit_monitor",     max_instances=1)
    _scheduler.add_job(send_daily_report,          CronTrigger(hour=16, minute=20, timezone=_ET), id="daily_report")
    _scheduler.add_job(send_weekly_calibration,    CronTrigger(day_of_week="fri", hour=16, minute=45, timezone=_ET), id="weekly_cal")

    _scheduler.start()
    log.info("Prime V2 ready — Adaptive Swing Engine operational")
    _notify(
        "<b>PRIME V2 ONLINE</b>\n"
        "Adaptive Swing Engine started.\n"
        "Arms: Earnings + Momentum + Learning\n"
        "Universe: S&P500 + NASDAQ100\n"
        "Capital: $100,000 | Max position: $10,000"
    )
    yield
    _scheduler.shutdown()
    log.info("Prime V2 stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Prime V2 — Adaptive Swing Engine", lifespan=lifespan)


@app.get("/health")
def health() -> JSONResponse:
    open_pos  = get_open_position_count()
    cal_count = len(_last_calendar)
    queue_count = len(_last_queue)
    tier_a    = sum(1 for o in _last_queue if o.get("tier")=="TIER_A")
    return JSONResponse({
        "status":         "healthy",
        "service":        "prime-v2",
        "version":        "2.0.0",
        "trades_today":   _trades_today,
        "open_positions": open_pos,
        "calendar_events": cal_count,
        "queue_size":     queue_count,
        "tier_a_count":   tier_a,
        "timestamp":      datetime.now(_ET).isoformat(),
    })


@app.get("/queue")
def get_queue() -> JSONResponse:
    """Current opportunity queue."""
    return JSONResponse({
        "queue":     _last_queue[:20],
        "count":     len(_last_queue),
        "timestamp": datetime.now(_ET).isoformat(),
    })


@app.get("/calendar")
def get_calendar() -> JSONResponse:
    """Upcoming earnings events."""
    upcoming = get_upcoming_events(days=14)
    return JSONResponse({
        "events":    upcoming,
        "count":     len(upcoming),
        "timestamp": datetime.now(_ET).isoformat(),
    })


@app.get("/pipeline")
def get_pipeline() -> JSONResponse:
    """7-day forward opportunity pipeline."""
    pipeline = get_weekly_pipeline()
    return JSONResponse({
        "pipeline":  pipeline[:20],
        "count":     len(pipeline),
        "timestamp": datetime.now(_ET).isoformat(),
    })


@app.post("/scan")
def trigger_scan() -> JSONResponse:
    """Manually trigger opportunity scan."""
    global _last_queue
    _last_queue = get_todays_queue()
    return JSONResponse({
        "triggered": True,
        "queue_size": len(_last_queue),
        "timestamp": datetime.now(_ET).isoformat(),
    })


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT","8012"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
