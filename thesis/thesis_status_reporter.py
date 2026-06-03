"""
THESIS Status Reporter — Automated reporting to Ahmed.

Sends status updates to Ahmed's Telegram DM at:
  - 12:15 PM EDT: Mid-session update (trades executed, positions, errors, health)
  - 4:15 PM EDT: Post-close summary (total trades, P&L, failures, tomorrow readiness)

Uses Axiom bot (@Axiom15Bot) to send messages to Ahmed (8573754783).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] thesis_status: %(message)s",
    handlers=[
        logging.FileHandler("/Users/ahmedsadek/nexus/logs/thesis-status.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("thesis_status")

_ET = ZoneInfo("America/New_York")


class ThesisStatusReporter:
    """
    Reports trading status to Ahmed at scheduled intervals.

    Collects:
      - Trades executed since open
      - Current open positions
      - Any errors/flags encountered
      - Health status (CHRONICLE, APScheduler, Alpaca, etc.)
      - Tomorrow readiness assessment
    """

    def __init__(
        self,
        chronicle_db_path: str = "/Users/ahmedsadek/nexus/data/chronicle.db",
        alpaca_key: str = "",
        alpaca_secret: str = "",
        thesis_service_url: str = "http://localhost:8060",
        telegram_token: str = "",
        ahmed_telegram_id: str = "8573754783",
    ):
        self.chronicle_db_path = chronicle_db_path
        self.alpaca_key = alpaca_key
        self.alpaca_secret = alpaca_secret
        self.thesis_service_url = thesis_service_url
        self.telegram_token = telegram_token
        self.ahmed_telegram_id = ahmed_telegram_id

    async def send_mid_session_update(self):
        """
        Send mid-session update at 12:15 PM EDT.

        Reports:
          - Trades executed so far (symbol, direction, entry price, P&L if closed)
          - Current open positions
          - Any errors/flags
          - Health status
        """
        logger.info("Sending mid-session update...")

        try:
            trades = await self._get_trades_today()
            positions = await self._get_open_positions()
            errors = await self._get_recent_errors()
            health = await self._get_health_status()

            message = self._format_mid_session_message(trades, positions, errors, health)

            await self._send_telegram_message(message)
            logger.info("Mid-session update sent successfully.")

        except Exception as e:
            logger.error(f"Failed to send mid-session update: {str(e)}", exc_info=True)
            # Send error notification
            await self._send_telegram_message(f"❌ Mid-session update failed: {str(e)}")

    async def send_post_close_summary(self):
        """
        Send post-close summary at 4:15 PM EDT.

        Reports:
          - Total trades executed today
          - Total P&L if applicable
          - Any failures encountered
          - Tomorrow readiness status
          - Recommendations
        """
        logger.info("Sending post-close summary...")

        try:
            trades = await self._get_trades_today()
            total_pnl = await self._get_daily_pnl()
            failures = await self._get_daily_failures()
            health = await self._get_health_status()
            thesis = await self._get_current_thesis()

            message = self._format_post_close_message(
                trades, total_pnl, failures, health, thesis
            )

            await self._send_telegram_message(message)
            logger.info("Post-close summary sent successfully.")

        except Exception as e:
            logger.error(f"Failed to send post-close summary: {str(e)}", exc_info=True)
            # Send error notification
            await self._send_telegram_message(f"❌ Post-close summary failed: {str(e)}")

    async def _get_trades_today(self) -> list[dict]:
        """Fetch trades executed today from CHRONICLE."""
        try:
            conn = sqlite3.connect(self.chronicle_db_path, timeout=5)
            cursor = conn.cursor()

            today = datetime.now(tz=_ET).date().isoformat()

            cursor.execute(
                """
                SELECT symbol, direction, entry_price, exit_price, entry_time, exit_time, pnl
                FROM trades
                WHERE DATE(entry_time) = ?
                ORDER BY entry_time DESC
                """,
                (today,),
            )

            trades = []
            for row in cursor.fetchall():
                trades.append({
                    "symbol": row[0],
                    "direction": row[1],
                    "entry_price": row[2],
                    "exit_price": row[3],
                    "entry_time": row[4],
                    "exit_time": row[5],
                    "pnl": row[6],
                })

            conn.close()
            return trades

        except Exception as e:
            logger.warning(f"Failed to fetch trades: {str(e)}")
            return []

    async def _get_open_positions(self) -> list[dict]:
        """Fetch open positions from Alpaca."""
        try:
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

            positions = response.json()

            result = []
            for pos in positions:
                result.append({
                    "symbol": pos.get("symbol"),
                    "qty": pos.get("qty"),
                    "avg_fill_price": pos.get("avg_fill_price"),
                    "current_price": pos.get("current_price"),
                    "unrealized_pl": pos.get("unrealized_pl"),
                    "unrealized_plpc": pos.get("unrealized_plpc"),
                })

            return result

        except Exception as e:
            logger.warning(f"Failed to fetch positions: {str(e)}")
            return []

    async def _get_recent_errors(self) -> list[str]:
        """Fetch recent errors from CHRONICLE event_log."""
        try:
            conn = sqlite3.connect(self.chronicle_db_path, timeout=5)
            cursor = conn.cursor()

            # Get errors from the last 4 hours
            cursor.execute(
                """
                SELECT message
                FROM event_log
                WHERE level = 'ERROR'
                AND timestamp > datetime('now', '-4 hours')
                ORDER BY timestamp DESC
                LIMIT 5
                """,
            )

            errors = [row[0] for row in cursor.fetchall()]
            conn.close()

            return errors

        except Exception as e:
            logger.warning(f"Failed to fetch errors: {str(e)}")
            return []

    async def _get_health_status(self) -> dict:
        """Get current health status from Thesis service."""
        try:
            response = requests.get(
                f"{self.thesis_service_url}/thesis/health",
                timeout=5,
            )
            response.raise_for_status()

            health = response.json()

            return {
                "chronicle_ok": health.get("chronicle_ok", False),
                "scheduler_ok": health.get("scheduler_ok", False),
                "oracle_ok": health.get("oracle_ok", False),
                "message": health.get("message", "Unknown"),
            }

        except Exception as e:
            logger.warning(f"Failed to fetch health status: {str(e)}")
            return {
                "chronicle_ok": False,
                "scheduler_ok": False,
                "oracle_ok": False,
                "message": f"Error: {str(e)}",
            }

    async def _get_current_thesis(self) -> dict:
        """Get current thesis context."""
        try:
            response = requests.get(
                f"{self.thesis_service_url}/thesis/current-context",
                timeout=5,
            )
            response.raise_for_status()

            context = response.json()

            return {
                "regime": context.get("regime", "UNKNOWN"),
                "vix_level": context.get("vix_level", 0),
                "spy_level": context.get("spy_level", 0),
                "market_regime_health": context.get("regime_health", "UNKNOWN"),
                "sentiment": context.get("sentiment", "NEUTRAL"),
            }

        except Exception as e:
            logger.warning(f"Failed to fetch thesis: {str(e)}")
            return {}

    async def _get_daily_pnl(self) -> dict:
        """Calculate daily P&L from CHRONICLE."""
        try:
            conn = sqlite3.connect(self.chronicle_db_path, timeout=5)
            cursor = conn.cursor()

            today = datetime.now(tz=_ET).date().isoformat()

            # Get P&L from closed trades
            cursor.execute(
                """
                SELECT SUM(pnl), COUNT(*), MAX(pnl), MIN(pnl)
                FROM trades
                WHERE DATE(exit_time) = ?
                AND pnl IS NOT NULL
                """,
                (today,),
            )

            row = cursor.fetchone()
            total_pnl = row[0] or 0.0
            trade_count = row[1] or 0
            max_pnl = row[2] or 0.0
            min_pnl = row[3] or 0.0

            conn.close()

            return {
                "total_pnl": total_pnl,
                "trade_count": trade_count,
                "max_pnl": max_pnl,
                "min_pnl": min_pnl,
            }

        except Exception as e:
            logger.warning(f"Failed to calculate daily P&L: {str(e)}")
            return {"total_pnl": 0, "trade_count": 0}

    async def _get_daily_failures(self) -> list[str]:
        """Get summary of daily failures."""
        try:
            conn = sqlite3.connect(self.chronicle_db_path, timeout=5)
            cursor = conn.cursor()

            today = datetime.now(tz=_ET).date().isoformat()

            cursor.execute(
                """
                SELECT DISTINCT message
                FROM event_log
                WHERE level IN ('ERROR', 'CRITICAL')
                AND DATE(timestamp) = ?
                LIMIT 5
                """,
                (today,),
            )

            failures = [row[0] for row in cursor.fetchall()]
            conn.close()

            return failures

        except Exception as e:
            logger.warning(f"Failed to fetch daily failures: {str(e)}")
            return []

    def _format_mid_session_message(
        self, trades: list[dict], positions: list[dict], errors: list[str], health: dict
    ) -> str:
        """Format mid-session update message."""
        timestamp = datetime.now(tz=_ET).strftime("%I:%M %p ET")

        message = f"📊 THESIS MID-SESSION UPDATE\n{timestamp}\n\n"

        # Trades executed
        message += f"📈 **Trades Executed:** {len(trades)}\n"
        if trades:
            for trade in trades[:5]:  # Show last 5
                pnl_str = f"P&L: {trade['pnl']:+.2f}" if trade["pnl"] else "Open"
                message += (
                    f"  • {trade['symbol']} {trade['direction']} @ ${trade['entry_price']:.2f} | {pnl_str}\n"
                )

        # Open positions
        message += f"\n💼 **Open Positions:** {len(positions)}\n"
        if positions:
            total_unrealized = sum(float(p.get("unrealized_pl", 0)) for p in positions)
            message += f"  • Total Unrealized P&L: {total_unrealized:+.2f}\n"
            for pos in positions[:5]:
                message += (
                    f"  • {pos['symbol']} {pos['qty']} @ ${pos['avg_fill_price']} "
                    f"({pos['unrealized_plpc']:+.2f}%)\n"
                )

        # Errors
        if errors:
            message += f"\n⚠️ **Errors (Last 4h):** {len(errors)}\n"
            for error in errors[:3]:
                message += f"  • {error[:80]}...\n"

        # Health
        health_emoji = "✅" if health.get("chronicle_ok") else "🔴"
        message += f"\n{health_emoji} **System Health:**\n"
        message += f"  • CHRONICLE: {'✅' if health.get('chronicle_ok') else '❌'}\n"
        message += f"  • APScheduler: {'✅' if health.get('scheduler_ok') else '❌'}\n"
        message += f"  • Oracle: {'✅' if health.get('oracle_ok') else '❌'}\n"

        return message

    def _format_post_close_message(
        self,
        trades: list[dict],
        pnl: dict,
        failures: list[str],
        health: dict,
        thesis: dict,
    ) -> str:
        """Format post-close summary message."""
        timestamp = datetime.now(tz=_ET).strftime("%I:%M %p ET")

        message = f"📊 THESIS POST-CLOSE SUMMARY\n{timestamp}\n\n"

        # Daily stats
        message += "**Daily Results:**\n"
        total_pnl = pnl.get("total_pnl", 0)
        trade_count = pnl.get("trade_count", 0)
        message += f"  • Trades: {trade_count}\n"
        message += f"  • Total P&L: {total_pnl:+.2f}\n"

        if trade_count > 0:
            avg_pnl = total_pnl / trade_count
            message += f"  • Avg P&L/Trade: {avg_pnl:+.2f}\n"

        # Best and worst
        if pnl.get("max_pnl"):
            message += f"  • Best Trade: {pnl['max_pnl']:+.2f}\n"
        if pnl.get("min_pnl"):
            message += f"  • Worst Trade: {pnl['min_pnl']:+.2f}\n"

        # Thesis context
        if thesis:
            message += f"\n**Thesis Context:**\n"
            message += f"  • Regime: {thesis.get('regime', 'UNKNOWN')}\n"
            message += f"  • VIX: {thesis.get('vix_level', 0):.1f}\n"
            message += f"  • Market Health: {thesis.get('market_regime_health', 'UNKNOWN')}\n"

        # Failures
        if failures:
            message += f"\n⚠️ **Failures Today:**\n"
            for failure in failures[:3]:
                message += f"  • {failure[:80]}...\n"

        # Health
        health_emoji = "✅" if health.get("chronicle_ok") else "🔴"
        message += f"\n{health_emoji} **System Health:**\n"
        message += f"  • CHRONICLE: {'✅' if health.get('chronicle_ok') else '❌'}\n"
        message += f"  • APScheduler: {'✅' if health.get('scheduler_ok') else '❌'}\n"

        # Tomorrow readiness
        message += f"\n**Tomorrow Readiness:** "
        all_ok = health.get("chronicle_ok") and health.get("scheduler_ok")
        message += ("✅ All systems ready\n" if all_ok else "⚠️ Some systems degraded\n")

        return message

    async def _send_telegram_message(self, message: str):
        """Send message to Ahmed's Telegram DM via Axiom bot."""
        if not self.telegram_token:
            logger.warning("Telegram token not configured. Skipping message send.")
            return

        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                json={
                    "chat_id": self.ahmed_telegram_id,
                    "text": message,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )

            response.raise_for_status()
            logger.info(f"Message sent to Ahmed (ID: {self.ahmed_telegram_id})")

        except Exception as e:
            logger.error(f"Failed to send Telegram message: {str(e)}")


# Standalone execution for testing
async def main():
    """Test the status reporter."""
    import os

    alpaca_key = os.getenv("ALPACA_KEY", "PKPGM3BRNYPGCF5Z56IAUZCZJL")
    alpaca_secret = os.getenv("ALPACA_SECRET", "5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")

    reporter = ThesisStatusReporter(
        alpaca_key=alpaca_key,
        alpaca_secret=alpaca_secret,
        telegram_token=telegram_token,
    )

    # Test mid-session update
    print("Testing mid-session update...")
    await reporter.send_mid_session_update()

    # Test post-close summary
    print("\nTesting post-close summary...")
    await reporter.send_post_close_summary()


if __name__ == "__main__":
    asyncio.run(main())
