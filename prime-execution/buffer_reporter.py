"""
buffer_reporter.py — Prime Execution → Prime Buffer Reporter

Reports closed trade outcomes back to Prime Buffer for circuit breaker updates.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("prime_exec.buffer_reporter")


class BufferReporter:
    """Reports outcomes to Prime Buffer."""

    def __init__(self, buffer_url: str, auth_secret: str) -> None:
        self.buffer_url  = buffer_url.rstrip("/")
        self.auth_secret = auth_secret

    def report_outcome(
        self,
        ticker:  str,
        won:     bool,
        pnl_pct: float,
        pathway: Optional[str] = None,
    ) -> bool:
        """
        Report a closed Prime trade to the Prime Buffer.

        Args:
            ticker:  Stock ticker symbol.
            won:     True if profitable.
            pnl_pct: P&L as decimal.
            pathway: Trade pathway (P1/P2/P3).

        Returns:
            True if reported successfully.
        """
        try:
            resp = requests.post(
                f"{self.buffer_url}/trade-outcome",
                json    = {"ticker": ticker, "won": won, "pnl_pct": pnl_pct, "pathway": pathway},
                headers = {"X-Nexus-Prime-Secret": self.auth_secret},
                timeout = 8,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.warning("Prime buffer report failed for %s: %s", ticker, e)
            return False
