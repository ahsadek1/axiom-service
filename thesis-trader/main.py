"""
main.py — THESIS Trader FastAPI Service (Port 8070)
Standalone autonomous trading system using Five Legends.
"""
from __future__ import annotations
import asyncio, logging, os, time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from market_brief_generator import MarketBriefGenerator
from thesis_reporter import send_mid_session_report, send_eod_report
from execution.options_chain import select_bull_put_strikes, get_underlying_price
from execution.spread_executor import execute_bull_put_spread, init_positions_db, get_open_positions
from execution.exit_monitor import run_exit_scan

# Guard module imports (structural safety fixes)
try:
    from execution.cascade_breaker import can_execute_component, record_component_success, record_component_failure
    from execution.escalation import escalate_critical
    HAS_GUARDS = True
except ImportError:
    HAS_GUARDS = False
    escalate_critical = lambda *args, **kwargs: None
    can_execute_component = lambda *args: True  # Fail-safe: allow execution if guards unavailable
    record_component_success = lambda *args: None
    record_component_failure = lambda *args, **kwargs: None
from execution.position_manager import run_all_checks, get_portfolio_summary
from execution.market_data import get_trade_data
from execution.legend_execution import compute_composite_params
from execution.learning_loop import run_eod_learning
from execution.performance_tracker import send_performance_report
from thesis_conversational import ThesisConversation
from legend_screener import LegendScreener
from thesis_trader import health_check, get_top_tickers_by_winrate, kelly_position_size, _notify, _alpaca, get_open_position_count, MAX_POSITIONS, MIN_BACKTEST_WINRATE, BASE_POSITION_USD

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
log = logging.getLogger("thesis.main")
_ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_scheduler: AsyncIOScheduler = None
_last_screening: dict = {}
_last_shortlist: list = []
_screenings_today: int = 0
_conversation: ThesisConversation = None

# ---------------------------------------------------------------------------
# Core screening cycle
# ---------------------------------------------------------------------------

async def run_screening_cycle() -> None:
    global _last_screening, _last_shortlist, _screenings_today
    now_et = datetime.now(_ET)
    log.info("=== THESIS Screening Cycle Starting === %s", now_et.strftime("%H:%M ET"))

    # Use top 100 tickers by backtest win rate in current regime
    # Full 534-ticker universe causes LLM context overflow and 29-min timeouts
    import sqlite3 as _sq
    try:
        _conn = _sq.connect("/Users/ahmedsadek/nexus/data/backtest.db", timeout=5)
        _rows = _conn.execute("""SELECT DISTINCT ticker FROM historical_win_rates
            WHERE strategy='bull_put_spread' AND direction='bullish'
            AND sample_count>=10 AND win_rate>=0.80
            ORDER BY win_rate DESC LIMIT 100""").fetchall()
        _conn.close()
        universe = [r[0] for r in _rows]
        if not universe:
            raise ValueError("Empty backtest universe")
        log.info("Universe: %d tickers from backtest DB (top by win rate)", len(universe))
    except Exception as _ue:
        log.error("Cannot load universe: %s — skipping cycle", _ue)
        return

    # 1. Generate market brief
    gen = MarketBriefGenerator()
    brief = await gen.generate()
    regime = brief["vix"]["regime"]
    vix = brief["vix"]["level"]
    log.info("Brief: VIX=%.1f regime=%s", vix, regime)

    # 2. Run Five Legends screening
    screener = LegendScreener()
    result = await screener.screen(brief, universe)
    _last_shortlist = result.shortlist
    log.info("Shortlist: %d/%d tickers (3+ endorsements)", result.shortlist_size, result.universe_size)

    # 3. Filter by backtest win rate
    qualified = []
    for ticker in result.shortlist[:30]:  # top 30 by composite score
        from thesis_trader import get_win_rate
        wr = get_win_rate(ticker, regime)
        if wr and wr >= MIN_BACKTEST_WINRATE:
            qualified.append({"ticker": ticker, "win_rate": wr,
                             "composite_score": result.composite_scores.get(ticker, 0),
                             "endorsements": result.endorsement_counts.get(ticker, 0)})

    log.info("Qualified (backtest >= %.0f%%): %d tickers", MIN_BACKTEST_WINRATE*100, len(qualified))

    # 4. Execute top picks — full 7-layer pipeline
    from zoneinfo import ZoneInfo as _ZI
    _et = datetime.now(_ZI("America/New_York"))
    _market_open = (_et.weekday() < 5 and
                   (_et.hour > 9 or (_et.hour == 9 and _et.minute >= 30)) and
                   _et.hour < 16)

    executed = 0
    if _market_open and qualified:
        portfolio = get_portfolio_summary()
        capacity  = MAX_POSITIONS - portfolio["open_positions"]
        log.info("Executing: capacity=%d regime=%s", capacity, regime)

        for pick in qualified[:capacity]:
            ticker   = pick["ticker"]
            win_rate = pick["win_rate"]
            endorsements = pick.get("endorsements", 3)
            comp_score   = pick.get("composite_score", 70)

            try:
                # Layer 4: Live market data check
                trade_data = get_trade_data(ticker)
                if not trade_data["tradeable"]:
                    log.info("%s: not tradeable (%s)", ticker,
                            "earnings" if trade_data["has_earnings_soon"] else "IV/price")
                    continue

                price = trade_data["price"]
                if not price:
                    continue

                # Layer 3: Risk checks
                approved, failures = run_all_checks(ticker, 5.0, 1)
                if not approved:
                    log.info("%s: risk check failed: %s", ticker, failures[0])
                    continue

                # Layer 1: Options chain + strike selection
                spread = select_bull_put_strikes(
                    ticker=ticker,
                    underlying_price=price,
                    target_dte=37,
                    short_delta_target=0.25,
                    spread_width=5.0,
                )
                if not spread:
                    log.info("%s: no suitable spread found", ticker)
                    continue

                # Layer 5: Legend-specific execution params
                legend_names = result.legend_results.keys() if hasattr(result,"legend_results") else ["Druckenmiller","Jones","Soros","Cohen","Buffett"]
                comp_params = compute_composite_params(
                    ticker=ticker,
                    endorsing_legends=list(legend_names),
                    composite_score=comp_score,
                    win_rate=win_rate,
                    regime=regime,
                    risk_reward=spread["risk_reward"],
                )

                if comp_params.size_multiplier == 0.0:
                    log.info("%s: Jones VETO — %s", ticker, comp_params.rationale)
                    continue

                # Kelly sizing with legend multiplier
                base_size = kelly_position_size(win_rate, BASE_POSITION_USD)
                final_size = base_size * comp_params.size_multiplier
                contracts  = max(1, int(final_size / (spread["spread_width"] * 100)))

                # High-impact week: reduce size
                if trade_data["high_impact_week"]:
                    contracts = max(1, contracts // 2)
                    log.info("%s: High-impact week — halving contracts to %d", ticker, contracts)

                # Layer 1: Execute (with circuit breaker)
                if not can_execute_component("order_placement"):
                    log.warning("%s: Circuit breaker OPEN for order_placement — skipping", ticker)
                    continue
                
                try:
                    position = execute_bull_put_spread(
                        spread=spread,
                        contracts=contracts,
                        endorsements=endorsements,
                        win_rate=win_rate,
                        legends=list(legend_names),
                    )

                    if position:
                        executed += 1
                        record_component_success("order_placement")
                        log.info("EXECUTED: %s %s/%s exp=%s premium=$%.2f contracts=%d",
                                 ticker, spread["short_strike"], spread["long_strike"],
                                 spread["expiration"], spread["net_premium"], contracts)
                    else:
                        record_component_failure("order_placement", reason="execute_bull_put_spread returned None")
                except Exception as exec_exc:
                    log.error("Execution error for %s: %s", ticker, exec_exc)
                    record_component_failure("order_placement", reason=str(exec_exc))
                    if HAS_GUARDS:
                        escalate_critical("Execution Failure", f"{ticker}: {str(exec_exc)[:100]}", ticker)
                    raise

            except Exception as exc:
                log.error("Screening error for %s: %s", ticker, exc)
    else:
        if not _market_open:
            log.info("Market closed — screening only, no execution")

    # Update state
    _last_screening = {
        "cycle_id": result.cycle_id,
        "regime": regime,
        "vix": vix,
        "universe_size": result.universe_size,
        "shortlist_size": result.shortlist_size,
        "qualified": len(qualified),
        "executed": executed,
        "timestamp": datetime.now().isoformat(),
    }
    _screenings_today += 1

    # Telegram FYI
    _notify(
        f"🔍 <b>THESIS Screening Complete</b>\n"
        f"Cycle: {result.cycle_id}\n"
        f"VIX: {vix:.1f} ({regime})\n"
        f"Shortlist: {result.shortlist_size}/{result.universe_size}\n"
        f"Qualified: {len(qualified)} tickers\n"
        f"Top picks: {', '.join([p['ticker'] for p in qualified[:5]])}"
    )
    log.info("=== Cycle Complete ===")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _scheduler
    log.info("THESIS Trader starting on port 8070...")

    h = health_check()
    if not h["alpaca_connected"]:
        log.error("Alpaca not connected — check .env credentials")
    else:
        log.info("Alpaca connected | buying_power=%s | paper=%s", h["buying_power"], h["paper_mode"])

    _notify(
        f"🚀 <b>THESIS TRADER STARTING</b>\n"
        f"Port: 8070 | Paper: {os.getenv('ALPACA_PAPER','true')}\n"
        f"Five Legends screening active\n"
        f"Backtest DB: 10,450 rows"
    )

    _scheduler = AsyncIOScheduler(timezone=_ET)

    # Pre-market scan: 8:45 AM ET
    _scheduler.add_job(run_screening_cycle, CronTrigger(hour=8, minute=45, timezone=_ET), id="premarket")

    # Market hours: every 90 minutes from 9:30 to 15:00
    for hour, minute in [(9,30),(11,0),(12,30),(14,0),(15,0)]:
        _scheduler.add_job(run_screening_cycle, CronTrigger(hour=hour, minute=minute, timezone=_ET),
                          id=f"screen_{hour:02d}{minute:02d}")

    # Mid-session reports: 10:00, 12:00, 14:00 ET
    for hour in [10, 12, 14]:
        _scheduler.add_job(
            lambda h=hour: send_mid_session_report({
                "screenings_today": _screenings_today,
                "last_screening": _last_screening,
                "shortlist_size": len(_last_shortlist),
                "regime": _last_screening.get("regime","NORMAL"),
                "vix": _last_screening.get("vix",0),
            }),
            CronTrigger(hour=hour, minute=0, timezone=_ET),
            id=f"report_{hour}",
        )

    # Exit monitor: every 5 minutes during market hours
    _scheduler.add_job(
        run_exit_scan,
        "interval", minutes=5, id="exit_monitor",
        max_instances=1,
    )

    # EOD learning: 16:30 ET
    _scheduler.add_job(
        run_eod_learning,
        CronTrigger(hour=16, minute=30, timezone=_ET),
        id="eod_learning",
    )

    # Weekly performance report: Friday 16:45 ET
    _scheduler.add_job(
        lambda: send_performance_report(days=30),
        CronTrigger(day_of_week="fri", hour=16, minute=45, timezone=_ET),
        id="weekly_performance",
    )

    # End-of-day report: 16:15 ET
    _scheduler.add_job(
        lambda: send_eod_report({
            "screenings_today": _screenings_today,
            "last_screening": _last_screening,
        }),
        CronTrigger(hour=16, minute=15, timezone=_ET),
        id="eod_report",
    )

    # Conversational polling — every 5 seconds
    global _conversation
    _conversation = ThesisConversation(lambda: {
        "screenings_today": _screenings_today,
        "last_screening": _last_screening,
        "shortlist_size": len(_last_shortlist),
        "regime": _last_screening.get("regime","NORMAL"),
        "vix": _last_screening.get("vix",0),
    })
    _scheduler.add_job(
        _conversation.poll_and_respond,
        "interval", seconds=5, id="conversation_poll",
        max_instances=1,
    )

    _scheduler.start()
    log.info("THESIS Trader ready — 6 screenings + reports + conversation active")

    yield

    _scheduler.shutdown()
    log.info("THESIS Trader stopped")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="THESIS Trader", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    h = health_check()
    h["screenings_today"] = _screenings_today
    h["last_screening"] = _last_screening
    h["shortlist_size"] = len(_last_shortlist)
    return JSONResponse(h)


@app.get("/shortlist")
def shortlist():
    return JSONResponse({"shortlist": _last_shortlist, "count": len(_last_shortlist)})


@app.get("/screening/last")
def last_screening():
    return JSONResponse(_last_screening)


@app.post("/screening/run")
async def manual_screening():
    asyncio.create_task(run_screening_cycle())
    return JSONResponse({"status": "queued", "message": "Screening cycle started"})
