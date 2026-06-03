"""
THESIS Trading Ability Validator.

Daily pre-market validation (9:45 AM EDT):
  1. Execute a mock trade (buy 1 share, verify order submitted)
  2. Verify position appears in Alpaca paper account
  3. Close the position
  4. Report pass/fail to Ahmed

If validation fails, escalates immediately to Axiom + Cipher.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] thesis_trading_validator: %(message)s",
    handlers=[
        logging.FileHandler("/Users/ahmedsadek/nexus/logs/thesis-trading-validator.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("thesis_trading_validator")

_ET = ZoneInfo("America/New_York")


class ThesisTradingValidator:
    """
    Pre-market trading ability validator.

    Verifies:
      1. Alpaca API connectivity
      2. Order submission capability
      3. Position tracking
      4. Position closure
    """

    def __init__(
        self,
        alpaca_key: str = "",
        alpaca_secret: str = "",
        chronicle_db_path: str = "/Users/ahmedsadek/nexus/data/chronicle.db",
        telegram_token: str = "",
        ahmed_telegram_id: str = "8573754783",
    ):
        self.alpaca_key = alpaca_key
        self.alpaca_secret = alpaca_secret
        self.chronicle_db_path = chronicle_db_path
        self.telegram_token = telegram_token
        self.ahmed_telegram_id = ahmed_telegram_id

        self.headers = {
            "APCA-API-KEY-ID": alpaca_key,
            "APCA-API-SECRET-KEY": alpaca_secret,
            "Content-Type": "application/json",
        }

    async def run_daily_validation(self) -> dict:
        """
        Run full pre-market validation.

        Steps:
          1. Check account access
          2. Submit test order (buy 1 SPY)
          3. Verify position appears
          4. Close position
          5. Verify closed
          6. Report result

        Returns:
            {
                "status": "pass" | "fail",
                "steps": [
                    {"name": "...", "status": "✅" | "❌", "message": "..."}
                ],
                "timestamp": "...",
            }
        """
        logger.info("Starting daily trading ability validation...")
        
        # Import asyncio for sleep
        import asyncio

        results = {
            "status": "pass",
            "steps": [],
            "timestamp": datetime.now(tz=_ET).isoformat(),
        }

        # Step 1: Check account access
        step1 = await self._check_account_access()
        results["steps"].append(step1)
        if step1["status"] != "✅":
            results["status"] = "fail"
            await self._report_validation_result(results)
            return results
        
        # Cool-off after account check
        await asyncio.sleep(0.5)

        # Step 2: Submit test order
        step2 = await self._submit_test_order()
        results["steps"].append(step2)
        if step2["status"] != "✅":
            results["status"] = "fail"
            await self._report_validation_result(results)
            return results

        order_id = step2.get("order_id")
        
        # Wait for order to fill before verifying position
        # Market may be processing the order, give it 2 seconds
        await asyncio.sleep(2.0)

        # Step 3: Verify position appears
        step3 = await self._verify_position(order_id)
        results["steps"].append(step3)
        if step3["status"] != "✅":
            results["status"] = "fail"
            await self._cleanup_test_position()
            await self._report_validation_result(results)
            return results
        
        # Cool-off before closing
        await asyncio.sleep(0.5)

        # Step 4: Close position
        step4 = await self._close_test_position()
        results["steps"].append(step4)
        if step4["status"] != "✅":
            results["status"] = "fail"
            await self._cleanup_test_position()
            await self._report_validation_result(results)
            return results
        
        # Wait for close to process
        await asyncio.sleep(1.0)

        # Step 5: Verify closed
        step5 = await self._verify_position_closed()
        results["steps"].append(step5)
        if step5["status"] != "✅":
            results["status"] = "fail"
            await self._cleanup_test_position()
            await self._report_validation_result(results)
            return results

        # All passed
        logger.info("Trading ability validation PASSED")
        await self._report_validation_result(results)

        return results

    async def _check_account_access(self) -> dict:
        """Check Alpaca account access."""
        try:
            response = requests.get(
                "https://paper-api.alpaca.markets/v2/account",
                headers=self.headers,
                timeout=5,
            )
            response.raise_for_status()

            account = response.json()
            equity = float(account.get("equity", 0))

            if equity <= 0:
                return {
                    "name": "Account Access",
                    "status": "❌",
                    "message": f"Invalid account state: equity=${equity}",
                }

            return {
                "name": "Account Access",
                "status": "✅",
                "message": f"Account accessible. Equity: ${equity:,.2f}",
            }

        except Exception as e:
            return {
                "name": "Account Access",
                "status": "❌",
                "message": f"Failed to access account: {str(e)[:100]}",
            }

    async def _submit_test_order(self) -> dict:
        """Submit a test order (buy 1 SPY) with retry on 403."""
        try:
            import asyncio
            
            max_retries = 3
            retry_delay = 2.0  # 2 seconds between retries for rate limits
            
            for attempt in range(max_retries):
                payload = {
                    "symbol": "SPY",
                    "qty": 1,
                    "side": "buy",
                    "type": "market",
                    "time_in_force": "day",
                }

                response = requests.post(
                    "https://paper-api.alpaca.markets/v2/orders",
                    headers=self.headers,
                    json=payload,
                    timeout=5,
                )
                
                if response.status_code == 403:
                    # Rate limited, retry with backoff
                    if attempt < max_retries - 1:
                        logger.warning(f"Rate limited (403), retrying in {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        return {
                            "name": "Submit Test Order (SPY)",
                            "status": "❌",
                            "message": f"Rate limited after {max_retries} retries",
                        }
                
                response.raise_for_status()

                order = response.json()
                order_id = order.get("id")
                logger.info(f"Order submitted: {order_id}")

                return {
                    "name": "Submit Test Order (SPY)",
                    "status": "✅",
                    "message": f"Order submitted: {order_id}",
                    "order_id": order_id,
                }

        except Exception as e:
            logger.error(f"Failed to submit order: {str(e)}")
            return {
                "name": "Submit Test Order (SPY)",
                "status": "❌",
                "message": f"Failed to submit order: {str(e)[:100]}",
            }

    async def _verify_position(self, order_id: Optional[str] = None) -> dict:
        """Verify position appears in account with retry logic."""
        try:
            import asyncio
            
            max_retries = 10
            retry_delay = 0.5
            
            for attempt in range(max_retries):
                try:
                    response = requests.get(
                        "https://paper-api.alpaca.markets/v2/positions/SPY",
                        headers=self.headers,
                        timeout=5,
                    )
                    
                    logger.debug(f"Position check attempt {attempt + 1}: status={response.status_code}")

                    if response.status_code == 200:
                        position = response.json()
                        qty = float(position.get("qty", 0))

                        if qty > 0:
                            logger.info(f"Position found after {attempt + 1} attempt(s): qty={qty}")
                            return {
                                "name": "Verify Position",
                                "status": "✅",
                                "message": f"Position verified: {qty} SPY",
                            }
                    elif response.status_code == 404:
                        logger.debug(f"Position not found (404), retrying...")
                    
                except Exception as e:
                    logger.debug(f"Exception during position check (attempt {attempt+1}): {str(e)}")
                
                # Position not ready yet, retry
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
            
            # After all retries, position still not found
            logger.error(f"Position not found after {max_retries} attempts (waited {max_retries * retry_delay}s)")
            return {
                "name": "Verify Position",
                "status": "❌",
                "message": f"Position not found after {max_retries} retries",
            }

        except Exception as e:
            logger.error(f"Exception verifying position: {str(e)}")
            return {
                "name": "Verify Position",
                "status": "❌",
                "message": f"Failed to verify position: {str(e)[:100]}",
            }

    async def _close_test_position(self) -> dict:
        """Close the test position with retry on 403."""
        try:
            import asyncio
            
            max_retries = 3
            retry_delay = 2.0
            
            for attempt in range(max_retries):
                payload = {
                    "qty": 1,
                }

                response = requests.delete(
                    "https://paper-api.alpaca.markets/v2/positions/SPY",
                    headers=self.headers,
                    json=payload,
                    timeout=5,
                )
                
                if response.status_code == 403:
                    if attempt < max_retries - 1:
                        logger.warning(f"Rate limited closing position (403), retrying...")
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        return {
                            "name": "Close Test Position",
                            "status": "❌",
                            "message": f"Rate limited after {max_retries} retries",
                        }
                
                response.raise_for_status()

                close_order = response.json()
                close_order_id = close_order.get("id")
                logger.info(f"Close order submitted: {close_order_id}")

                return {
                    "name": "Close Test Position",
                    "status": "✅",
                    "message": f"Close order submitted: {close_order_id}",
                    "close_order_id": close_order_id,
                }

        except Exception as e:
            logger.error(f"Failed to close position: {str(e)}")
            return {
                "name": "Close Test Position",
                "status": "❌",
                "message": f"Failed to close position: {str(e)[:100]}",
            }

    async def _verify_position_closed(self) -> dict:
        """Verify position is fully closed with retry logic.
        
        Retry logic handles race condition where close order is submitted
        but not yet filled by Alpaca (especially before market open).
        """
        try:
            import asyncio
            
            # Retry parameters
            max_retries = 5
            retry_delay = 0.5  # 500ms between retries
            
            for attempt in range(max_retries):
                # Wait before checking
                await asyncio.sleep(retry_delay if attempt > 0 else 1.0)
                
                response = requests.get(
                    "https://paper-api.alpaca.markets/v2/positions/SPY",
                    headers=self.headers,
                    timeout=5,
                )

                if response.status_code == 404:
                    # Position fully closed (removed from account)
                    logger.info(f"Position closed after {attempt + 1} attempt(s)")
                    return {
                        "name": "Verify Position Closed",
                        "status": "✅",
                        "message": f"Position confirmed closed (attempt {attempt + 1}/{max_retries})",
                    }

                if response.status_code == 200:
                    position = response.json()
                    qty = float(position.get("qty", 0))

                    if qty == 0:
                        # Position exists but qty is zero
                        logger.info(f"Position qty is 0 after {attempt + 1} attempt(s)")
                        return {
                            "name": "Verify Position Closed",
                            "status": "✅",
                            "message": f"Position confirmed closed (qty=0, attempt {attempt + 1}/{max_retries})",
                        }
                    
                    # Still open, retry if we have attempts left
                    if attempt < max_retries - 1:
                        logger.debug(f"Position still open (qty={qty}), retrying in {retry_delay}s...")
                        continue
                    
                    # Last attempt and still open
                    logger.error(f"Position still open after {max_retries} attempts: qty={qty}")
                    return {
                        "name": "Verify Position Closed",
                        "status": "❌",
                        "message": f"Position still open after {max_retries} retries: qty={qty}",
                    }
            
            # Should not reach here
            return {
                "name": "Verify Position Closed",
                "status": "❌",
                "message": "Verification loop exhausted without clear result",
            }

        except Exception as e:
            logger.error(f"Exception during position closure verification: {str(e)}")
            return {
                "name": "Verify Position Closed",
                "status": "❌",
                "message": f"Failed to verify closure: {str(e)[:100]}",
            }

    async def _cleanup_test_position(self):
        """Cleanup any remaining test position with retry."""
        try:
            import asyncio
            
            logger.warning("Attempting to cleanup test position...")

            response = requests.get(
                "https://paper-api.alpaca.markets/v2/positions/SPY",
                headers=self.headers,
                timeout=5,
            )

            if response.status_code == 200:
                # Position exists, close it with retry
                max_retries = 3
                for attempt in range(max_retries):
                    delete_response = requests.delete(
                        "https://paper-api.alpaca.markets/v2/positions/SPY",
                        headers=self.headers,
                        timeout=5,
                    )
                    
                    if delete_response.status_code == 403 and attempt < max_retries - 1:
                        await asyncio.sleep(2.0)
                        continue
                    
                    if delete_response.status_code == 200:
                        logger.info("Test position cleaned up")
                        break

        except Exception as e:
            logger.error(f"Cleanup failed: {str(e)}")

    async def _report_validation_result(self, results: dict):
        """Report validation result to Ahmed via Telegram."""
        message = self._format_validation_message(results)
        logger.info(f"Validation result: {results['status'].upper()}")

        try:
            if self.telegram_token:
                requests.post(
                    f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                    json={
                        "chat_id": self.ahmed_telegram_id,
                        "text": message,
                        "parse_mode": "Markdown",
                    },
                    timeout=10,
                )
        except Exception as e:
            logger.error(f"Failed to send Telegram report: {str(e)}")

        # Log to CHRONICLE
        try:
            self._log_validation_to_chronicle(results)
        except Exception as e:
            logger.error(f"Failed to log to CHRONICLE: {str(e)}")

    def _format_validation_message(self, results: dict) -> str:
        """Format validation result message for Telegram."""
        status_emoji = "✅" if results["status"] == "pass" else "❌"
        timestamp = datetime.now(tz=_ET).strftime("%I:%M %p ET")

        message = f"{status_emoji} **THESIS TRADING VALIDATOR**\n{timestamp}\n\n"
        message += f"**Status:** {results['status'].upper()}\n\n"
        message += "**Steps:**\n"

        for step in results["steps"]:
            message += f"{step['status']} {step['name']}\n"
            message += f"   {step['message']}\n\n"

        if results["status"] == "fail":
            message += (
                "⚠️ **ACTION REQUIRED:** Trading ability validation failed. "
                "Manual review needed before market open.\n"
            )

        return message

    def _log_validation_to_chronicle(self, results: dict):
        """Log validation result to CHRONICLE."""
        try:
            conn = sqlite3.connect(self.chronicle_db_path, timeout=5)
            cursor = conn.cursor()

            # Check if event_log table exists
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='event_log'"
            )
            if not cursor.fetchone():
                logger.warning("event_log table does not exist, skipping CHRONICLE log")
                conn.close()
                return

            status = results["status"]
            steps_json = json.dumps(results["steps"])

            cursor.execute(
                """
                INSERT INTO event_log (timestamp, level, component, message, details)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(tz=_ET).isoformat(),
                    "INFO" if status == "pass" else "ERROR",
                    "trading_validator",
                    f"Daily trading ability validation: {status.upper()}",
                    steps_json,
                ),
            )

            conn.commit()
            conn.close()
            logger.info("Validation logged to CHRONICLE")

        except Exception as e:
            logger.error(f"Failed to log to CHRONICLE: {str(e)}")


# Standalone execution for testing
async def main():
    """Test the trading validator."""
    import os

    alpaca_key = os.getenv("ALPACA_KEY", "PKPGM3BRNYPGCF5Z56IAUZCZJL")
    alpaca_secret = os.getenv("ALPACA_SECRET", "5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")

    validator = ThesisTradingValidator(
        alpaca_key=alpaca_key,
        alpaca_secret=alpaca_secret,
        telegram_token=telegram_token,
    )

    result = await validator.run_daily_validation()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
