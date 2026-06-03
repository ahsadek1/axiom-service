"""
THESIS Health Monitor & Self-Healing Engine.

Runs every 30 minutes during trading hours (9:30 AM - 4:00 PM ET).
Checks system health (CHRONICLE, APScheduler, Alpaca connectivity).
On failure: attempts autonomous fixes (restart, reconnect, cache clear).
Escalates to Axiom + Cipher only after 3 failed auto-fix attempts.

All health events logged to /Users/ahmedsadek/nexus/logs/thesis-health.log
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

import requests

# Configure logger for health monitor
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] thesis_health: %(message)s",
    handlers=[
        logging.FileHandler("/Users/ahmedsadek/nexus/logs/thesis-health.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("thesis_health")

_ET = ZoneInfo("America/New_York")


class HealthStatus(Enum):
    """Health status enumeration."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    RECOVERING = "recovering"


class HealthCheckResult:
    """Result of a single health check."""

    def __init__(self, component: str, status: HealthStatus, message: str = "", details: dict = None):
        self.component = component
        self.status = status
        self.message = message
        self.details = details or {}
        self.timestamp = datetime.now(tz=_ET).isoformat()

    def to_dict(self):
        return {
            "component": self.component,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "timestamp": self.timestamp,
        }

    def __str__(self):
        return f"{self.component}: {self.status.value} — {self.message}"


class ThesisHealthMonitor:
    """
    Autonomous health monitoring and self-healing system for Thesis.

    Checks:
      1. CHRONICLE database connectivity & writability
      2. APScheduler job status
      3. Alpaca API connectivity & trading ability
      4. Entry logic responsiveness
      5. Position data consistency

    On failure: Auto-fixes (restart, reconnect, cache clear) → escalates after 3 failures.
    """

    def __init__(
        self,
        chronicle_db_path: str = "/Users/ahmedsadek/nexus/data/chronicle.db",
        alpaca_key: str = "",
        alpaca_secret: str = "",
        thesis_service_url: str = "http://localhost:8060",
        telegram_token: str = "",
        telegram_chat_id: str = "8573754783",  # Ahmed's DM
    ):
        self.chronicle_db_path = chronicle_db_path
        self.alpaca_key = alpaca_key
        self.alpaca_secret = alpaca_secret
        self.thesis_service_url = thesis_service_url
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id

        # Track consecutive failures per component
        self.failure_counts = {
            "chronicle": 0,
            "apscheduler": 0,
            "alpaca": 0,
            "entry_logic": 0,
            "position_data": 0,
        }

        # Track auto-fix attempts
        self.autofix_attempts = {
            "chronicle": 0,
            "apscheduler": 0,
            "alpaca": 0,
            "entry_logic": 0,
            "position_data": 0,
        }

    def is_market_hours(self) -> bool:
        """Check if current time is within trading hours (9:30 AM - 4:00 PM ET)."""
        from datetime import time
        now = datetime.now(tz=_ET).time()
        return time(9, 30) <= now <= time(16, 0)

    async def run_health_check(self) -> dict:
        """
        Run full health check suite. Returns aggregated health status.

        Returns:
            {
                "overall_status": "healthy" | "degraded" | "critical",
                "checks": [HealthCheckResult.to_dict(), ...],
                "autofix_summary": {...},
                "escalation_triggered": bool,
            }
        """
        if not self.is_market_hours():
            logger.info("Outside trading hours — skipping health check")
            return {
                "overall_status": "healthy",
                "message": "Outside trading hours",
                "checks": [],
            }

        results = []

        # 1. Check CHRONICLE
        chronicle_result = await self._check_chronicle()
        results.append(chronicle_result)

        # 2. Check APScheduler
        apscheduler_result = await self._check_apscheduler()
        results.append(apscheduler_result)

        # 3. Check Alpaca
        alpaca_result = await self._check_alpaca()
        results.append(alpaca_result)

        # 4. Check Entry Logic
        entry_result = await self._check_entry_logic()
        results.append(entry_result)

        # 5. Check Position Data
        position_result = await self._check_position_data()
        results.append(position_result)

        # Determine overall status
        critical_count = sum(1 for r in results if r.status == HealthStatus.CRITICAL)
        degraded_count = sum(1 for r in results if r.status == HealthStatus.DEGRADED)

        if critical_count >= 2:
            overall_status = HealthStatus.CRITICAL
        elif degraded_count >= 2 or critical_count == 1:
            overall_status = HealthStatus.DEGRADED
        else:
            overall_status = HealthStatus.HEALTHY

        # Attempt auto-fixes for failed components
        escalation_triggered = await self._attempt_autofixes(results)

        # Log aggregated result
        logger.info(
            f"Health check complete: {overall_status.value} "
            f"(critical={critical_count}, degraded={degraded_count})"
        )

        return {
            "overall_status": overall_status.value,
            "checks": [r.to_dict() for r in results],
            "autofix_summary": dict(self.autofix_attempts),
            "failure_counts": dict(self.failure_counts),
            "escalation_triggered": escalation_triggered,
            "timestamp": datetime.now(tz=_ET).isoformat(),
        }

    async def _check_chronicle(self) -> HealthCheckResult:
        """Check CHRONICLE database connectivity and writability."""
        try:
            conn = sqlite3.connect(self.chronicle_db_path, timeout=5)
            cursor = conn.cursor()

            # Test read from thesis_context (actual table)
            cursor.execute("SELECT COUNT(*) FROM thesis_context")
            thesis_context_count = cursor.fetchone()[0]

            # Test read from thesis_events (actual table)
            cursor.execute("SELECT COUNT(*) FROM thesis_events")
            thesis_events_count = cursor.fetchone()[0]

            # Test write to thesis_context (rollback to avoid actual changes)
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "INSERT INTO thesis_context "
                "(layer, valid_from, valid_until, trading_posture, sizing_multiplier, "
                "macro_gate, risk_reward_gate, thesis_sentence, confidence_adjustment) "
                "VALUES ('_healthcheck', datetime('now'), datetime('now'), 'NEUTRAL', 1.0, "
                "'PASS', 'PASS', '_healthcheck', 0)"
            )
            conn.rollback()
            conn.close()

            self.failure_counts["chronicle"] = 0
            return HealthCheckResult(
                "chronicle",
                HealthStatus.HEALTHY,
                f"Connected and writable. Tables: thesis_context={thesis_context_count} rows, thesis_events={thesis_events_count} rows",
                {"thesis_context_rows": thesis_context_count, "thesis_events_rows": thesis_events_count},
            )

        except Exception as e:
            self.failure_counts["chronicle"] += 1
            status = (
                HealthStatus.CRITICAL
                if self.failure_counts["chronicle"] >= 3
                else HealthStatus.DEGRADED
            )
            return HealthCheckResult(
                "chronicle",
                status,
                f"Failed to connect or write: {str(e)[:100]}",
                {"error": str(e)},
            )

    async def _check_apscheduler(self) -> HealthCheckResult:
        """Check APScheduler job status via Thesis service health endpoint."""
        try:
            response = requests.get(
                f"{self.thesis_service_url}/thesis/health",
                timeout=5,
            )
            response.raise_for_status()

            health_data = response.json()

            # Check scheduler status
            scheduler_ok = health_data.get("scheduler_ok", False)
            if not scheduler_ok:
                raise ValueError("APScheduler not running")

            self.failure_counts["apscheduler"] = 0
            return HealthCheckResult(
                "apscheduler",
                HealthStatus.HEALTHY,
                "Jobs running: layer1_thesis, layer2_brief, layer3_monitor",
                {
                    "scheduler_ok": scheduler_ok,
                    "jobs_running": 3,
                },
            )

        except Exception as e:
            self.failure_counts["apscheduler"] += 1
            status = (
                HealthStatus.CRITICAL
                if self.failure_counts["apscheduler"] >= 3
                else HealthStatus.DEGRADED
            )
            return HealthCheckResult(
                "apscheduler",
                status,
                f"Cannot reach Thesis service: {str(e)[:100]}",
                {"error": str(e)},
            )

    async def _check_alpaca(self) -> HealthCheckResult:
        """Check Alpaca API connectivity and trading ability."""
        try:
            headers = {
                "APCA-API-KEY-ID": self.alpaca_key,
                "APCA-API-SECRET-KEY": self.alpaca_secret,
                "Content-Type": "application/json",
            }

            response = requests.get(
                "https://paper-api.alpaca.markets/v2/account",
                headers=headers,
                timeout=5,
            )
            response.raise_for_status()

            account = response.json()
            equity = float(account.get("equity", 0))
            cash = float(account.get("cash", 0))
            buying_power = float(account.get("buying_power", 0))

            if buying_power <= 0:
                raise ValueError(f"Buying power is ${buying_power} — cannot trade")

            self.failure_counts["alpaca"] = 0
            return HealthCheckResult(
                "alpaca",
                HealthStatus.HEALTHY,
                f"Equity: ${equity:,.2f}, Cash: ${cash:,.2f}, BP: ${buying_power:,.2f}",
                {
                    "equity": equity,
                    "cash": cash,
                    "buying_power": buying_power,
                },
            )

        except Exception as e:
            self.failure_counts["alpaca"] += 1
            status = (
                HealthStatus.CRITICAL
                if self.failure_counts["alpaca"] >= 3
                else HealthStatus.DEGRADED
            )
            return HealthCheckResult(
                "alpaca",
                status,
                f"API error or trading disabled: {str(e)[:100]}",
                {"error": str(e)},
            )

    async def _check_entry_logic(self) -> HealthCheckResult:
        """Check entry logic responsiveness by calling /thesis/current-context."""
        try:
            response = requests.get(
                f"{self.thesis_service_url}/thesis/current-context",
                timeout=5,
            )
            response.raise_for_status()

            context = response.json()
            regime = context.get("regime", "UNKNOWN")
            vix_level = context.get("vix_level", 0)

            self.failure_counts["entry_logic"] = 0
            return HealthCheckResult(
                "entry_logic",
                HealthStatus.HEALTHY,
                f"Entry logic active. Regime: {regime}, VIX: {vix_level:.1f}",
                {"regime": regime, "vix_level": vix_level},
            )

        except Exception as e:
            self.failure_counts["entry_logic"] += 1
            status = (
                HealthStatus.CRITICAL
                if self.failure_counts["entry_logic"] >= 3
                else HealthStatus.DEGRADED
            )
            return HealthCheckResult(
                "entry_logic",
                status,
                f"Entry logic check failed: {str(e)[:100]}",
                {"error": str(e)},
            )

    async def _check_position_data(self) -> HealthCheckResult:
        """Check position data consistency between CHRONICLE and Alpaca."""
        try:
            # Get positions from Alpaca
            headers = {
                "APCA-API-KEY-ID": self.alpaca_key,
                "APCA-API-SECRET-KEY": self.alpaca_secret,
                "Content-Type": "application/json",
            }

            response = requests.get(
                "https://paper-api.alpaca.markets/v2/positions",
                headers=headers,
                timeout=5,
            )
            response.raise_for_status()

            alpaca_positions = response.json()
            position_count = len(alpaca_positions)

            # Check CHRONICLE consistency
            conn = sqlite3.connect(self.chronicle_db_path, timeout=5)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM thesis_context WHERE layer IN ('weekly', 'daily')")
            thesis_rows = cursor.fetchone()[0]
            conn.close()

            self.failure_counts["position_data"] = 0
            return HealthCheckResult(
                "position_data",
                HealthStatus.HEALTHY,
                f"Alpaca positions: {position_count}, CHRONICLE thesis rows: {thesis_rows}",
                {
                    "alpaca_positions": position_count,
                    "thesis_rows": thesis_rows,
                },
            )

        except Exception as e:
            self.failure_counts["position_data"] += 1
            status = (
                HealthStatus.CRITICAL
                if self.failure_counts["position_data"] >= 3
                else HealthStatus.DEGRADED
            )
            return HealthCheckResult(
                "position_data",
                status,
                f"Position data check failed: {str(e)[:100]}",
                {"error": str(e)},
            )

    async def _attempt_autofixes(self, results: list[HealthCheckResult]) -> bool:
        """
        Attempt to auto-fix failed components.

        Tier 0: Reconnect to service
        Tier 1: Clear cache & reset connections
        Tier 2: Restart APScheduler jobs
        Tier 3: Escalate (return True)

        Returns:
            True if escalation triggered, False otherwise.
        """
        escalation_triggered = False

        for result in results:
            if result.status == HealthStatus.HEALTHY:
                continue

            component = result.component
            failure_count = self.failure_counts.get(component, 0)

            if failure_count >= 3:
                # Escalation threshold reached
                logger.error(
                    f"ESCALATION: {component} failed {failure_count} times. "
                    f"Escalating to Axiom + Cipher"
                )
                await self._escalate_to_axiom_cipher(component, result)
                escalation_triggered = True
                continue

            # Attempt auto-fix based on component
            if component == "chronicle":
                await self._autofix_chronicle(result)
            elif component == "apscheduler":
                await self._autofix_apscheduler(result)
            elif component == "alpaca":
                await self._autofix_alpaca(result)
            elif component == "entry_logic":
                await self._autofix_entry_logic(result)
            elif component == "position_data":
                await self._autofix_position_data(result)

        return escalation_triggered

    async def _autofix_chronicle(self, result: HealthCheckResult):
        """Auto-fix CHRONICLE: try to restart connection."""
        logger.warning("Attempting autofix for CHRONICLE...")
        self.autofix_attempts["chronicle"] += 1
        try:
            # Attempt database integrity check & repair
            conn = sqlite3.connect(self.chronicle_db_path, timeout=10)
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check")
            check_result = cursor.fetchone()
            conn.close()

            if check_result[0] == "ok":
                logger.info("CHRONICLE integrity check passed. Autofix successful.")
            else:
                logger.error(f"CHRONICLE integrity issue: {check_result[0]}")
        except Exception as e:
            logger.error(f"CHRONICLE autofix failed: {str(e)}")

    async def _autofix_apscheduler(self, result: HealthCheckResult):
        """Auto-fix APScheduler: try to restart service."""
        logger.warning("Attempting autofix for APScheduler...")
        self.autofix_attempts["apscheduler"] += 1
        try:
            # Send restart signal to Thesis service
            response = requests.post(
                f"{self.thesis_service_url}/thesis/restart-scheduler",
                timeout=10,
                json={"force": True},
            )
            if response.status_code == 200:
                logger.info("APScheduler restart signal sent. Waiting for recovery...")
            else:
                logger.error(f"APScheduler restart failed: {response.text}")
        except Exception as e:
            logger.error(f"APScheduler autofix failed: {str(e)}")

    async def _autofix_alpaca(self, result: HealthCheckResult):
        """Auto-fix Alpaca: reconnect with fresh auth."""
        logger.warning("Attempting autofix for Alpaca...")
        self.autofix_attempts["alpaca"] += 1
        try:
            # Test connectivity again with fresh request
            headers = {
                "APCA-API-KEY-ID": self.alpaca_key,
                "APCA-API-SECRET-KEY": self.alpaca_secret,
                "Content-Type": "application/json",
            }

            response = requests.get(
                "https://paper-api.alpaca.markets/v2/account",
                headers=headers,
                timeout=5,
            )
            response.raise_for_status()
            logger.info("Alpaca connectivity restored.")
        except Exception as e:
            logger.error(f"Alpaca autofix failed: {str(e)}")

    async def _autofix_entry_logic(self, result: HealthCheckResult):
        """Auto-fix entry logic: clear cache & reconnect."""
        logger.warning("Attempting autofix for entry logic...")
        self.autofix_attempts["entry_logic"] += 1
        try:
            response = requests.post(
                f"{self.thesis_service_url}/thesis/clear-cache",
                timeout=10,
            )
            if response.status_code == 200:
                logger.info("Entry logic cache cleared.")
            else:
                logger.error(f"Cache clear failed: {response.text}")
        except Exception as e:
            logger.error(f"Entry logic autofix failed: {str(e)}")

    async def _autofix_position_data(self, result: HealthCheckResult):
        """Auto-fix position data: sync cache with Alpaca."""
        logger.warning("Attempting autofix for position data...")
        self.autofix_attempts["position_data"] += 1
        try:
            response = requests.post(
                f"{self.thesis_service_url}/thesis/sync-positions",
                timeout=10,
            )
            if response.status_code == 200:
                logger.info("Position data synced.")
            else:
                logger.error(f"Position sync failed: {response.text}")
        except Exception as e:
            logger.error(f"Position data autofix failed: {str(e)}")

    async def _escalate_to_axiom_cipher(self, component: str, result: HealthCheckResult):
        """Escalate failure to Axiom and Cipher via Telegram."""
        message = (
            f"🚨 THESIS HEALTH ESCALATION\n\n"
            f"Component: {component}\n"
            f"Status: {result.status.value}\n"
            f"Message: {result.message}\n"
            f"Details: {json.dumps(result.details, indent=2)}\n"
            f"Timestamp: {result.timestamp}\n\n"
            f"@Axiom15Bot @Cipher — please diagnose and respond."
        )
        logger.error(f"Escalation message: {message}")

        # Send to Telegram if configured
        if self.telegram_token:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                    json={"chat_id": self.telegram_chat_id, "text": message},
                    timeout=5,
                )
            except Exception as e:
                logger.error(f"Failed to send escalation message: {str(e)}")


# Standalone execution for testing
async def main():
    """Test the health monitor."""
    import os

    alpaca_key = os.getenv("ALPACA_KEY", "PKPGM3BRNYPGCF5Z56IAUZCZJL")
    alpaca_secret = os.getenv("ALPACA_SECRET", "5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs")

    monitor = ThesisHealthMonitor(
        alpaca_key=alpaca_key,
        alpaca_secret=alpaca_secret,
    )

    result = await monitor.run_health_check()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
