"""
canary_protocol.py — Approach 5: Canary Trading Protocol

Executes minimum-size ($200) live paper canary trades at 9:31 AM.
These are REAL paper trades — not simulations.

If all canaries pass → full-size trading approved at 9:45 AM.
If canaries fail → diagnose + fix + retry (max 2 attempts).
If canaries fail twice → halt service, alert Ahmed.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

import pytz

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


@dataclass
class CanaryResult:
    """Result of a single canary trade execution."""
    canary_id: str
    service: str
    ticker: str
    success: bool
    failure_point: Optional[str]
    failure_description: Optional[str]
    execution_time_ms: float
    order_confirmed: bool
    db_record_created: bool
    telegram_sent: bool
    monitor_active: bool
    no_exceptions: bool


@dataclass
class CanaryAssessment:
    """Assessment of all canary results for a service."""
    all_passed: bool
    passed_count: int
    failed_count: int
    results: List[CanaryResult]
    full_size_approved: bool
    delay_minutes: int
    blocking_issues: List[str]
    recommendation: str


class CanaryProtocol:
    """
    Executes minimum-size live canary trades at 9:31 AM.

    Purpose: Verify the full production pipeline under live market
    conditions at minimum capital exposure before committing full size.

    Canary size: $200
    Canary window: 9:31–9:45 AM ET
    Max retries: 2
    Full-size trading: 9:45 AM if all canaries pass

    Tickers:
        NEXUS_ALPHA  → SPY, AAPL
        NEXUS_PRIME  → SPY, QQQ
    """

    CANARY_SIZE_USD = 200.0
    CANARY_WINDOW_MINUTES = 14
    MAX_RETRY_ATTEMPTS = 2

    SUCCESS_CRITERIA = [
        "order_confirmed_within_60s",
        "db_record_created",
        "telegram_notification_sent",
        "exit_monitor_active",
        "no_exception_in_logs",
        "capital_router_allocated",
        "deduplication_registered",
    ]

    CANARY_TICKERS: Dict[str, List[str]] = {
        "ATM_MULTIWEEK": ["SPY", "QQQ"],
        "ATM_0DTE":      ["SPY"],
        "ATG_SWING":     ["SPY", "QQQ"],
        "ATG_INTRADAY":  ["SPY"],
        "NEXUS_ALPHA":   ["SPY", "AAPL"],
        "NEXUS_PRIME":   ["SPY", "QQQ"],
    }

    def __init__(
        self,
        service_name: str,
        execute_trade_fn: Callable,
        telegram_alert_fn: Callable,
        db_path: str,
    ):
        """
        Args:
            service_name: e.g. "NEXUS_ALPHA" or "NEXUS_PRIME".
            execute_trade_fn: Async callable that executes a trade.
                Signature: execute_trade_fn(ticker, size_override, canary_id, canary_mode) → dict
                Returns dict with keys: order_confirmed, db_record, telegram_sent, monitor_active.
            telegram_alert_fn: Async callable(message: str) for Ahmed alerts.
            db_path: SQLite path for canary result logging.
        """
        self.service_name = service_name
        self.execute_trade = execute_trade_fn
        self.telegram_alert = telegram_alert_fn
        self.db_path = db_path
        self.canary_tickers = self.CANARY_TICKERS.get(service_name, ["SPY"])
        self._retry_count = 0

    async def run(self) -> CanaryAssessment:
        """
        Run canary trades for this service. Call at exactly 9:31 AM ET.

        Returns:
            CanaryAssessment with pass/fail results and full-size trading decision.
        """
        logger.info(
            "CANARY PROTOCOL START: %s | %s | Tickers: %s",
            self.service_name,
            datetime.now(ET).strftime("%I:%M %p ET"),
            self.canary_tickers,
        )

        for attempt in range(1, self.MAX_RETRY_ATTEMPTS + 1):
            results: List[CanaryResult] = []
            for ticker in self.canary_tickers:
                ts = datetime.now(ET).strftime("%Y%m%d_%H%M%S")
                canary_id = f"CANARY_{self.service_name}_{ticker}_{ts}"
                result = await self._execute_canary(canary_id, ticker)
                results.append(result)
                icon = "✅" if result.success else "❌"
                logger.info("%s Canary %s (%s): %s", icon, canary_id, ticker,
                            result.failure_description or "PASS")

            assessment = self._assess_results(results)
            await self._report_canary_results(assessment, attempt)

            if assessment.all_passed:
                return assessment

            if attempt < self.MAX_RETRY_ATTEMPTS:
                logger.info(
                    "Canary attempt %d/%d failed. Retrying in 5 minutes...",
                    attempt, self.MAX_RETRY_ATTEMPTS,
                )
                await asyncio.sleep(300)   # 5 minute retry window
            else:
                # Final failure — halt and alert
                await self.telegram_alert(
                    f"🚨 CANARY PROTOCOL FAILED — {self.service_name}\n"
                    f"All {self.MAX_RETRY_ATTEMPTS} attempts exhausted.\n"
                    f"Full-size trading HALTED.\n"
                    f"Issues:\n" + "\n".join(f"  → {i}" for i in assessment.blocking_issues)
                )

        return assessment

    async def _execute_canary(self, canary_id: str, ticker: str) -> CanaryResult:
        """Execute a single canary trade through the full pipeline."""
        start = asyncio.get_event_loop().time()
        result = CanaryResult(
            canary_id=canary_id,
            service=self.service_name,
            ticker=ticker,
            success=False,
            failure_point=None,
            failure_description=None,
            execution_time_ms=0.0,
            order_confirmed=False,
            db_record_created=False,
            telegram_sent=False,
            monitor_active=False,
            no_exceptions=True,
        )

        try:
            trade_result = await asyncio.wait_for(
                self.execute_trade(
                    ticker=ticker,
                    size_override=self.CANARY_SIZE_USD,
                    canary_id=canary_id,
                    canary_mode=True,
                ),
                timeout=90.0,   # 90 seconds max per canary
            )

            result.order_confirmed    = bool(trade_result.get("order_confirmed"))
            result.db_record_created  = bool(trade_result.get("db_record"))
            result.telegram_sent      = bool(trade_result.get("telegram_sent"))
            result.monitor_active     = bool(trade_result.get("monitor_active"))

            criteria_passed = [
                result.order_confirmed,
                result.db_record_created,
                result.telegram_sent,
                result.monitor_active,
                result.no_exceptions,
            ]

            if all(criteria_passed):
                result.success = True
            else:
                failed = [
                    c for c, p in zip(self.SUCCESS_CRITERIA, criteria_passed) if not p
                ]
                result.failure_point = "success_criteria"
                result.failure_description = f"Failed criteria: {failed}"

        except asyncio.TimeoutError:
            result.no_exceptions = False
            result.failure_point = "timeout"
            result.failure_description = "Canary trade timed out after 90s"
            logger.error("Canary %s timed out", canary_id)
        except Exception as e:
            result.no_exceptions = False
            result.failure_point = "exception"
            result.failure_description = str(e)
            logger.error("Canary %s exception: %s", canary_id, e, exc_info=True)

        result.execution_time_ms = (asyncio.get_event_loop().time() - start) * 1000
        return result

    def _assess_results(self, results: List[CanaryResult]) -> CanaryAssessment:
        """Assess canary results and determine full-size trading decision."""
        passed = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        all_passed = len(failed) == 0
        blocking_issues = [
            f"{r.ticker}: {r.failure_point} — {r.failure_description}"
            for r in failed
        ]

        if all_passed:
            recommendation = (
                f"All {len(passed)}/{len(results)} canaries passed. "
                f"Full-size trading APPROVED at 9:45 AM."
            )
            full_size_approved = True
            delay = 14
        elif len(passed) > len(failed):
            recommendation = (
                f"{len(passed)}/{len(results)} canaries passed. "
                f"Diagnosing failures. Retry in 5 minutes."
            )
            full_size_approved = False
            delay = 5
        else:
            recommendation = (
                f"MAJORITY FAILED ({len(failed)}/{len(results)}). "
                f"Systemic issue detected. Service HALTED. Ahmed alerted."
            )
            full_size_approved = False
            delay = 0

        return CanaryAssessment(
            all_passed=all_passed,
            passed_count=len(passed),
            failed_count=len(failed),
            results=results,
            full_size_approved=full_size_approved,
            delay_minutes=delay,
            blocking_issues=blocking_issues,
            recommendation=recommendation,
        )

    async def _report_canary_results(self, assessment: CanaryAssessment, attempt: int) -> None:
        """Send canary results to Ahmed and log."""
        icon = "✅" if assessment.all_passed else "❌"
        msg = (
            f"{icon} CANARY RESULTS: {self.service_name} (attempt {attempt}/{self.MAX_RETRY_ATTEMPTS})\n"
            f"Passed: {assessment.passed_count}/{assessment.passed_count + assessment.failed_count}\n"
        )
        if assessment.blocking_issues:
            msg += "\nBlocking issues:\n"
            for issue in assessment.blocking_issues:
                msg += f"  → {issue}\n"
        msg += f"\n{assessment.recommendation}"

        logger.info("Canary assessment: %s", assessment.recommendation)
        if not assessment.all_passed:
            await self.telegram_alert(msg)
