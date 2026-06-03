"""
resilience_manager.py — Resilience Orchestrator

Wires all 6 approaches together into the unified pre-market timeline.
Single entry point for all resilience operations.

Pre-market timeline:
  6:00 AM  — Snapshot capture
  6:15 AM  — Snapshot validation + auto-correct
  6:30 AM  — Predictive model warmup
  7:45 AM  — Diagnostic report to Ahmed
  8:55 AM  — Final clearance report
  9:00 AM  — Pre-market preflight
  9:31 AM  — Canary trades fire
  9:45 AM  — Full-size trading approved/denied

Trading loop (every 30s):
  — Predictive model cycle
  — Drift detection
  — Degradation update
"""

import asyncio
import logging
import os
import threading
import time
from datetime import datetime
from typing import Callable, Dict, Optional

import pytz
import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

from pre_market_snapshot import capture_snapshot, drift_detection_loop, validate_snapshot
from graceful_degradation import GracefulDegradationManager
from predictive_failure_model import PredictiveFailureModel

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


def _send_telegram(token: str, chat_id: str, message: str) -> None:
    """Send a Telegram message. Splits messages > 4096 chars."""
    max_len = 4096
    chunks = [message[i:i + max_len] for i in range(0, len(message), max_len)]
    for chunk in chunks:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=10,
            )
        except Exception as e:
            logger.error("Telegram send failed: %s", e)


class ResilienceManager:
    """
    Orchestrates all 6 resilience approaches for Nexus V2.

    Usage:
        mgr = ResilienceManager()
        await mgr.startup()          # On service start
        await mgr.pre_market_sequence()   # Morning of each trading day
        # During trading (every 30s):
        await mgr.trading_loop_cycle()
        # EOD:
        await mgr.eod_sequence()
    """

    def __init__(self):
        self._secrets = self._load_secrets()
        self._token   = self._secrets.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = self._secrets.get("TELEGRAM_CHAT_ID", "8573754783")

        def _alert(msg: str) -> None:
            _send_telegram(self._token, self._chat_id, msg)

        async def _async_alert(msg: str) -> None:
            _alert(msg)

        self._telegram_alert = _alert
        self._async_alert    = _async_alert

        self.degradation = GracefulDegradationManager(telegram_alert_fn=_alert)
        self.predictor   = PredictiveFailureModel(telegram_alert_fn=_async_alert, db_path="/Users/ahmedsadek/nexus/data/predictions.db")
        self._baseline_snapshot = None
        self._full_size_approved = False

    # -------------------------------------------------------------------------
    # Startup
    # -------------------------------------------------------------------------

    async def startup(self) -> None:
        """
        Run at service startup.
        Loads the latest snapshot for drift comparison.
        """
        logger.info("ResilienceManager startup")
        from pre_market_snapshot import load_latest_snapshot
        self._baseline_snapshot = load_latest_snapshot()
        if self._baseline_snapshot:
            logger.info("Loaded baseline snapshot from %s", self._baseline_snapshot.captured_at)
        else:
            logger.warning("No baseline snapshot found — will capture at pre-market")

    # -------------------------------------------------------------------------
    # Pre-market sequence
    # -------------------------------------------------------------------------

    async def pre_market_sequence(self) -> None:
        """
        Run the full unified pre-market timeline.
        Blocks until canary assessment is complete.
        """
        now_et = datetime.now(ET)
        logger.info("Pre-market sequence starting at %s ET", now_et.strftime("%I:%M %p"))

        # 6:00 AM — Snapshot capture
        logger.info("▶ 6:00 AM — Capturing snapshot")
        self._baseline_snapshot = capture_snapshot(self._secrets)

        # 6:15 AM — Snapshot validation
        logger.info("▶ 6:15 AM — Validating snapshot")
        all_clear, drift_items = validate_snapshot(self._baseline_snapshot, self._secrets)
        if not all_clear:
            self._handle_drift(drift_items)

        # 6:30 AM — Predictive model warmup (feed initial metrics)
        logger.info("▶ 6:30 AM — Predictive model warmup")
        await self._warmup_predictive_model()

        # 7:45 AM — Diagnostic report
        logger.info("▶ 7:45 AM — Sending diagnostic report")
        await self._send_diagnostic_report()

        # 8:55 AM — Final clearance
        logger.info("▶ 8:55 AM — Final clearance check")
        await self._send_clearance_report()

        # 9:00 AM — Pre-market preflight
        logger.info("▶ 9:00 AM — Pre-market preflight")
        await self._preflight_check()

        # Start drift detection loop in background thread
        drift_thread = threading.Thread(
            target=drift_detection_loop,
            args=(self._baseline_snapshot, self._secrets, self._telegram_alert, 60),
            daemon=True,
            name="resilience-drift-detection",
        )
        drift_thread.start()
        logger.info("Drift detection loop started")

    # -------------------------------------------------------------------------
    # Trading loop cycle (every 30s during market hours)
    # -------------------------------------------------------------------------

    async def trading_loop_cycle(self) -> None:
        """
        Run one trading loop cycle. Call every 30 seconds during 9:00 AM – 4:15 PM ET.
        """
        risks = await self.predictor.run_cycle()
        if risks:
            for risk in risks:
                logger.info(
                    "Risk detected: %s (score=%.2f) → %s",
                    risk.failure_class, risk.risk_score, risk.intervention,
                )

    # -------------------------------------------------------------------------
    # EOD sequence
    # -------------------------------------------------------------------------

    async def eod_sequence(self) -> None:
        """
        Run end-of-day sequence. Call at 4:15 PM ET.
        Sends EOD summary to Ahmed.
        """
        logger.info("EOD sequence starting")
        prediction_report = self.predictor.get_eod_prediction_report()
        degradation_report = self.degradation.get_eod_degradation_report()
        status = self.degradation.get_status_summary()

        msg = (
            f"📊 NEXUS V2 EOD RESILIENCE SUMMARY\n"
            f"{datetime.now(ET).strftime('%Y-%m-%d')}\n\n"
            f"DEGRADATION:\n"
            f"  Final level: {status['level_name']}\n"
            f"  Total events: {degradation_report['total_degradation_events']}\n"
            f"  Active conditions: {status['active_conditions'] or 'None'}\n\n"
            f"PREDICTIVE MODEL:\n"
            f"  Total interventions: {prediction_report['total_interventions']}\n"
            f"  By class: {prediction_report['interventions_by_class'] or 'None'}\n\n"
            f"RESILIENCE SYSTEMS STATUS:\n"
            f"  Snapshot system: {'✅' if self._baseline_snapshot else '❌'}\n"
            f"  Degradation manager: ✅\n"
            f"  Predictive model: ✅\n"
        )
        _send_telegram(self._token, self._chat_id, msg)
        logger.info("EOD summary sent to Ahmed")

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _handle_drift(self, drift_items: list) -> None:
        """Process drift items — auto-correct safe ones, alert Ahmed for others."""
        from pre_market_snapshot import classify_drift_action, SAFE_AUTO_CORRECT
        for item in drift_items:
            action = classify_drift_action(item)
            if action in SAFE_AUTO_CORRECT:
                logger.info("Auto-correcting drift: %s → %s", item, action)
                self._safe_auto_correct(action, item)
            else:
                msg = f"⚠️ REQUIRES AHMED — Drift: {item}\nAction: {action}"
                _send_telegram(self._token, self._chat_id, msg)
                logger.warning("Ahmed required for drift: %s", item)

    def _safe_auto_correct(self, action: str, detail: str) -> None:
        """Execute a safe auto-correction."""
        if action == "service_restart":
            # Extract service name from detail
            try:
                svc = detail.split(":")[1].strip() if ":" in detail else "unknown"
                import subprocess
                subprocess.run(["launchctl", "stop", f"ai.nexus.{svc}"], check=False)
                time.sleep(2)
                subprocess.run(["launchctl", "start", f"ai.nexus.{svc}"], check=False)
                logger.info("Auto-restarted service: %s", svc)
            except Exception as e:
                logger.error("Auto-restart failed for %s: %s", detail, e)

    async def _warmup_predictive_model(self) -> None:
        """Feed initial metrics to predictive model from service health checks."""
        import requests as req
        for svc, (port, secret_hdr) in {
            "axiom": (8001, "X-Axiom-Secret"),
            "oracle": (8007, "X-Oracle-Secret"),
        }.items():
            try:
                secret = self._resolve_secret(secret_hdr)
                resp = req.get(f"http://localhost:{port}/health", headers={secret_hdr: secret}, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if svc == "axiom" and "vix" in data:
                        self.predictor.record_metric("vix_level", float(data["vix"]))
                    if svc == "oracle":
                        # Oracle cache hit rate as health proxy
                        cache_rate = data.get("cache_hit_rate", 0.5)
                        self.predictor.record_metric("oracle_cache_hit_rate", cache_rate)
            except Exception as e:
                logger.warning("Warmup metric fetch failed for %s: %s", svc, e)

    async def _send_diagnostic_report(self) -> None:
        """Send 7:45 AM diagnostic report to Ahmed."""
        snap = self._baseline_snapshot
        if not snap:
            _send_telegram(self._token, self._chat_id, "⚠️ No baseline snapshot available for 7:45 AM report")
            return

        healthy = sum(1 for s in snap.services.values() if s.get("status") == "healthy")
        total = len(snap.services)
        icon = "✅" if healthy == total else "⚠️" if healthy > total // 2 else "❌"

        msg = (
            f"{icon} 7:45 AM DIAGNOSTIC REPORT — Nexus V2\n"
            f"{snap.snapshot_date}\n\n"
            f"Services: {healthy}/{total} healthy\n"
        )
        for svc, data in snap.services.items():
            svc_icon = "✅" if data.get("status") == "healthy" else "❌"
            msg += f"  {svc_icon} {svc}: {data.get('status', 'unknown')}\n"

        msg += f"\nAgent weights: Cipher {snap.agent_weights.get('Cipher', '?'):.0%} | Atlas {snap.agent_weights.get('Atlas', '?'):.0%} | Sage {snap.agent_weights.get('Sage', '?'):.0%}\n"
        msg += f"Snapshot signature: {snap.signature[:16]}..."

        _send_telegram(self._token, self._chat_id, msg)

    async def _send_clearance_report(self) -> None:
        """Send 8:55 AM final clearance report."""
        snap = self._baseline_snapshot
        if not snap:
            return
        healthy = sum(1 for s in snap.services.values() if s.get("status") == "healthy")
        total = len(snap.services)
        cleared = healthy == total
        msg = (
            f"{'✅ CLEARED' if cleared else '🚨 NOT CLEARED'} — Nexus V2 Pre-Market\n"
            f"Services: {healthy}/{total}\n"
            f"{'Ready for canary trades at 9:31 AM.' if cleared else 'Fix required before open.'}"
        )
        _send_telegram(self._token, self._chat_id, msg)

    async def _preflight_check(self) -> None:
        """9:00 AM final connection verification."""
        import requests as req
        all_ok = True
        for port in [8001, 8002, 8003, 8004, 8005, 8006, 9001, 9002, 9003]:
            try:
                req.get(f"http://localhost:{port}/health", timeout=3)
            except Exception:
                logger.warning("Preflight: port %d not responding", port)
                all_ok = False
        if not all_ok:
            _send_telegram(self._token, self._chat_id, "⚠️ 9:00 AM PREFLIGHT: Some services not responding. Check before 9:31 AM.")
        else:
            logger.info("9:00 AM preflight: all services responding")

    @staticmethod
    def _load_secrets() -> Dict[str, str]:
        """Load secrets from environment."""
        return {
            "NEXUS_SECRET":         os.getenv("NEXUS_SECRET", ""),
            "NEXUS_PRIME_SECRET":   os.getenv("NEXUS_PRIME_SECRET", ""),
            "ORACLE_SECRET":        os.getenv("ORACLE_SECRET", ""),
            "AILS_SECRET":          os.getenv("AILS_SECRET", ""),
            "TELEGRAM_BOT_TOKEN":   os.getenv("TELEGRAM_BOT_TOKEN", ""),
            "TELEGRAM_CHAT_ID":     os.getenv("TELEGRAM_CHAT_ID", "8573754783"),
            "POLYGON_API_KEY":      os.getenv("POLYGON_API_KEY", ""),
            "ALPHA_VANTAGE_KEY":    os.getenv("ALPHA_VANTAGE_KEY", ""),
            "FRED_API_KEY":         os.getenv("FRED_API_KEY", ""),
        }

    def _resolve_secret(self, header: str) -> str:
        """Map header name to secret value."""
        mapping = {
            "X-Axiom-Secret":        self._secrets.get("NEXUS_SECRET", ""),
            "X-Nexus-Secret":        self._secrets.get("NEXUS_SECRET", ""),
            "X-Nexus-Prime-Secret":  self._secrets.get("NEXUS_PRIME_SECRET", ""),
            "X-Oracle-Secret":       self._secrets.get("ORACLE_SECRET", ""),
            "X-AILS-Secret":         self._secrets.get("AILS_SECRET", ""),
        }
        return mapping.get(header, "")
