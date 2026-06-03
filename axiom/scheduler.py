"""
scheduler.py — Axiom APScheduler Setup

All scheduled jobs for the Axiom service.
Uses APScheduler with BackgroundScheduler and ET timezone.
Scheduler state held in-memory — jobs defined at startup, not persisted.
"""

import logging
import sys
import os
from datetime import datetime, date

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from resilience.health import AxiomHealthReport

logger = logging.getLogger("axiom.scheduler")

ET = pytz.timezone("America/New_York")


def create_scheduler(app_state: dict) -> BackgroundScheduler:
    """
    Create and configure the APScheduler BackgroundScheduler with all Axiom jobs.

    Jobs defined:
      - 8:45 AM ET:   Pre-market brief generation
      - 9:00 AM ET:   Tier 1 morning filter run
      - 9:15 AM ET:   First Tier 2 run (opens pool)
      - Every 15 min, 9:15–3:30 PM ET: Tier 2 live pool refresh
      - 1:00 PM ET:   Tier 1 mid-day refresh
      - 2:15 PM ET:   Tier 1 afternoon refresh (maintains pool freshness)
      - 3:30 PM ET:   Submission window close (stops Tier 2)

    Args:
        app_state: Shared application state dict (holds pool, regime, settings, etc.).

    Returns:
        Configured BackgroundScheduler (not yet started).
    """
    scheduler = BackgroundScheduler(timezone=ET)

    # 8:45 AM — Pre-market brief
    scheduler.add_job(
        func    = lambda: _run_premarket_brief(app_state),
        trigger = CronTrigger(hour=8, minute=45, timezone=ET),
        id      = "premarket_brief",
        name    = "Pre-market brief",
        replace_existing=True,
    )

    # 9:00 AM — Tier 1 morning filter
    scheduler.add_job(
        func    = lambda: _run_tier1(app_state, run_type="morning"),
        trigger = CronTrigger(hour=9, minute=0, timezone=ET),
        id      = "tier1_morning",
        name    = "Tier 1 morning filter",
        replace_existing=True,
    )

    # 1:00 PM — Tier 1 mid-day refresh
    scheduler.add_job(
        func    = lambda: _run_tier1(app_state, run_type="midday"),
        trigger = CronTrigger(hour=13, minute=0, timezone=ET),
        id      = "tier1_midday",
        name    = "Tier 1 mid-day refresh",
        replace_existing=True,
    )

    # 2:15 PM — Tier 1 late afternoon refresh (maintains freshness until close)
    scheduler.add_job(
        func    = lambda: _run_tier1(app_state, run_type="afternoon"),
        trigger = CronTrigger(hour=14, minute=15, timezone=ET),
        id      = "tier1_afternoon",
        name    = "Tier 1 afternoon refresh",
        replace_existing=True,
    )

    # Every 15 minutes from 9:15 AM to 4:30 PM — live pool refresh + health monitor
    scheduler.add_job(
        func    = lambda: _run_tier2_if_open(app_state),
        trigger = CronTrigger(
            hour   = "9-15",
            minute = "15,30,45,0",
            timezone=ET,
        ),
        id      = "tier2_refresh",
        name    = "Tier 2 live pool refresh",
        replace_existing=True,
    )

    # Every 5 minutes — resilience health check (external data sources + local DB)
    # SPEC: AXIOM_30_SPEC v1.0 — check_all() runs in scheduler only, NOT in /health handler
    scheduler.add_job(
        func    = lambda: _run_health_check(app_state),
        trigger = IntervalTrigger(minutes=5),
        id      = "resilience_health_check",
        name    = "Resilience health check",
        replace_existing=True,
    )

    logger.info("Axiom scheduler created with %d jobs", len(scheduler.get_jobs()))
    return scheduler


def _run_health_check(app_state: dict) -> None:
    """
    Run Axiom resilience health check and store result in app_state.

    Called every 5 minutes by the scheduler.
    Results are served by /health endpoint's resilience_status field.

    Never raises — failure logged, _health_report left as previous value
    (stale is better than crashing the scheduler).
    """
    try:
        report = AxiomHealthReport()
        report.check_all(app_state)
        app_state["_health_report"] = report
        logger.info(
            "Resilience health check: overall=%s degraded=%s",
            report.overall, report.degraded_sources,
        )
    except Exception as e:
        logger.error("Resilience health check failed: %s", e)


def _is_market_hours(now: datetime = None) -> bool:
    """
    Return True if current ET time is within market hours (9:15 AM – 3:30 PM, Mon-Fri).

    Args:
        now: Datetime to check (defaults to current time).

    Returns:
        True if within market hours.
    """
    if now is None:
        now = datetime.now(ET)

    # Weekend check
    if now.weekday() >= 5:
        return False

    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


def _run_premarket_brief(app_state: dict) -> None:
    """Execute pre-market brief generation."""
    from premarket import generate_premarket_brief
    from data_sources import get_vix_with_fallback

    settings = app_state["settings"]
    deepseek_key = app_state.get("deepseek_api_key", "")

    try:
        vix, is_estimated = get_vix_with_fallback(
            settings.fred_api_key,
            app_state.get("last_vix"),
        )
        app_state["last_vix"] = vix
        if app_state.get("_vix_cache"):
            app_state["_vix_cache"].update(vix)

        from regime import classify_regime
        regime = classify_regime(vix, is_estimated)
        app_state["regime"] = regime
        if app_state.get("_regime_cache"):
            app_state["_regime_cache"].update(regime)

        pool = app_state.get("pool", [])
        generate_premarket_brief(
            vix                    = vix,
            regime_classification  = regime.classification,
            strategy_bias          = regime.strategy_bias,
            pool                   = pool,
            bot_token              = settings.telegram_bot_token,
            chat_id                = settings.ahmed_chat_id,
            deepseek_api_key       = deepseek_key,
        )
    except Exception as e:
        logger.error("Pre-market brief failed: %s", e)


def _run_tier1(app_state: dict, run_type: str) -> None:
    """Execute Tier 1 filter, update anchor stocks, and trigger ORACLE preliminary pre-warm."""
    from tier1_filter import run_tier1_filter
    from database import save_anchor_stocks
    import oracle_client

    settings = app_state["settings"]
    today    = date.today().isoformat()

    try:
        logger.info("Running Tier 1 filter (%s)", run_type)
        anchors = run_tier1_filter(
            universe          = settings.stock_universe,
            polygon_api_key   = settings.polygon_api_key,
            alpha_vantage_key = settings.alpha_vantage_key,
        )
        app_state["anchor_stocks"] = anchors
        save_anchor_stocks(settings.axiom_db_path, anchors, today, run_type)
        logger.info("Tier 1 (%s) complete — %d anchor stocks", run_type, len(anchors))

        # Fire-and-forget: pre-warm ORACLE Preliminary Cards for all anchor stocks
        if anchors:
            warmed = oracle_client.prefetch(anchors, tier="preliminary")
            app_state["oracle_preliminary_warmed"] = warmed
            if warmed:
                logger.info("ORACLE preliminary pre-warm triggered for %d tickers", len(anchors))
            else:
                logger.warning("ORACLE preliminary pre-warm failed — continuing without it")

    except Exception as e:
        logger.error("Tier 1 filter (%s) failed: %s", run_type, e)


def _run_tier2_if_open(app_state: dict, force: bool = False) -> None:
    """
    Execute Tier 2 filter, enrich with ORACLE intelligence, and push to agents.

    Args:
        app_state: Shared application state dict.
        force:     If True, bypass the market-hours gate (for manual /trigger-tier2 calls).
    """
    if not force and not _is_market_hours():
        logger.debug("Tier 2 skipped — outside market hours")
        return

    from tier2_filter import run_tier2_filter
    from data_sources import get_vix_with_fallback
    from regime import classify_regime, classify_regime_v2
    from database import save_pool_snapshot
    from agent_push import push_pool_to_agents
    import oracle_client

    settings = app_state["settings"]

    try:
        # ── Step 1: Regime classification (v4 composite, v3 fallback) ─────────
        macro_data = oracle_client.get_macro_data()
        if macro_data:
            regime = classify_regime_v2(macro_data)
            logger.info(
                "Regime v2 — composite=%d, class=%s, vix=%.1f, HY=%.0fbps, curve=%.0fbps",
                regime.composite_score, regime.classification,
                regime.vix,
                regime.hy_spread_bps or 0,
                regime.yield_curve_bps or 0,
            )
            # Update VIX cache so /assess never sees stale data during ORACLE-available runs.
            # Previously this path skipped the cache write, causing 12-min TTL expiry.
            if regime.vix is not None:
                app_state["last_vix"] = regime.vix
                if app_state.get("_vix_cache"):
                    app_state["_vix_cache"].update(regime.vix)
        else:
            # ORACLE unavailable — fall back to VIX-only
            vix, is_estimated = get_vix_with_fallback(
                settings.fred_api_key,
                app_state.get("last_vix"),
            )
            app_state["last_vix"] = vix
            if app_state.get("_vix_cache"):
                app_state["_vix_cache"].update(vix)
            regime = classify_regime(vix, is_estimated)
            logger.warning(
                "ORACLE unavailable — regime fallback: VIX=%.1f, class=%s",
                vix, regime.classification,
            )

        app_state["regime"] = regime
        if app_state.get("_regime_cache"):
            app_state["_regime_cache"].update(regime)

        # ── Step 2: Tier 2 filter ─────────────────────────────────────────────
        anchor_stocks = app_state.get("anchor_stocks", [])
        if not anchor_stocks:
            logger.warning("No anchor stocks — Tier 1 may not have run yet")
            return

        new_pool = run_tier2_filter(
            anchor_stocks     = anchor_stocks,
            regime            = regime,
            polygon_api_key   = settings.polygon_api_key,
            alpha_vantage_key = settings.alpha_vantage_key,
            fred_api_key      = settings.fred_api_key,
        )

        # Pool protection — keep previous if new pool too small
        if len(new_pool) < 10:
            prev_pool = app_state.get("pool", [])
            if prev_pool:
                from telegram import send_pool_alert
                send_pool_alert(
                    bot_token = settings.telegram_bot_token,
                    chat_id   = settings.ahmed_chat_id,
                    pool_size = len(new_pool),
                    reason    = f"Only {len(new_pool)} stocks passed Tier 2 criteria",
                )
                logger.warning(
                    "Pool too small (%d) — keeping previous pool (%d)",
                    len(new_pool), len(prev_pool),
                )
                new_pool = prev_pool
            else:
                logger.error(
                    "Pool too small (%d) and no previous pool — using empty pool",
                    len(new_pool),
                )

        app_state["pool"] = new_pool

        # Generate window ID
        from main import current_window_id
        window_id = current_window_id()
        app_state["window_id"] = window_id

        # ── Step 3: ORACLE full pre-warm (fire-and-forget) ────────────────────
        oracle_warmed = False
        if new_pool:
            oracle_warmed = oracle_client.prefetch(new_pool, tier="full")
            if oracle_warmed:
                logger.info("ORACLE full pre-warm triggered for %d pool tickers", len(new_pool))
            else:
                logger.warning("ORACLE full pre-warm failed — agents will query on-demand")

        # ── Step 4: Signal coherence scores (10s total budget) ───────────────
        coherence_summary: dict = {}
        coherence_available = False
        if new_pool:
            coherence_summary = oracle_client.get_coherence_scores(new_pool, timeout=8.0)
            coherence_available = len(coherence_summary) > 0
            if coherence_available:
                logger.info(
                    "Coherence scores fetched — %d/%d tickers",
                    len(coherence_summary), len(new_pool),
                )
            else:
                logger.warning("No coherence scores returned — ORACLE may still be warming")

        # ── Step 5: Cross-ticker pattern intelligence ─────────────────────────
        echo_chamber_risk: list = []
        cycle_patterns: list = []
        pattern_intelligence_available = False

        cycle_intel = oracle_client.get_cycle_intelligence()
        if cycle_intel:
            echo_chamber_risk = cycle_intel.get("echo_chamber_risk_tickers", [])
            raw_patterns = cycle_intel.get("patterns_detected", [])
            cycle_patterns = [
                f"{p.get('type', 'UNKNOWN')}: {p.get('description', '')}"
                for p in raw_patterns
                if isinstance(p, dict)
            ]
            pattern_intelligence_available = True
            if echo_chamber_risk:
                logger.info(
                    "Echo chamber risk flagged for: %s",
                    ", ".join(echo_chamber_risk),
                )

        # ── Step 6: Save snapshot ─────────────────────────────────────────────
        save_pool_snapshot(settings.axiom_db_path, window_id, new_pool, regime.to_dict())

        # ── Step 7: Build enriched pool payload ───────────────────────────────
        pool_payload = {
            "pool":                         new_pool,
            "count":                        len(new_pool),
            "window_id":                    window_id,
            "updated_at":                   datetime.now(ET).isoformat(),
            "market_open":                  True,
            "regime":                       regime.to_dict(),
            # ORACLE intelligence layer (v4)
            "coherence_summary":            coherence_summary,
            "coherence_available":          coherence_available,
            "echo_chamber_risk":            echo_chamber_risk,
            "cycle_patterns":               cycle_patterns,
            "pattern_intelligence_available": pattern_intelligence_available,
            "oracle_warmed":                oracle_warmed,
        }

        # ── Step 8: Push to agents ────────────────────────────────────────────
        push_pool_to_agents(
            pool_payload   = pool_payload,
            agent_webhooks = settings.agent_webhooks,
            db_path        = settings.axiom_db_path,
            bot_token      = settings.telegram_bot_token,
            chat_id        = settings.ahmed_chat_id,
            window_id      = window_id,
            nexus_secret   = settings.nexus_secret,
        )
        logger.info(
            "Tier 2 complete — pool=%d | window=%s | regime=%s (score=%d) | "
            "coherence=%d tickers | patterns=%d | echo_risk=%d",
            len(new_pool), window_id, regime.classification, regime.composite_score,
            len(coherence_summary), len(cycle_patterns), len(echo_chamber_risk),
        )

    except Exception as e:
        logger.error("Tier 2 refresh failed: %s", e, exc_info=True)
        app_state["tier2_consecutive_failures"] = (
            app_state.get("tier2_consecutive_failures", 0) + 1
        )
        if app_state["tier2_consecutive_failures"] >= 2:
            from telegram import send_message
            send_message(
                settings.telegram_bot_token,
                settings.ahmed_chat_id,
                f"⚠️ <b>AXIOM TIER 2 FAILURE</b>\n"
                f"Consecutive failures: {app_state['tier2_consecutive_failures']}\n"
                f"Error: {str(e)[:200]}\n"
                f"Using last known pool.",
            )



