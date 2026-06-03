"""
guardian.py — Universal Alpaca Guardian
========================================
One instance per Alpaca account. Zero direct API calls allowed.
Every failure has a deterministic fix. Never gives up until fully recovered.

This is the single entry point for ALL Alpaca interactions across
every system: THESIS, Nexus Alpha, Nexus Prime, all SQS accounts.

Architecture:
  AlpacaGuardian
  ├── CircuitBreaker       — stops hammering a down API
  ├── RequestExecutor      — every call goes through here
  ├── AccountStateMachine  — HEALTHY/DEGRADED/RECOVERING/DOWN
  ├── GhostHunter          — continuous ghost position cleanup
  └── RecoveryLoop         — persistent recovery until fully back

Ahmed directive:
  "I see Alpaca crashing all the time. Often times whatever is in place
   is not enough to get it fully back up and operational.
   Ghost allocations very common. Capital drift commonly noted.
   I would want you to not rely much on what already exists as it is
   inefficient at best."
"""
from __future__ import annotations
import logging
import os
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from .circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from .request_executor import RequestExecutor
from .ghost_hunter import GhostHunter
from .error_taxonomy import (
    AlpacaError, AlpacaErrorCode,
    ALPACA_ERROR_REGISTRY, Severity,
)

log = logging.getLogger("nexus.alpaca.guardian")

# Recovery loop — never gives up
RECOVERY_PROBE_INTERVAL_S = 30    # probe every 30s when down
RECOVERY_FULL_RECON_S     = 60    # full reconciliation wait after recovery


class AccountState(str, Enum):
    HEALTHY    = "HEALTHY"      # All systems normal
    DEGRADED   = "DEGRADED"     # API slow/intermittent, trading continues
    RECOVERING = "RECOVERING"   # API was down, recovery in progress
    DOWN       = "DOWN"         # API confirmed down, trading halted


class AlpacaGuardian:
    """
    Universal Alpaca Guardian. One instance per account.

    Usage:
        guardian = AlpacaGuardian(
            system_id  = "THESIS",
            api_key    = "PKUYQ...",
            secret_key = "4UDMa...",
            db_path    = "/Users/ahmedsadek/nexus/data/thesis_positions.db",
            notify_fn  = send_telegram,
        )
        guardian.start()

        # Use guardian instead of direct Alpaca calls
        account  = guardian.get_account()
        order    = guardian.place_option_order(...)
        positions = guardian.get_positions()
    """

    def __init__(
        self,
        system_id:  str,
        api_key:    str,
        secret_key: str,
        db_path:    Optional[str] = None,
        notify_fn:  Optional[Callable[[str], None]] = None,
        positions_table: str = "positions",
        status_column:   str = "status",
        ticker_column:   str = "ticker",
        open_value:      str = "OPEN",
    ):
        self.system_id  = system_id
        self.notify_fn  = notify_fn

        # ── Core components ───────────────────────────────────────────────────
        self._cb = CircuitBreaker(
            system_id         = system_id,
            failure_threshold = 5,
            failure_window_s  = 60,
            recovery_probe_s  = RECOVERY_PROBE_INTERVAL_S,
            success_threshold = 3,
            notify_fn         = notify_fn,
        )

        self._executor = RequestExecutor(
            system_id       = system_id,
            api_key         = api_key,
            secret_key      = secret_key,
            circuit_breaker = self._cb,
            notify_fn       = notify_fn,
        )

        # ── Ghost hunter (only if DB provided) ───────────────────────────────
        self._ghost_hunter: Optional[GhostHunter] = None
        if db_path and os.path.exists(os.path.dirname(db_path)):
            self._ghost_hunter = GhostHunter(
                system_id        = system_id,
                db_path          = db_path,
                executor         = self._executor,
                notify_fn        = notify_fn,
                positions_table  = positions_table,
                status_column    = status_column,
                ticker_column    = ticker_column,
                open_value       = open_value,
            )

        # ── State machine ─────────────────────────────────────────────────────
        self._state      = AccountState.HEALTHY
        self._state_lock = threading.Lock()
        self._running    = False
        self._recovery_thread: Optional[threading.Thread] = None

        # ── Stats ─────────────────────────────────────────────────────────────
        self._stats = {
            "total_calls":      0,
            "total_errors":     0,
            "total_recoveries": 0,
            "ghosts_fixed":     0,
            "uptime_start":     time.time(),
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start guardian — launches ghost hunter and recovery monitor."""
        self._running = True

        # Startup health check
        account = self._executor.probe()
        if account:
            self._set_state(AccountState.HEALTHY)
            log.info(
                "[%s] Guardian started — account %s ACTIVE BP=$%s",
                self.system_id,
                account.get("account_number","?"),
                account.get("options_buying_power") or account.get("buying_power","?"),
            )
        else:
            self._set_state(AccountState.DOWN)
            log.error("[%s] Guardian started but Alpaca unreachable — entering recovery", self.system_id)
            self._start_recovery_loop()

        # Ghost hunter
        if self._ghost_hunter:
            self._ghost_hunter.start()

        # Recovery monitor (watches for state degradation)
        threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name=f"guardian-monitor-{self.system_id.lower()}",
        ).start()

        log.info("[%s] Guardian fully operational", self.system_id)

    def stop(self) -> None:
        self._running = False
        if self._ghost_hunter:
            self._ghost_hunter.stop()

    # ── State machine ─────────────────────────────────────────────────────────

    def _set_state(self, new_state: AccountState) -> None:
        with self._state_lock:
            old_state = self._state
            self._state = new_state
            if old_state != new_state:
                log.info("[%s] State: %s → %s", self.system_id, old_state, new_state)
                if new_state == AccountState.HEALTHY and old_state != AccountState.HEALTHY:
                    self._stats["total_recoveries"] += 1
                    self._notify(
                        f"ALPACA GUARDIAN: {self.system_id} RECOVERED\n"
                        f"State: {old_state} → HEALTHY\n"
                        f"Trading resumed. Running position reconciliation."
                    )

    @property
    def state(self) -> AccountState:
        return self._state

    @property
    def is_healthy(self) -> bool:
        return self._state == AccountState.HEALTHY

    @property
    def can_trade(self) -> bool:
        return self._state in (AccountState.HEALTHY, AccountState.DEGRADED)

    # ── Monitor loop ──────────────────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        """Continuous health monitor. Detects degradation and triggers recovery."""
        while self._running:
            try:
                if self._state == AccountState.DOWN:
                    time.sleep(RECOVERY_PROBE_INTERVAL_S)
                    continue

                if self._cb.is_open:
                    if self._state != AccountState.DOWN:
                        self._set_state(AccountState.DOWN)
                        self._start_recovery_loop()

                elif self._cb.state.value == "HALF_OPEN":
                    self._set_state(AccountState.RECOVERING)

                else:
                    if self._state == AccountState.RECOVERING:
                        # Just recovered — run full reconciliation
                        self._post_recovery_reconciliation()
                    elif self._state != AccountState.HEALTHY:
                        self._set_state(AccountState.HEALTHY)

            except Exception as exc:
                log.error("[%s] Monitor loop error: %s", self.system_id, exc)

            time.sleep(15)

    # ── Recovery loop ─────────────────────────────────────────────────────────

    def _start_recovery_loop(self) -> None:
        """Start persistent recovery — probes until API is back."""
        if (self._recovery_thread and
                self._recovery_thread.is_alive()):
            return  # Already recovering

        self._recovery_thread = threading.Thread(
            target=self._recovery_loop,
            daemon=True,
            name=f"guardian-recovery-{self.system_id.lower()}",
        )
        self._recovery_thread.start()

    def _recovery_loop(self) -> None:
        """
        Persistent recovery. Probes every 30s until API is back.
        Never gives up. Notifies on recovery.
        """
        attempt = 0
        log.error("[%s] RECOVERY LOOP STARTED — will probe every %ds",
                 self.system_id, RECOVERY_PROBE_INTERVAL_S)

        while self._running and self._state == AccountState.DOWN:
            attempt += 1
            time.sleep(RECOVERY_PROBE_INTERVAL_S)

            account = self._executor.probe()
            if account:
                acct_status = account.get("status","")
                if acct_status == "ACTIVE":
                    log.info("[%s] Recovery: Alpaca back online (attempt %d)",
                            self.system_id, attempt)
                    self._cb.force_close()
                    self._set_state(AccountState.RECOVERING)
                    self._post_recovery_reconciliation()
                    self._set_state(AccountState.HEALTHY)
                    return
                else:
                    log.warning("[%s] Recovery: account status=%s (not ACTIVE)",
                               self.system_id, acct_status)
            else:
                if attempt % 5 == 0:  # Alert every 5 attempts (2.5 min)
                    self._notify(
                        f"ALPACA DOWN: {self.system_id}\n"
                        f"Recovery attempt {attempt}\n"
                        f"Alpaca unreachable for {attempt * RECOVERY_PROBE_INTERVAL_S / 60:.0f} min\n"
                        f"Trading HALTED. Continuing to probe every {RECOVERY_PROBE_INTERVAL_S}s."
                    )

        log.info("[%s] Recovery loop exiting (running=%s state=%s)",
                self.system_id, self._running, self._state)

    def _post_recovery_reconciliation(self) -> None:
        """After recovery, sync positions before resuming trading."""
        log.info("[%s] Post-recovery reconciliation starting", self.system_id)
        time.sleep(5)  # Let Alpaca stabilize
        if self._ghost_hunter:
            try:
                results = self._ghost_hunter.run_scan()
                log.info("[%s] Post-recovery scan: %s", self.system_id, results)
            except Exception as exc:
                log.error("[%s] Post-recovery scan failed: %s", self.system_id, exc)

    # ── Public API — ALL Alpaca calls go through here ────────────────────────

    def _check_can_trade(self) -> None:
        """Raise if trading is not allowed in current state."""
        if not self.can_trade:
            raise RuntimeError(
                f"{self.system_id} guardian state is {self._state} — trading halted"
            )

    def get_account(self) -> dict:
        """Get account info — always allowed."""
        self._stats["total_calls"] += 1
        return self._executor.get_account()

    def get_positions(self) -> list:
        """Get all open positions."""
        self._stats["total_calls"] += 1
        return self._executor.get_positions()

    def get_orders(self, status: str = "open") -> list:
        self._stats["total_calls"] += 1
        return self._executor.get_orders(status=status)

    def get_order(self, order_id: str) -> Optional[dict]:
        self._stats["total_calls"] += 1
        return self._executor.get_order(order_id)

    def get_buying_power(self) -> float:
        """Get current options buying power."""
        account = self.get_account()
        return float(
            account.get("options_buying_power") or
            account.get("buying_power") or 0
        )

    def place_option_order(
        self,
        contract_symbol: str,
        qty:             int,
        side:            str,
        order_type:      str = "limit",
        limit_price:     Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """Place options order — blocked if guardian state is DOWN."""
        self._check_can_trade()
        self._stats["total_calls"] += 1
        try:
            return self._executor.place_option_order(
                contract_symbol = contract_symbol,
                qty             = qty,
                side            = side,
                order_type      = order_type,
                limit_price     = limit_price,
                client_order_id = client_order_id,
            )
        except Exception as exc:
            self._stats["total_errors"] += 1
            self._handle_trade_error(exc, contract_symbol)
            raise

    def place_order(
        self,
        symbol:          str,
        qty:             int,
        side:            str,
        order_type:      str = "limit",
        limit_price:     Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """Place equity order — blocked if guardian state is DOWN."""
        self._check_can_trade()
        self._stats["total_calls"] += 1
        try:
            return self._executor.place_order(
                symbol          = symbol,
                qty             = qty,
                side            = side,
                order_type      = order_type,
                limit_price     = limit_price,
                client_order_id = client_order_id,
            )
        except Exception as exc:
            self._stats["total_errors"] += 1
            self._handle_trade_error(exc, symbol)
            raise

    def cancel_order_safe(self, order_id: str) -> bool:
        self._stats["total_calls"] += 1
        return self._executor.cancel_order_safe(order_id)

    def wait_for_fill(
        self,
        order_id:   str,
        timeout_s:  int = 30,
    ) -> Optional[dict]:
        return self._executor.wait_for_fill(order_id, timeout_s)

    def close_position(self, symbol: str, qty: Optional[float] = None) -> Optional[dict]:
        """Close position — allowed even when DEGRADED."""
        self._stats["total_calls"] += 1
        params = {}
        if qty is not None:
            params["qty"] = str(qty)
        try:
            return self._executor.delete(f"/v2/positions/{symbol}", params=params)
        except Exception as exc:
            log.error("[%s] Close position failed for %s: %s", self.system_id, symbol, exc)
            return None

    def get_option_contracts(
        self,
        underlying:       str,
        exp_date_gte:     str,
        exp_date_lte:     str,
        option_type:      str = "put",
        strike_price_gte: Optional[float] = None,
        strike_price_lte: Optional[float] = None,
        limit:            int = 50,
    ) -> list:
        self._stats["total_calls"] += 1
        params = {
            "underlying_symbols":  underlying,
            "type":               option_type,
            "expiration_date_gte": exp_date_gte,
            "expiration_date_lte": exp_date_lte,
            "tradable":            "true",
            "limit":               limit,
        }
        if strike_price_gte:
            params["strike_price_gte"] = str(int(strike_price_gte))
        if strike_price_lte:
            params["strike_price_lte"] = str(int(strike_price_lte))
        try:
            data = self._executor.get("/v2/options/contracts", params=params)
            return data.get("option_contracts", [])
        except Exception as exc:
            log.error("[%s] Options chain failed for %s: %s", self.system_id, underlying, exc)
            return []

    def get_option_snapshot(self, symbol: str) -> Optional[dict]:
        """Get options quote/greeks snapshot."""
        try:
            data = self._executor.get_data(
                "/v1beta1/options/snapshots",
                params={"symbols": symbol, "feed": "indicative"},
            )
            return data.get("snapshots", {}).get(symbol)
        except Exception:
            return None

    def get_stock_price(self, ticker: str) -> Optional[float]:
        """Get latest stock price with fallback."""
        try:
            data = self._executor.get_data(
                f"/v2/stocks/{ticker}/trades/latest",
                params={"feed": "iex"},
            )
            price = float(data.get("trade", {}).get("p", 0))
            return price if price > 0 else None
        except Exception:
            pass
        try:
            data = self._executor.get_data(
                "/v2/stocks/snapshots",
                params={"symbols": ticker, "feed": "iex"},
            )
            price = float(data.get(ticker, {}).get("latestTrade", {}).get("p", 0))
            return price if price > 0 else None
        except Exception:
            return None

    # ── Error handling ────────────────────────────────────────────────────────

    def _handle_trade_error(self, exc: Exception, symbol: str) -> None:
        """Handle trade error — update state machine if needed."""
        if isinstance(exc, AlpacaError):
            if exc.error_code in (
                AlpacaErrorCode.EAL003,   # service down
                AlpacaErrorCode.EAL001,   # timeout
            ):
                if self._state == AccountState.HEALTHY:
                    self._set_state(AccountState.DEGRADED)
        elif isinstance(exc, CircuitBreakerOpen):
            self._set_state(AccountState.DOWN)
            self._start_recovery_loop()

    # ── Manual operations ─────────────────────────────────────────────────────

    def run_ghost_scan(self) -> Optional[dict]:
        """Manually trigger ghost scan."""
        if self._ghost_hunter:
            return self._ghost_hunter.run_scan()
        return None

    def force_recovery(self) -> None:
        """Force recovery attempt (manual intervention)."""
        self._cb.force_close()
        self._set_state(AccountState.RECOVERING)
        self._post_recovery_reconciliation()
        self._set_state(AccountState.HEALTHY)

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Complete guardian status."""
        uptime = time.time() - self._stats["uptime_start"]
        return {
            "system_id":         self.system_id,
            "state":             self._state.value,
            "can_trade":         self.can_trade,
            "circuit_breaker":   self._cb.status(),
            "stats":             {**self._stats, "uptime_seconds": round(uptime)},
            "ghost_hunter":      "active" if self._ghost_hunter else "disabled",
            "timestamp":         datetime.now(timezone.utc).isoformat(),
        }

    def _notify(self, msg: str) -> None:
        if self.notify_fn:
            try:
                self.notify_fn(msg)
            except Exception:
                pass
