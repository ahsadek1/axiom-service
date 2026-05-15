"""
NNS WATCHDOG — Agent Signal Polling Loop
shared/watchdog.py

One-liner usage:
    from shared.watchdog import Watchdog
    w = Watchdog("cipher")
    w.register("restart", lambda sig: restart_service())
    w.start()
"""

import threading
import time
import logging
import requests
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


class Watchdog:
    """
    Polls the NNS signal bus for signals addressed to this agent.
    Dispatches to registered handlers. ACKs on success, escalates on error.
    Runs in a daemon thread — never blocks agent startup.
    """

    def __init__(
        self,
        agent_name: str,
        bus_host: str = "192.168.1.141",
        bus_port: int = 9999,
        poll_interval: int = 5,
    ) -> None:
        """
        Args:
            agent_name:     Name of this agent (must match bus 'to' field)
            bus_host:       NNS signal bus host
            bus_port:       NNS signal bus port
            poll_interval:  Seconds between polls (default 5)
        """
        self.agent_name = agent_name
        self.bus_url = f"http://{bus_host}:{bus_port}"
        self.poll_interval = poll_interval
        self._handlers: Dict[str, Callable[[dict], None]] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, action_type: str, handler: Callable[[dict], None]) -> None:
        """
        Register a handler for a specific action_type.
        Handler signature: handler(signal: dict) -> None

        Args:
            action_type:  Signal action_type string to match
            handler:      Callable that receives the full signal dict
        """
        self._handlers[action_type] = handler
        logger.info(f"[WATCHDOG:{self.agent_name}] Registered handler for action_type={action_type!r}")

    def start(self) -> None:
        """Start the polling daemon thread. Non-blocking."""
        if self._thread and self._thread.is_alive():
            logger.warning(f"[WATCHDOG:{self.agent_name}] Already running — ignoring start()")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"watchdog-{self.agent_name}",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"[WATCHDOG:{self.agent_name}] Started — polling {self.bus_url} every {self.poll_interval}s")

    def stop(self) -> None:
        """Signal the loop to exit. Returns within one poll cycle."""
        self._stop_event.set()
        logger.info(f"[WATCHDOG:{self.agent_name}] Stop requested")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main polling loop. Runs until stop() is called."""
        backoff = self.poll_interval  # normal interval
        BACKOFF_MAX = 30             # max backoff on bus failure

        while not self._stop_event.is_set():
            try:
                signals = self._poll()
                backoff = self.poll_interval  # reset on success
                for signal in signals:
                    self._dispatch(signal)
            except _BusUnavailable as e:
                logger.warning(f"[WATCHDOG:{self.agent_name}] Bus unavailable: {e} — backing off {BACKOFF_MAX}s")
                backoff = BACKOFF_MAX
            except Exception as e:
                logger.error(f"[WATCHDOG:{self.agent_name}] Unexpected loop error: {e}", exc_info=True)

            self._stop_event.wait(timeout=backoff)

    def _poll(self) -> list:
        """
        Fetch pending signals for this agent from /recv.
        Returns list of signal dicts. Raises _BusUnavailable on network error.
        """
        try:
            resp = requests.get(
                f"{self.bus_url}/recv",
                params={"agent": self.agent_name},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            signals = data if isinstance(data, list) else data.get("signals", [])
            if signals:
                logger.debug(f"[WATCHDOG:{self.agent_name}] {len(signals)} pending signal(s)")
            return signals
        except requests.exceptions.RequestException as e:
            raise _BusUnavailable(str(e)) from e

    def _dispatch(self, signal: dict) -> None:
        """
        Dispatch a single signal to its registered handler.
        ACKs as 'received' on success, 'escalated' on handler error.
        No handler registered → ACK as 'received' with WARNING log.
        """
        signal_id = signal.get("signal_id") or signal.get("id")
        action_type = signal.get("action_type", "")
        priority = signal.get("priority", "P2")

        if not signal_id:
            logger.error(f"[WATCHDOG:{self.agent_name}] Signal missing signal_id — skipping: {signal}")
            return

        handler = self._handlers.get(action_type)

        if handler is None:
            logger.warning(
                f"[WATCHDOG:{self.agent_name}] No handler for action_type={action_type!r} "
                f"(signal_id={signal_id}, priority={priority}) — ACKing as received"
            )
            self._ack(signal_id, "received", f"No handler registered for action_type={action_type!r}")
            return

        try:
            logger.info(
                f"[WATCHDOG:{self.agent_name}] Dispatching signal_id={signal_id} "
                f"action_type={action_type!r} priority={priority}"
            )
            handler(signal)
            self._ack(signal_id, "received")
            logger.info(f"[WATCHDOG:{self.agent_name}] ACKed signal_id={signal_id} as received")
        except Exception as e:
            logger.error(
                f"[WATCHDOG:{self.agent_name}] Handler error for signal_id={signal_id}: {e}",
                exc_info=True,
            )
            self._ack(signal_id, "escalated", f"Handler raised: {e}")

    def _ack(self, signal_id: str, status: str, note: str = "") -> None:
        """
        ACK a signal on the bus. Idempotent — re-ACKing already-ACKed signal is safe.

        Args:
            signal_id:  Signal to ACK
            status:     'received' | 'resolved' | 'escalated' | 'rejected'
            note:       Optional human-readable note stored with ACK
        """
        payload: dict = {
            "signal_id": signal_id,
            "agent": self.agent_name,
            "status": status,
        }
        if note:
            payload["note"] = note

        try:
            resp = requests.post(f"{self.bus_url}/ack", json=payload, timeout=5)
            if resp.status_code not in (200, 409):  # 409 = already ACKed (idempotent)
                logger.warning(
                    f"[WATCHDOG:{self.agent_name}] ACK returned {resp.status_code} for signal_id={signal_id}"
                )
        except requests.exceptions.RequestException as e:
            logger.error(
                f"[WATCHDOG:{self.agent_name}] ACK failed for signal_id={signal_id}: {e}"
            )


class _BusUnavailable(Exception):
    """Internal: raised when the bus cannot be reached."""
    pass
