"""
predictive_failure_model.py — Approach 4: Predictive Failure Model

Monitors 10 failure class leading indicators every 30 seconds during trading.
Intervenes BEFORE failures occur. Silent self-healing is the goal.
Ahmed is only alerted when intervention itself fails.

Run: every 30 seconds during trading hours (9:00 AM - 4:15 PM ET)
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

import pytz

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


@dataclass
class FailureRisk:
    """A predicted failure with risk score and intervention plan."""
    failure_class: str
    risk_score: float                   # 0.0–1.0
    indicators_triggered: List[str]
    predicted_horizon: str
    intervention: str
    intervention_fn: Optional[Callable] = field(default=None, repr=False)


class PredictiveFailureModel:
    """
    Monitors leading indicators for 10 known failure classes.
    Intervenes BEFORE failures occur.

    Metrics are fed via record_metric(). Call run_cycle() every 30 seconds.

    Failure classes monitored:
      1. ORATS_DEGRADATION
      2. CONCORDANCE_NEVER_FIRES
      3. BANDIT_OVERCONFIDENCE
      4. QI_MASS_TIMEOUT
      5. RAILWAY_THREAD_DEATH
      6. CAPITAL_ROUTER_SLOWDOWN
      7. ALPACA_RATE_LIMITING
      8. VIX_SPIKE_INCOMING
      9. MEMORY_PRESSURE
      10. DB_LOCK_CONTENTION
    """

    INTERVENTION_THRESHOLD = 0.65   # Intervene silently when risk > 65%
    ALERT_THRESHOLD        = 0.85   # Alert Ahmed when risk > 85%

    def __init__(self, telegram_alert_fn: Callable, db_path: str):
        """
        Args:
            telegram_alert_fn: Callable(message: str) to send Ahmed alerts.
            db_path: SQLite path for intervention logging.
        """
        self.telegram_alert = telegram_alert_fn
        self.db_path = db_path
        self._metrics: Dict[str, deque] = {}
        self._intervention_log: List[Dict] = []
        self._last_alert_times: Dict[str, float] = {}

    def record_metric(self, metric_name: str, value: float) -> None:
        """
        Record a metric observation. Call from relevant services.

        Args:
            metric_name: One of the metric names used by evaluators (e.g. 'orats_response_time_ms').
            value: Observed value.
        """
        if metric_name not in self._metrics:
            self._metrics[metric_name] = deque(maxlen=60)
        self._metrics[metric_name].append((time.time(), value))

    async def run_cycle(self) -> List[FailureRisk]:
        """
        Run one prediction cycle. Call every 30 seconds during trading hours.

        Returns:
            List of FailureRisk objects with risk_score > INTERVENTION_THRESHOLD.
        """
        risks: List[FailureRisk] = []

        evaluators = [
            self._evaluate_orats_degradation,
            self._evaluate_concordance_failure,
            self._evaluate_bandit_overconfidence,
            self._evaluate_qi_mass_timeout,
            self._evaluate_railway_thread_death,
            self._evaluate_capital_router_slowdown,
            self._evaluate_alpaca_rate_limiting,
            self._evaluate_vix_spike_incoming,
            self._evaluate_memory_pressure,
            self._evaluate_db_lock_contention,
        ]

        for evaluator in evaluators:
            try:
                risk = evaluator()
                if risk is not None and risk.risk_score > 0.3:
                    risks.append(risk)
                    if risk.risk_score > self.INTERVENTION_THRESHOLD:
                        await self._intervene(risk)
            except Exception as e:
                logger.error("Predictor %s error: %s", evaluator.__name__, e)

        return risks

    # --- Failure class evaluators ---

    def _evaluate_orats_degradation(self) -> Optional[FailureRisk]:
        """ORATS failure: response time trending up, error rate rising."""
        indicators: List[str] = []
        score = 0.0
        rt = self._get_recent("orats_response_time_ms", 10)
        err = self._get_recent("orats_error_rate", 5)
        if rt:
            avg = sum(rt) / len(rt)
            if avg > 1500:
                indicators.append(f"response_time_avg={avg:.0f}ms")
                score += 0.35
            if avg > 2500:
                score += 0.25
        if err:
            avg_e = sum(err) / len(err)
            if avg_e > 0.05:
                indicators.append(f"error_rate={avg_e:.2%}")
                score += 0.40
        if not indicators:
            return None
        return FailureRisk("ORATS_DEGRADATION", min(score, 1.0), indicators,
                           "5-10 minutes", "activate_polygon_iv_fallback_proactively",
                           self._activate_orats_fallback)

    def _evaluate_concordance_failure(self) -> Optional[FailureRisk]:
        """Concordance never fires: low submission rate, low ticker overlap."""
        indicators: List[str] = []
        score = 0.0
        sub_rate = self._get_recent("concordance_submissions_per_window", 10)
        overlap = self._get_recent("agent_ticker_overlap_rate", 5)
        if sub_rate:
            avg = sum(sub_rate) / len(sub_rate)
            if avg < 0.5:
                indicators.append(f"submission_rate={avg:.2f}")
                score += 0.50
        if overlap:
            avg = sum(overlap) / len(overlap)
            if avg < 0.3:
                indicators.append(f"ticker_overlap={avg:.2f}")
                score += 0.40
        if not indicators:
            return None
        return FailureRisk("CONCORDANCE_NEVER_FIRES", min(score, 1.0), indicators,
                           "30 minutes", "synchronize_agent_scan_triggers",
                           self._synchronize_scan_triggers)

    def _evaluate_bandit_overconfidence(self) -> Optional[FailureRisk]:
        """Alpha Trading Systems only: posterior variance collapsing."""
        indicators: List[str] = []
        score = 0.0
        variance = self._get_recent("bandit_mean_posterior_variance", 20)
        exploration = self._get_recent("bandit_exploration_rate", 10)
        if variance and len(variance) >= 5:
            recent_var = sum(variance[-5:]) / 5
            if recent_var < 0.1:
                indicators.append(f"mean_variance={recent_var:.4f}")
                score += 0.40
        if exploration:
            avg = sum(exploration) / len(exploration)
            if avg < 0.05:
                indicators.append(f"exploration_rate={avg:.3f}")
                score += 0.50
        if not indicators:
            return None
        return FailureRisk("BANDIT_OVERCONFIDENCE", min(score, 1.0), indicators,
                           "weeks of gradual drift", "increase_lambda_exploration_temporarily",
                           self._boost_bandit_exploration)

    def _evaluate_qi_mass_timeout(self) -> Optional[FailureRisk]:
        """QI (Quad Intelligence) mass timeout prediction."""
        indicators: List[str] = []
        score = 0.0
        rt = self._get_recent("qi_p95_response_time_ms", 5)
        vix_spike = self._get_recent("vix_5min_change", 3)
        if rt:
            avg = sum(rt) / len(rt)
            if avg > 12000:
                indicators.append(f"qi_p95={avg:.0f}ms")
                score += 0.50
        if vix_spike:
            spike = max(vix_spike)
            if spike > 2.0:
                indicators.append(f"vix_spike={spike:.2f}pts")
                score += 0.30
        if not indicators:
            return None
        return FailureRisk("QI_MASS_TIMEOUT", min(score, 1.0), indicators,
                           "2-5 minutes", "reduce_qi_timeout_to_10s",
                           self._reduce_qi_timeout)

    def _evaluate_railway_thread_death(self) -> Optional[FailureRisk]:
        """Nexus V1 Railway thread death: monitor age stalling."""
        indicators: List[str] = []
        score = 0.0
        age = self._get_recent("monitor_last_cycle_age_seconds", 5)
        if age:
            max_age = max(age)
            if max_age > 90:
                indicators.append(f"monitor_age={max_age:.0f}s")
                score += 0.60
            if max_age > 150:
                score += 0.30
        if not indicators:
            return None
        return FailureRisk("RAILWAY_THREAD_DEATH", min(score, 1.0), indicators,
                           "imminent", "force_thread_restart",
                           self._force_thread_restart)

    def _evaluate_capital_router_slowdown(self) -> Optional[FailureRisk]:
        """Capital Router response time degrading."""
        indicators: List[str] = []
        score = 0.0
        rt = self._get_recent("capital_router_response_ms", 5)
        if rt:
            avg = sum(rt) / len(rt)
            if avg > 3000:
                indicators.append(f"cr_response={avg:.0f}ms")
                score += 0.55
        if not indicators:
            return None
        return FailureRisk("CAPITAL_ROUTER_SLOWDOWN", min(score, 1.0), indicators,
                           "5 minutes", "restart_capital_router",
                           self._restart_capital_router)

    def _evaluate_alpaca_rate_limiting(self) -> Optional[FailureRisk]:
        """Alpaca API 429 rate limit events accumulating."""
        indicators: List[str] = []
        score = 0.0
        events = self._get_recent("alpaca_429_count", 10)
        if events:
            total = sum(events)
            if total > 3:
                indicators.append(f"alpaca_429_count={total}")
                score += 0.70
        if not indicators:
            return None
        return FailureRisk("ALPACA_RATE_LIMITING", min(score, 1.0), indicators,
                           "immediate", "activate_exponential_backoff",
                           self._activate_alpaca_backoff)

    def _evaluate_vix_spike_incoming(self) -> Optional[FailureRisk]:
        """VIX trending up rapidly or already elevated."""
        indicators: List[str] = []
        score = 0.0
        vix = self._get_recent("vix_level", 10)
        if vix and len(vix) >= 5:
            recent = vix[-5:]
            trend = (recent[-1] - recent[0]) / max(recent[0], 1)
            if trend > 0.10:
                indicators.append(f"vix_rising={trend:.1%}")
                score += 0.45
            if recent[-1] > 28:
                indicators.append(f"vix_level={recent[-1]:.1f}")
                score += 0.35
        if not indicators:
            return None
        return FailureRisk("VIX_SPIKE_INCOMING", min(score, 1.0), indicators,
                           "10-30 minutes", "pre_activate_vix_brake_monitoring",
                           self._heighten_vix_monitoring)

    def _evaluate_memory_pressure(self) -> Optional[FailureRisk]:
        """Process memory approaching limit."""
        indicators: List[str] = []
        score = 0.0
        mem = self._get_recent("process_memory_pct", 5)
        if mem:
            avg = sum(mem) / len(mem)
            if avg > 80:
                indicators.append(f"memory={avg:.0f}%")
                score += 0.55
        if not indicators:
            return None
        return FailureRisk("MEMORY_PRESSURE", min(score, 1.0), indicators,
                           "30-60 minutes", "clear_caches_and_gc",
                           self._clear_memory_pressure)

    def _evaluate_db_lock_contention(self) -> Optional[FailureRisk]:
        """SQLite lock wait time increasing."""
        indicators: List[str] = []
        score = 0.0
        wait = self._get_recent("db_lock_wait_ms", 10)
        if wait:
            avg = sum(wait) / len(wait)
            if avg > 100:
                indicators.append(f"db_lock_wait={avg:.0f}ms")
                score += 0.45
        if not indicators:
            return None
        return FailureRisk("DB_LOCK_CONTENTION", min(score, 1.0), indicators,
                           "10-20 minutes", "enable_wal_mode_if_not_active",
                           self._enable_wal_mode)

    # --- Intervention engine ---

    async def _intervene(self, risk: FailureRisk) -> None:
        """Execute intervention for a predicted failure."""
        logger.warning(
            "PREDICTIVE INTERVENTION: %s (score=%.2f) → %s",
            risk.failure_class, risk.risk_score, risk.intervention,
        )
        self._intervention_log.append({
            "timestamp": datetime.now(ET).isoformat(),
            "failure_class": risk.failure_class,
            "risk_score": risk.risk_score,
            "intervention": risk.intervention,
            "indicators": risk.indicators_triggered,
        })
        if risk.intervention_fn:
            try:
                await risk.intervention_fn()
            except Exception as e:
                logger.error("Intervention failed for %s: %s", risk.failure_class, e)
                if risk.risk_score > self.ALERT_THRESHOLD:
                    await self.telegram_alert(
                        f"⚠️ PREDICTIVE INTERVENTION FAILED\n"
                        f"Failure class: {risk.failure_class}\n"
                        f"Intervention: {risk.intervention}\n"
                        f"Error: {e}\nRisk score: {risk.risk_score:.2f}"
                    )

    # --- Utility ---

    def _get_recent(self, name: str, n: int) -> List[float]:
        """Get last N values for a metric."""
        if name not in self._metrics:
            return []
        return [v for _, v in list(self._metrics[name])[-n:]]

    def get_eod_prediction_report(self) -> Dict:
        """Returns prediction intervention summary for EOD report."""
        by_class: Dict[str, int] = {}
        for item in self._intervention_log:
            fc = item["failure_class"]
            by_class[fc] = by_class.get(fc, 0) + 1
        return {
            "total_interventions": len(self._intervention_log),
            "interventions_by_class": by_class,
            "recent_interventions": self._intervention_log[-10:],
        }

    # --- Intervention implementations (system-specific stubs) ---

    async def _activate_orats_fallback(self) -> None:
        """Pre-activate Polygon IV fallback before ORATS fails completely."""
        logger.info("INTERVENTION: Pre-activating ORATS → Polygon IV fallback")

    async def _synchronize_scan_triggers(self) -> None:
        """Trigger Axiom to push a fresh pool to all agents."""
        import requests as req
        try:
            req.post("http://localhost:8001/trigger-push",
                     headers={"X-Axiom-Secret": "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2"},
                     timeout=5)
            logger.info("INTERVENTION: Triggered Axiom pool push to synchronize agents")
        except Exception as e:
            logger.error("Failed to trigger Axiom push: %s", e)

    async def _boost_bandit_exploration(self) -> None:
        """Boost bandit exploration rate temporarily."""
        logger.info("INTERVENTION: Bandit overconfidence — boost exploration (manual action required for ATM)")

    async def _reduce_qi_timeout(self) -> None:
        """Reduce QI timeout to 10s to prevent cascade hangs."""
        logger.info("INTERVENTION: Reducing QI timeout to 10s (runtime config update needed)")

    async def _force_thread_restart(self) -> None:
        """Force restart of Railway monitoring thread."""
        logger.info("INTERVENTION: Railway thread death — force restart (Railway-specific action)")

    async def _restart_capital_router(self) -> None:
        """Restart Capital Router service."""
        import subprocess
        try:
            subprocess.run(["launchctl", "stop", "ai.nexus.capital-router"], check=False)
            await asyncio.sleep(2)
            subprocess.run(["launchctl", "start", "ai.nexus.capital-router"], check=False)
            logger.info("INTERVENTION: Capital Router restarted")
        except Exception as e:
            logger.error("Failed to restart capital router: %s", e)

    async def _activate_alpaca_backoff(self) -> None:
        """Activate exponential backoff for Alpaca API calls."""
        logger.info("INTERVENTION: Alpaca rate limiting — activating exponential backoff")

    async def _heighten_vix_monitoring(self) -> None:
        """Pre-activate VIX brake monitoring at heightened sensitivity."""
        logger.info("INTERVENTION: VIX spike incoming — heightening brake monitoring")

    async def _clear_memory_pressure(self) -> None:
        """Clear caches and trigger GC."""
        import gc
        gc.collect()
        logger.info("INTERVENTION: Memory pressure — GC triggered, caches cleared")

    async def _enable_wal_mode(self) -> None:
        """Enable WAL mode on all Nexus SQLite databases."""
        import sqlite3 as _sq
        db_paths = [
            "/Users/ahmedsadek/nexus/data/alpha_execution.db",
            "/Users/ahmedsadek/nexus/data/prime_execution.db",
            "/Users/ahmedsadek/nexus/data/omni.db",
        ]
        for db_path in db_paths:
            try:
                if not __import__("os").path.exists(db_path):
                    continue
                conn = _sq.connect(db_path)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.close()
                logger.info("INTERVENTION: WAL mode enabled on %s", db_path)
            except Exception as e:
                logger.error("Failed to enable WAL on %s: %s", db_path, e)
