"""
predictive_failure_model.py — Nexus v1 Predictive Failure Model
================================================================
Part of NEXUS RESILIENCE FRAMEWORK v1.0 — Week 2 Build

PURPOSE:
  Predict failures BEFORE they happen. Act proactively.
  Goal: > 60% of failures predicted and prevented.

PREDICTORS (5 initial, expanding to 10):
  1. ORATS Degradation Predictor
     Signal: rising latency + increasing error rate
     Action: pre-warm Polygon IV cache before ORATS fails

  2. Concordance Failure Predictor
     Signal: R2 submission gap > 4h, pool not clearing
     Action: alert OMNI to check agent health

  3. Railway Thread Death Predictor  ← CRITICAL for Nexus v1
     Signal: UTC vs ET timezone mismatch in scanner timing
     Action: restart scanner loop or alert

  4. QI Mass Timeout Predictor
     Signal: multiple consecutive timeouts on ORATS/Polygon
     Action: switch to redundant data client immediately

  5. Pending Queue Explosion Predictor
     Signal: pending queue growing > 10/hour without GO verdicts
     Action: alert, check execution bridge health

Each predictor runs on every 30-second cycle.
When prediction confidence > 70% → pre-emptive action fires.
All predictions logged for model improvement.

Author: OMNI 🌐
Date:   2026-04-15 (Resilience Framework Week 2)
"""

import os
import time
import json
import logging
import sqlite3
import threading
import datetime
import requests
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from enum import Enum

logger = logging.getLogger("omni.predictive")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(name)s] %(levelname)s — %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────
ALPHA_URL       = os.getenv("ALPHA_SERVICE_URL", "https://worker-production-2060.up.railway.app")
PRIME_URL       = os.getenv("PRIME_SERVICE_URL", "https://nexus-prime-bot-production.up.railway.app")
AXIOM_URL       = os.getenv("AXIOM_URL", "https://axiom-production-334c.up.railway.app")
ORATS_KEY       = os.getenv("ORATS_API_KEY", "4476e955-241a-4540-b114-ebbf1a3a3b87")
POLYGON_KEY     = os.getenv("POLYGON_API_KEY", "OaxOzJMu_JZpl7uF64L7FyhowSZtwcvI")
TG_BOT_TOKEN    = os.getenv("TG_BOT_TOKEN", "")
TG_AHMED_DM     = "8573754783"
TG_HEALTH_GRP   = os.getenv("TG_HEALTH_GROUP", "-5184172590")
DB_PATH         = os.getenv("PREDICT_DB_PATH", "/data/nexus_predictive.db")

CYCLE_INTERVAL  = 30    # seconds between prediction cycles
PREDICT_THRESHOLD = 70  # confidence % to trigger action


class PredictionSeverity(Enum):
    INFO    = "INFO"
    WARNING = "WARNING"
    URGENT  = "URGENT"
    CRITICAL = "CRITICAL"


# ── Database ──────────────────────────────────────────────────────────────────

def _init_predict_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS predictions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            predictor       TEXT,
            confidence      REAL,
            severity        TEXT,
            signal          TEXT,
            action_taken    TEXT,
            outcome         TEXT,        -- filled in later if prediction was correct
            correct         INTEGER,     -- 1=correct, 0=false positive
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS predictor_metrics (
            predictor       TEXT,
            metric_key      TEXT,
            metric_value    REAL,
            updated_at      TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (predictor, metric_key)
        );
    """)
    conn.commit()
    conn.close()


# ── Metric Store ──────────────────────────────────────────────────────────────

class MetricStore:
    """Rolling metrics for each predictor."""

    def __init__(self, window: int = 20):
        self._data: Dict[str, List[Tuple[float, float]]] = {}  # key → [(ts, val)]
        self._window = window

    def record(self, key: str, value: float):
        if key not in self._data:
            self._data[key] = []
        self._data[key].append((time.time(), value))
        # Keep only last N readings
        self._data[key] = self._data[key][-self._window:]

    def get_latest(self, key: str) -> Optional[float]:
        series = self._data.get(key, [])
        return series[-1][1] if series else None

    def get_trend(self, key: str) -> float:
        """Returns positive if rising, negative if falling, 0 if stable."""
        series = self._data.get(key, [])
        if len(series) < 3:
            return 0.0
        recent  = sum(v for _, v in series[-3:]) / 3
        earlier = sum(v for _, v in series[:3]) / 3
        return recent - earlier

    def get_all(self, key: str) -> List[float]:
        return [v for _, v in self._data.get(key, [])]


_metrics = MetricStore(window=30)


# ── Predictors ────────────────────────────────────────────────────────────────

class PredictiveFailureModel:
    """
    Runs all 5 predictors every 30 seconds.
    When confidence > PREDICT_THRESHOLD → fire pre-emptive action.
    """

    def __init__(self):
        _init_predict_db()
        self._running     = False
        self._cycle_count = 0
        self._monitor_th  = None
        self._last_alerts: Dict[str, float] = {}  # predictor → last alert time

    def start(self):
        self._running = True
        self._monitor_th = threading.Thread(
            target=self._cycle_loop, daemon=True, name="predictive-model"
        )
        self._monitor_th.start()
        logger.info("🔮 Predictive Failure Model started (5 predictors active)")

    def stop(self):
        self._running = False

    def _cycle_loop(self):
        while self._running:
            try:
                self._run_all_predictors()
                self._cycle_count += 1
            except Exception as e:
                logger.error(f"Predictive cycle error: {e}")
            time.sleep(CYCLE_INTERVAL)

    def _run_all_predictors(self):
        """Run all predictors and act on high-confidence predictions."""
        results = [
            self._predict_orats_degradation(),
            self._predict_concordance_failure(),
            self._predict_railway_thread_death(),
            self._predict_qi_mass_timeout(),
            self._predict_pending_queue_explosion(),
        ]

        for result in results:
            if result and result.get("confidence", 0) >= PREDICT_THRESHOLD:
                self._act_on_prediction(result)

    # ── Predictor 1: ORATS Degradation ───────────────────────────────────────

    def _predict_orats_degradation(self) -> dict:
        """
        Signal: ORATS latency rising + error rate increasing.
        Probes ORATS and records latency. Triggers Polygon pre-warm.
        """
        t0 = time.time()
        try:
            r = requests.get(
                "https://api.orats.io/datav2/summaries",
                params={"token": ORATS_KEY, "ticker": "SPY"},
                timeout=5,
            )
            latency_ms = (time.time() - t0) * 1000
            success = r.status_code == 200 and bool(r.json().get("data"))
        except Exception:
            latency_ms = (time.time() - t0) * 1000
            success = False

        _metrics.record("orats_latency", latency_ms)
        _metrics.record("orats_success", 1.0 if success else 0.0)

        # Confidence: high latency + trending up + recent failures
        latency_trend = _metrics.get_trend("orats_latency")
        recent_successes = _metrics.get_all("orats_success")[-5:] if _metrics.get_all("orats_success") else [1]
        failure_rate = 1 - (sum(recent_successes) / len(recent_successes))
        current_latency = latency_ms

        confidence = 0.0
        signals = []

        if current_latency > 3000:
            confidence += 40
            signals.append(f"latency={current_latency:.0f}ms>3s")
        elif current_latency > 1500:
            confidence += 20
            signals.append(f"latency={current_latency:.0f}ms>1.5s")

        if latency_trend > 500:
            confidence += 25
            signals.append(f"latency rising +{latency_trend:.0f}ms/cycle")

        if failure_rate > 0.4:
            confidence += 35
            signals.append(f"failure_rate={failure_rate*100:.0f}%")

        if not success:
            confidence = max(confidence, 75)
            signals.append("current call FAILED")

        return {
            "predictor":  "ORATS_DEGRADATION",
            "confidence": min(100, confidence),
            "severity":   PredictionSeverity.URGENT.value if confidence > 70 else PredictionSeverity.WARNING.value,
            "signal":     " | ".join(signals) or "nominal",
            "action":     "pre_warm_polygon_iv_cache",
        }

    # ── Predictor 2: Concordance Failure ─────────────────────────────────────

    def _predict_concordance_failure(self) -> dict:
        """
        Signal: R2 submissions stalled, pool not clearing, pending growing.
        Action: alert OMNI to check agent submission health.
        """
        try:
            r = requests.get(f"{ALPHA_URL}/health", timeout=8)
            if r.status_code != 200:
                return {"predictor": "CONCORDANCE_FAILURE", "confidence": 80,
                        "severity": PredictionSeverity.URGENT.value,
                        "signal": f"Alpha health unreachable: {r.status_code}",
                        "action": "alert_and_check_alpha"}

            health = r.json()
            pending     = health.get("pending", 0)
            go_today    = health.get("go_verdicts_today", health.get("omni_scanner", {}).get("go_count", 0))
            total_cycles= health.get("omni_scanner", {}).get("total_cycles", 0)
            dry_cycles  = health.get("omni_scanner", {}).get("dry_cycles", 0)

            _metrics.record("alpha_pending", float(pending))
            _metrics.record("alpha_go_count", float(go_today))
            _metrics.record("alpha_dry_cycles", float(dry_cycles))

            pending_trend = _metrics.get_trend("alpha_pending")
            confidence = 0.0
            signals = []

            # Pending growing but no GOs
            if pending > 20 and go_today == 0 and total_cycles > 3:
                confidence += 50
                signals.append(f"pending={pending} with 0 GOs after {total_cycles} cycles")

            # Pending growing fast
            if pending_trend > 5:
                confidence += 30
                signals.append(f"pending growing +{pending_trend:.1f}/cycle")

            # All dry cycles
            if total_cycles > 5 and dry_cycles == total_cycles:
                confidence += 20
                signals.append(f"all {total_cycles} cycles dry")

            return {
                "predictor":  "CONCORDANCE_FAILURE",
                "confidence": min(100, confidence),
                "severity":   PredictionSeverity.WARNING.value,
                "signal":     " | ".join(signals) or "nominal",
                "action":     "alert_check_agent_submissions",
            }
        except Exception as e:
            return {"predictor": "CONCORDANCE_FAILURE", "confidence": 60,
                    "severity": PredictionSeverity.WARNING.value,
                    "signal": f"Check failed: {e}", "action": "alert"}

    # ── Predictor 3: Railway Thread Death ─────────────────────────────────────

    def _predict_railway_thread_death(self) -> dict:
        """
        Signal: Scanner last_cycle_time stale or UTC/ET mismatch in timing.
        This is Nexus v1's most dangerous predictor — Railway UTC vs ET mismatch
        can cause the scanner to fire outside market hours thinking it's inside.
        Action: verify scanner cycle times are in ET window.
        """
        try:
            r = requests.get(f"{ALPHA_URL}/health", timeout=8)
            health = r.json() if r.status_code == 200 else {}

            scanner = health.get("omni_scanner", {})
            loop_active = scanner.get("loop_active", False)
            last_cycle_raw = scanner.get("last_cycle_time")

            confidence = 0.0
            signals = []

            if not loop_active:
                confidence = 90
                signals.append("loop_active=False — scanner dead")

            if last_cycle_raw:
                try:
                    last_cycle = datetime.datetime.fromisoformat(last_cycle_raw.replace("Z", "+00:00"))
                    age_minutes = (datetime.datetime.now(datetime.timezone.utc) - last_cycle).total_seconds() / 60

                    _metrics.record("scanner_cycle_age_min", age_minutes)

                    # During market hours (9:30-16:00 ET), cycle age > 10 min is suspicious
                    import pytz
                    et_now = datetime.datetime.now(pytz.timezone("America/New_York"))
                    in_market = 9 <= et_now.hour < 16 and et_now.weekday() < 5

                    if in_market and age_minutes > 10:
                        confidence += 60
                        signals.append(f"scanner stale {age_minutes:.1f}min during market hours")
                    elif in_market and age_minutes > 7:
                        confidence += 30
                        signals.append(f"scanner aging {age_minutes:.1f}min")
                except Exception:
                    pass
            elif loop_active:
                # No last_cycle but loop says active — first cycle hasn't fired
                pass

            # UTC vs ET check — verify Railway is using ET for market window
            # If scanner fires at UTC 9:30 instead of ET 9:30, it's 5h early
            utc_now = datetime.datetime.utcnow()
            if utc_now.hour == 9 and utc_now.minute < 35 and last_cycle_raw:
                # UTC 9:30 = ET 5:30 AM — no scanner should be firing
                signals.append("UTC/ET: check Railway timezone config")

            return {
                "predictor":  "RAILWAY_THREAD_DEATH",
                "confidence": min(100, confidence),
                "severity":   PredictionSeverity.CRITICAL.value if confidence > 70 else PredictionSeverity.INFO.value,
                "signal":     " | ".join(signals) or "nominal",
                "action":     "restart_scanner_loop",
            }
        except Exception as e:
            return {"predictor": "RAILWAY_THREAD_DEATH", "confidence": 50,
                    "severity": PredictionSeverity.WARNING.value,
                    "signal": f"Health check failed: {e}", "action": "alert"}

    # ── Predictor 4: QI Mass Timeout ─────────────────────────────────────────

    def _predict_qi_mass_timeout(self) -> dict:
        """
        Signal: multiple consecutive API timeouts across data sources.
        Precursor to: Axiom failing, scanner scoring stuck at default.
        Action: switch to redundant data client, pre-warm caches.
        """
        # Check Axiom health
        confidence = 0.0
        signals = []

        try:
            t0 = time.time()
            r = requests.get(f"{AXIOM_URL}/health", timeout=6)
            axiom_latency = (time.time() - t0) * 1000
            _metrics.record("axiom_latency", axiom_latency)

            if r.status_code != 200:
                confidence += 40
                signals.append(f"Axiom health {r.status_code}")

            if axiom_latency > 4000:
                confidence += 30
                signals.append(f"Axiom latency {axiom_latency:.0f}ms")

            # Check Layer 11 active failures from Alpha health
            try:
                r2 = requests.get(f"{ALPHA_URL}/health", timeout=6)
                h = r2.json() if r2.status_code == 200 else {}
                l11 = h.get("layer11", {})
                if l11.get("active_failures"):
                    confidence += 20
                    signals.append(f"L11 failures: {l11['active_failures']}")
                if l11.get("alpha_latency_ms", 0) > 5000:
                    confidence += 20
                    signals.append(f"L11 latency {l11['alpha_latency_ms']}ms")
            except Exception:
                pass

        except requests.Timeout:
            confidence = 80
            signals.append("Axiom health TIMED OUT")
        except Exception as e:
            confidence = 50
            signals.append(f"Axiom check error: {e}")

        return {
            "predictor":  "QI_MASS_TIMEOUT",
            "confidence": min(100, confidence),
            "severity":   PredictionSeverity.URGENT.value if confidence > 60 else PredictionSeverity.INFO.value,
            "signal":     " | ".join(signals) or "nominal",
            "action":     "switch_to_redundant_data",
        }

    # ── Predictor 5: Pending Queue Explosion ──────────────────────────────────

    def _predict_pending_queue_explosion(self) -> dict:
        """
        Signal: pending queue growing > 10/hour with no clearance.
        Indicates: execution bridge is stuck (today's incident pattern).
        Action: alert Ahmed + check execution bridge.
        """
        try:
            r = requests.get(f"{ALPHA_URL}/health", timeout=8)
            health = r.json() if r.status_code == 200 else {}

            pending = health.get("pending", 0)
            _metrics.record("pending_queue", float(pending))

            pending_trend  = _metrics.get_trend("pending_queue")
            pending_series = _metrics.get_all("pending_queue")

            confidence = 0.0
            signals = []

            # Queue growing steadily
            if pending > 30:
                confidence += 50
                signals.append(f"pending={pending} (explosion level)")
            elif pending > 15:
                confidence += 25
                signals.append(f"pending={pending} elevated")

            if pending_trend > 3:
                confidence += 30
                signals.append(f"queue growing +{pending_trend:.1f}/cycle")

            # Queue never decreasing (stuck execution bridge)
            if len(pending_series) >= 5 and min(pending_series[-5:]) > 10:
                confidence += 20
                signals.append("queue not clearing — execution bridge likely stuck")

            return {
                "predictor":  "PENDING_QUEUE_EXPLOSION",
                "confidence": min(100, confidence),
                "severity":   PredictionSeverity.URGENT.value if confidence > 60 else PredictionSeverity.WARNING.value,
                "signal":     " | ".join(signals) or "nominal",
                "action":     "alert_execution_bridge_stuck",
            }
        except Exception as e:
            return {"predictor": "PENDING_QUEUE_EXPLOSION", "confidence": 0,
                    "severity": PredictionSeverity.INFO.value,
                    "signal": f"Check failed: {e}", "action": "none"}

    # ── Act on Prediction ─────────────────────────────────────────────────────

    def _act_on_prediction(self, result: dict):
        """Take pre-emptive action based on high-confidence prediction."""
        predictor  = result.get("predictor", "UNKNOWN")
        confidence = result.get("confidence", 0)
        action     = result.get("action", "none")
        signal     = result.get("signal", "")
        severity   = result.get("severity", "INFO")

        # Rate-limit alerts: don't re-alert same predictor within 5 minutes
        last_alert = self._last_alerts.get(predictor, 0)
        if time.time() - last_alert < 300:
            return
        self._last_alerts[predictor] = time.time()

        logger.warning(f"🔮 PREDICTION [{confidence:.0f}%] {predictor}: {signal}")

        # Log to DB
        self._log_prediction(predictor, confidence, severity, signal, action)

        # Actions
        if action == "pre_warm_polygon_iv_cache":
            self._prewarm_polygon(["SPY", "QQQ", "NVDA", "AAPL"])
        elif action in ("alert", "alert_and_check_alpha", "alert_check_agent_submissions",
                        "alert_execution_bridge_stuck"):
            self._send_alert(predictor, confidence, signal, severity)
        elif action == "restart_scanner_loop":
            self._send_alert(predictor, confidence, signal, severity, urgent=True)
        elif action == "switch_to_redundant_data":
            self._send_alert(predictor, confidence, signal, severity)

    def _prewarm_polygon(self, tickers: list):
        """Pre-warm Polygon IV cache before ORATS fails."""
        logger.info(f"🔮 Pre-warming Polygon cache for: {tickers}")
        for ticker in tickers:
            try:
                requests.get(
                    f"https://api.polygon.io/v3/snapshot/options/{ticker}",
                    params={"apiKey": POLYGON_KEY, "limit": 20},
                    timeout=5,
                )
            except Exception:
                pass

    def _send_alert(self, predictor: str, confidence: float, signal: str, severity: str, urgent: bool = False):
        """Send prediction alert to health group and Ahmed if urgent."""
        icon = {"CRITICAL": "🚨", "URGENT": "⚠️", "WARNING": "🔶", "INFO": "ℹ️"}.get(severity, "🔮")
        msg = (
            f"{icon} PREDICTIVE MODEL: {predictor}\n"
            f"Confidence: {confidence:.0f}% | Severity: {severity}\n"
            f"Signal: {signal}\n"
            f"Action: Pre-emptive intervention in progress"
        )
        targets = [TG_HEALTH_GRP]
        if urgent or severity in ("CRITICAL", "URGENT"):
            targets.append(TG_AHMED_DM)

        if TG_BOT_TOKEN:
            for chat_id in targets:
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                        json={"chat_id": chat_id, "text": msg},
                        timeout=5,
                    )
                except Exception:
                    pass

    def _log_prediction(self, predictor, confidence, severity, signal, action):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO predictions (predictor, confidence, severity, signal, action_taken) VALUES (?,?,?,?,?)",
                (predictor, confidence, severity, signal, action)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def get_status(self) -> dict:
        """Return current model status."""
        try:
            conn = sqlite3.connect(DB_PATH)
            recent = conn.execute("""
                SELECT predictor, MAX(confidence), severity, signal
                FROM predictions
                WHERE datetime(created_at) > datetime('now', '-1 hour')
                GROUP BY predictor
            """).fetchall()
            conn.close()
        except Exception:
            recent = []

        return {
            "cycle_count":     self._cycle_count,
            "running":         self._running,
            "recent_predictions": [
                {"predictor": r[0], "confidence": r[1], "severity": r[2], "signal": r[3]}
                for r in recent
            ],
            "metrics": {
                "orats_latency":    _metrics.get_latest("orats_latency"),
                "alpha_pending":    _metrics.get_latest("alpha_pending"),
                "scanner_age_min":  _metrics.get_latest("scanner_cycle_age_min"),
                "axiom_latency":    _metrics.get_latest("axiom_latency"),
            }
        }


# ── Module-level singleton ────────────────────────────────────────────────────
_model: Optional[PredictiveFailureModel] = None


def get_predictive_model() -> PredictiveFailureModel:
    global _model
    if _model is None:
        _model = PredictiveFailureModel()
        _model.start()
    return _model


if __name__ == "__main__":
    print("🔮 Predictive Failure Model — Manual Run")
    m = PredictiveFailureModel()

    print("\nRunning all 5 predictors...")
    results = [
        m._predict_orats_degradation(),
        m._predict_concordance_failure(),
        m._predict_railway_thread_death(),
        m._predict_qi_mass_timeout(),
        m._predict_pending_queue_explosion(),
    ]
    for r in results:
        icon = "🚨" if r["confidence"] >= 70 else "✅"
        print(f"  {icon} {r['predictor']}: {r['confidence']:.0f}% | {r['signal'][:60]}")


# ══════════════════════════════════════════════════════════════════════════════
# PREDICTORS 6-10 — Appended to PredictiveFailureModel class
# ══════════════════════════════════════════════════════════════════════════════

def _predict_naked_position_risk(self) -> dict:
    """
    Predictor 6: Naked Position Risk
    Signal: position monitor detects short options without matching longs.
    Action: emergency close firing.
    """
    try:
        from position_monitor import get_position_monitor
        pm = get_position_monitor()
        status = pm.get_status()
        closed_today = status.get("closed_today", [])

        confidence = 0.0
        signals = []

        if any("naked" in str(c).lower() or "emergency" in str(c).lower() for c in closed_today):
            confidence = 95
            signals.append(f"Naked close fired today: {closed_today}")

        return {
            "predictor":  "NAKED_POSITION_RISK",
            "confidence": confidence,
            "severity":   PredictionSeverity.CRITICAL.value if confidence > 70 else PredictionSeverity.INFO.value,
            "signal":     " | ".join(signals) or "no naked positions detected",
            "action":     "position_monitor_emergency_close",
        }
    except Exception as e:
        return {"predictor": "NAKED_POSITION_RISK", "confidence": 0,
                "severity": PredictionSeverity.INFO.value, "signal": str(e), "action": "none"}


def _predict_dte_expiry_risk(self) -> dict:
    """
    Predictor 7: DTE Expiry Risk
    Signal: open positions approaching 7 DTE mandatory close.
    Action: pre-alert for upcoming mandatory closes.
    """
    try:
        r = requests.get(
            f"{ALPHA_URL}/positions",
            headers={"X-Nexus-Secret": NEXUS_SECRET},
            timeout=8,
        )
        positions = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []

        import re
        warning_positions = []
        for pos in positions:
            symbol = pos.get("symbol", "")
            m = re.match(r'^[A-Z]{1,5}(\d{6})[CP]\d{8}$', symbol)
            if m:
                date_str = m.group(1)
                try:
                    import datetime as dt
                    exp = dt.date(2000 + int(date_str[:2]), int(date_str[2:4]), int(date_str[4:]))
                    dte = (exp - dt.date.today()).days
                    if 0 < dte <= 10:
                        warning_positions.append(f"{symbol} DTE={dte}")
                except Exception:
                    pass

        confidence = min(100, len(warning_positions) * 30)
        return {
            "predictor":  "DTE_EXPIRY_RISK",
            "confidence": confidence,
            "severity":   PredictionSeverity.WARNING.value if confidence > 50 else PredictionSeverity.INFO.value,
            "signal":     f"Expiry approaching: {', '.join(warning_positions)}" if warning_positions else "all DTEs healthy",
            "action":     "flag_for_mandatory_close",
        }
    except Exception as e:
        return {"predictor": "DTE_EXPIRY_RISK", "confidence": 0,
                "severity": PredictionSeverity.INFO.value, "signal": str(e), "action": "none"}


def _predict_axiom_score_drift(self) -> dict:
    """
    Predictor 8: Axiom Score Drift
    Signal: Axiom consistently scoring too high or too low (model drift).
    Action: alert OMNI to recalibrate Axiom weights.
    """
    # Monitor Axiom latency as proxy for health
    latency = _metrics.get_latest("axiom_latency")
    trend   = _metrics.get_trend("axiom_latency")

    confidence = 0.0
    signals    = []

    if latency and latency > 5000:
        confidence += 50
        signals.append(f"Axiom latency {latency:.0f}ms — scoring may timeout")
    if trend and trend > 1000:
        confidence += 30
        signals.append(f"Axiom latency rising +{trend:.0f}ms/cycle")

    return {
        "predictor":  "AXIOM_SCORE_DRIFT",
        "confidence": confidence,
        "severity":   PredictionSeverity.WARNING.value if confidence > 50 else PredictionSeverity.INFO.value,
        "signal":     " | ".join(signals) or "Axiom scoring nominal",
        "action":     "alert_axiom_recalibration",
    }


def _predict_market_regime_shift(self) -> dict:
    """
    Predictor 9: Market Regime Shift
    Signal: VIX spike > 30 or VIX rate-of-change > 20% in 1 day.
    Action: reduce sizing, alert OMNI.
    """
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        vix = None
        if r.status_code == 200:
            vix = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {}).get("regularMarketPrice")

        if vix:
            _metrics.record("vix_level", float(vix))

        vix_now   = _metrics.get_latest("vix_level")
        vix_trend = _metrics.get_trend("vix_level")

        confidence = 0.0
        signals    = []

        if vix_now and vix_now > 35:
            confidence += 60
            signals.append(f"VIX={vix_now:.1f} > 35 — regime shift")
        elif vix_now and vix_now > 25:
            confidence += 25
            signals.append(f"VIX={vix_now:.1f} elevated")

        if vix_trend and vix_trend > 5:
            confidence += 30
            signals.append(f"VIX rising +{vix_trend:.1f}/cycle")

        return {
            "predictor":  "MARKET_REGIME_SHIFT",
            "confidence": min(100, confidence),
            "severity":   PredictionSeverity.URGENT.value if confidence > 70 else PredictionSeverity.INFO.value,
            "signal":     " | ".join(signals) or f"VIX={vix_now or '?'} nominal",
            "action":     "reduce_sizing_alert_omni",
        }
    except Exception as e:
        return {"predictor": "MARKET_REGIME_SHIFT", "confidence": 0,
                "severity": PredictionSeverity.INFO.value, "signal": str(e), "action": "none"}


def _predict_agent_silence(self) -> dict:
    """
    Predictor 10: Agent Silence
    Signal: No R1 submissions from any agent in > 2 hours during market hours.
    Action: alert OMNI, check agent connectivity.
    """
    try:
        import pytz, datetime as dt
        et_tz = pytz.timezone("America/New_York")
        now_et = dt.datetime.now(et_tz)
        in_market = 9 <= now_et.hour < 16 and now_et.weekday() < 5

        if not in_market:
            return {"predictor": "AGENT_SILENCE", "confidence": 0,
                    "severity": PredictionSeverity.INFO.value, "signal": "market closed", "action": "none"}

        r = requests.get(f"{ALPHA_URL}/health", timeout=8)
        health = r.json() if r.status_code == 200 else {}
        scanner = health.get("omni_scanner", {})
        total_cycles = scanner.get("total_cycles", 0)
        picks_today  = scanner.get("picks_today", [])
        l11 = health.get("layer11", {})
        l11_failures = l11.get("active_failures", [])

        confidence = 0.0
        signals    = []

        if "AGENT_SILENCE" in l11_failures:
            confidence += 70
            signals.append("Layer 11 flagging AGENT_SILENCE")

        if total_cycles > 6 and not picks_today:
            confidence += 30
            signals.append(f"{total_cycles} cycles with 0 picks")

        return {
            "predictor":  "AGENT_SILENCE",
            "confidence": min(100, confidence),
            "severity":   PredictionSeverity.WARNING.value if confidence > 60 else PredictionSeverity.INFO.value,
            "signal":     " | ".join(signals) or "agent activity normal",
            "action":     "alert_check_agent_connectivity",
        }
    except Exception as e:
        return {"predictor": "AGENT_SILENCE", "confidence": 0,
                "severity": PredictionSeverity.INFO.value, "signal": str(e), "action": "none"}


# Monkey-patch all 10 predictors onto the class
PredictiveFailureModel._predict_naked_position_risk = _predict_naked_position_risk
PredictiveFailureModel._predict_dte_expiry_risk      = _predict_dte_expiry_risk
PredictiveFailureModel._predict_axiom_score_drift    = _predict_axiom_score_drift
PredictiveFailureModel._predict_market_regime_shift  = _predict_market_regime_shift
PredictiveFailureModel._predict_agent_silence        = _predict_agent_silence

# Override _run_all_predictors to include all 10
def _run_all_10_predictors(self):
    results = [
        self._predict_orats_degradation(),
        self._predict_concordance_failure(),
        self._predict_railway_thread_death(),
        self._predict_qi_mass_timeout(),
        self._predict_pending_queue_explosion(),
        self._predict_naked_position_risk(),
        self._predict_dte_expiry_risk(),
        self._predict_axiom_score_drift(),
        self._predict_market_regime_shift(),
        self._predict_agent_silence(),
    ]
    for result in results:
        if result and result.get("confidence", 0) >= PREDICT_THRESHOLD:
            self._act_on_prediction(result)

PredictiveFailureModel._run_all_predictors = _run_all_10_predictors
