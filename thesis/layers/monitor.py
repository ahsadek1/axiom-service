"""
RealTimeMonitor — Layer 3 of THESIS.

Monitors market data during market hours (9:30 AM - 4:00 PM ET) for
thesis-breaking events.  Silent when nothing significant occurs.

Checks every 5 minutes via APScheduler.  On event detection, fires
SOVEREIGN alert and logs to CHRONICLE thesis_events table.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

from models import EventType, MarketSnapshot, ThesisEvent
from clients.chronicle_client import ChronicleClient
from clients.sovereign_client import SovereignClient
from clients.telegram_client import TelegramClient

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)

# Event thresholds
_VIX_SPIKE_THRESHOLD = 5.0      # VIX points within 1 hour
_SPY_SHOCK_THRESHOLD = 1.0      # SPY % change in 30 minutes (absolute)
_HY_BLOWOUT_THRESHOLD = 50.0    # HY spread basis points intraday


class RealTimeMonitor:
    """Monitors for thesis-breaking events during market hours.

    Args:
        chronicle: CHRONICLE SQLite client for event persistence.
        sovereign: SOVEREIGN message bus client.
        telegram: Telegram client for critical escalations.
    """

    def __init__(
        self,
        chronicle: ChronicleClient,
        sovereign: SovereignClient,
        telegram: TelegramClient,
    ) -> None:
        """Initialise monitor with shared clients."""
        self.chronicle = chronicle
        self.sovereign = sovereign
        self.telegram = telegram

    def _is_market_hours(self) -> bool:
        """Check whether current ET time is within regular market hours.

        Returns:
            True if current time is between 9:30 AM and 4:00 PM ET.
        """
        now = datetime.now(tz=_ET).time()
        return _MARKET_OPEN <= now <= _MARKET_CLOSE

    async def check(self, snapshot: MarketSnapshot) -> Optional[ThesisEvent]:
        """Evaluate a market snapshot for thesis-breaking conditions.

        Checks (in priority order):
          1. VIX spike (>= 5 points in 1 hour)
          2. Major geopolitical shock (SPY moves >= 1% in 30 min)
          3. Credit spread blowout (HY spreads widen >= 50 bps)

        On detection:
          - Logs ThesisEvent to CHRONICLE
          - Alerts SOVEREIGN
          - Returns ThesisEvent for caller to act on

        Args:
            snapshot: Current market data snapshot.

        Returns:
            ThesisEvent if a thesis-breaking condition is detected, else None.
        """
        event = self._detect_event(snapshot)
        if event is None:
            return None

        logger.warning(
            "RealTimeMonitor: thesis-breaking event detected: %s", event.event_type
        )

        saved = self.chronicle.save_event(event)
        if not saved:
            logger.error(
                "RealTimeMonitor: failed to persist event %s to CHRONICLE",
                event.event_type,
            )

        await self.sovereign.send(
            f"THESIS ALERT: {event.event_type} detected. "
            f"{event.description} "
            f"Pausing new entries. Generating updated thesis.",
            event_type="event",
        )

        return event

    def _detect_event(self, snapshot: MarketSnapshot) -> Optional[ThesisEvent]:
        """Check snapshot against all configured thresholds.

        Args:
            snapshot: Current market snapshot.

        Returns:
            The first ThesisEvent detected, or None.
        """
        now_str = datetime.now(tz=_ET).isoformat()

        # 1. VIX spike
        if snapshot.vix_change_1h is not None:
            if snapshot.vix_change_1h >= _VIX_SPIKE_THRESHOLD:
                return ThesisEvent(
                    event_type=EventType.VIX_SPIKE.value,
                    detected_at=now_str,
                    description=(
                        f"VIX rose {snapshot.vix_change_1h:.1f} points in 1 hour "
                        f"(threshold: {_VIX_SPIKE_THRESHOLD}). "
                        f"Current VIX: {snapshot.vix_level}."
                    ),
                    affected_sectors=["ALL"],
                    thesis_paused=True,
                )

        # 2. Major geopolitical / macro shock (SPY 30-min move)
        if snapshot.spy_change_30m is not None:
            if abs(snapshot.spy_change_30m) >= _SPY_SHOCK_THRESHOLD:
                return ThesisEvent(
                    event_type=EventType.MAJOR_GEOPOLITICAL_SHOCK.value,
                    detected_at=now_str,
                    description=(
                        f"SPY moved {snapshot.spy_change_30m:+.2f}% in 30 minutes "
                        f"(threshold: ±{_SPY_SHOCK_THRESHOLD}%)."
                    ),
                    affected_sectors=["ALL"],
                    thesis_paused=True,
                )

        # 3. Credit spread blowout
        if snapshot.hy_spread_change is not None:
            if snapshot.hy_spread_change >= _HY_BLOWOUT_THRESHOLD:
                return ThesisEvent(
                    event_type=EventType.CREDIT_SPREAD_BLOW_OUT.value,
                    detected_at=now_str,
                    description=(
                        f"HY credit spreads widened {snapshot.hy_spread_change:.0f} bps "
                        f"intraday (threshold: {_HY_BLOWOUT_THRESHOLD} bps)."
                    ),
                    affected_sectors=["HIGH_YIELD", "CREDIT", "FINANCIALS"],
                    thesis_paused=True,
                )

        return None
