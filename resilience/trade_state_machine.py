"""
trade_state_machine.py — Approach 3: Self-Healing Trade State Machine

Every trade transition is logged. Every failure triggers a defined recovery.
No state is undefined — every possible condition has a response.
Human intervention required ONLY for ESCALATED state.

Implement in: ALL execution services (alpha-exec, prime-exec).
"""

import logging
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

DB_PATH = "/Users/ahmedsadek/nexus/data/trade_states.db"


class TradeState(Enum):
    """All valid states for a trade lifecycle."""
    # Pre-execution
    SIGNAL_GENERATED      = "signal_generated"
    CONCORDANCE_SUBMITTED = "concordance_submitted"
    VERDICT_RECEIVED      = "verdict_received"

    # Capital authorization
    CR_ALLOCATING = "cr_allocating"
    CR_ALLOCATED  = "cr_allocated"

    # Order placement
    ORDER_BUILDING  = "order_building"
    ORDER_SUBMITTED = "order_submitted"

    # Fill confirmation
    FILL_PENDING   = "fill_pending"
    FILL_CONFIRMED = "fill_confirmed"

    # Spread-specific (Alpha — credit spreads)
    LEG1_FILLED          = "leg1_filled"        # Short leg filled
    LEG2_PENDING         = "leg2_pending"        # Long leg not yet filled
    SPREAD_COMPLETE      = "spread_complete"     # Both legs filled
    PARTIAL_FILL_DETECTED = "partial_fill_detected"  # CRITICAL — naked short

    # Active monitoring
    STOP_SET         = "stop_set"
    MONITORING_ACTIVE = "monitoring_active"

    # Exit
    EXIT_TRIGGERED = "exit_triggered"
    EXIT_SUBMITTED = "exit_submitted"
    EXIT_CONFIRMED = "exit_confirmed"

    # Terminal
    CLOSED  = "closed"
    VOIDED  = "voided"

    # Error recovery
    RECOVERING = "recovering"
    ESCALATED  = "escalated"


# State machine transition table
# format: state → (success_next_description, failure_recovery_action)
TRANSITIONS: Dict[TradeState, tuple] = {
    TradeState.SIGNAL_GENERATED:      ("concordance_submit",           "discard_signal"),
    TradeState.CONCORDANCE_SUBMITTED: ("await_verdict",                "resubmit_once"),
    TradeState.VERDICT_RECEIVED:      ("allocate_capital",             "release_nothing"),
    TradeState.CR_ALLOCATING:         ("build_order",                  "release_cr"),
    TradeState.CR_ALLOCATED:          ("submit_order",                 "release_cr"),
    TradeState.ORDER_BUILDING:        ("submit_order",                 "release_cr"),
    TradeState.ORDER_SUBMITTED:       ("confirm_fill",                 "cancel_and_release"),
    TradeState.FILL_PENDING:          ("set_stop",                     "check_alpaca_then_decide"),
    TradeState.LEG1_FILLED:           ("submit_leg2",                  "PARTIAL_FILL_EMERGENCY"),
    TradeState.LEG2_PENDING:          ("confirm_spread",               "PARTIAL_FILL_EMERGENCY"),
    TradeState.SPREAD_COMPLETE:       ("set_stop",                     "log_and_monitor"),
    TradeState.PARTIAL_FILL_DETECTED: ("force_close_leg1",             "escalate_to_ahmed"),
    TradeState.FILL_CONFIRMED:        ("set_stop",                     "log_and_monitor"),
    TradeState.STOP_SET:              ("activate_monitor",             "retry_stop_set"),
    TradeState.MONITORING_ACTIVE:     ("await_exit_trigger",           "check_position_exists"),
    TradeState.EXIT_TRIGGERED:        ("submit_exit",                  "retry_exit"),
    TradeState.EXIT_SUBMITTED:        ("confirm_exit",                 "check_alpaca"),
    TradeState.EXIT_CONFIRMED:        ("post_to_ails",                 "retry_ails"),
    TradeState.CLOSED:                ("release_cr",                   "retry_release"),
}

TERMINAL_STATES = {TradeState.CLOSED, TradeState.VOIDED, TradeState.ESCALATED}


def _ensure_db(db_path: str) -> None:
    """Create trade_state_log table if not exists."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_state_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id  TEXT NOT NULL,
            state     TEXT NOT NULL,
            metadata  TEXT,
            timestamp TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_id ON trade_state_log(trade_id)")
    conn.commit()
    conn.close()


import json
import os


class TradeSM:
    """
    Self-healing state machine for a single trade.

    Every state transition is logged to SQLite.
    Every failure triggers the defined recovery action.
    No state is undefined — every possible condition has a response.
    Human intervention required ONLY for ESCALATED state.

    Usage:
        sm = TradeSM(trade_id="NEXUS_ALPHA_001", db_path=DB_PATH, recovery_fns={...})
        sm.transition(TradeState.CONCORDANCE_SUBMITTED)
        try:
            submit_to_buffer(...)
        except Exception as e:
            sm.on_failure(e)
    """

    def __init__(
        self,
        trade_id: str,
        db_path: str,
        recovery_fns: Dict[str, Callable],
        telegram_alert_fn: Optional[Callable] = None,
    ):
        """
        Args:
            trade_id: Unique trade identifier.
            db_path: SQLite path for state logging.
            recovery_fns: Dict mapping recovery action name → callable(trade_id, metadata).
            telegram_alert_fn: Optional callable(message) for Ahmed alerts.
        """
        self.trade_id = trade_id
        self.db_path = db_path
        self.recovery_fns = recovery_fns
        self.telegram_alert_fn = telegram_alert_fn
        self.current_state = TradeState.SIGNAL_GENERATED
        _ensure_db(db_path)
        self._save_state()

    def transition(self, next_state: TradeState, metadata: Optional[Dict] = None) -> None:
        """
        Move to next state on success.

        Args:
            next_state: The state to transition to.
            metadata: Optional context dict logged with the transition.
        """
        prev = self.current_state
        self.current_state = next_state
        self._save_state(metadata)
        logger.info("Trade %s: %s → %s", self.trade_id, prev.value, next_state.value)

    def on_failure(self, error: Exception, metadata: Optional[Dict] = None) -> None:
        """
        Called when the current operation fails.
        Executes the defined recovery action for this state.
        Never raises — always handles the failure.

        Args:
            error: The exception that caused the failure.
            metadata: Optional context for logging.
        """
        recovery_action = TRANSITIONS.get(self.current_state, (None, "escalate_to_ahmed"))[1]

        logger.error(
            "Trade %s failure in state %s: %s. Recovery: %s",
            self.trade_id, self.current_state.value, error, recovery_action,
        )

        if recovery_action == "PARTIAL_FILL_EMERGENCY":
            self._handle_partial_fill_emergency()
            return

        if recovery_action == "escalate_to_ahmed":
            self._escalate(error, recovery_action)
            return

        recovery_fn = self.recovery_fns.get(recovery_action)
        if recovery_fn:
            try:
                recovery_fn(self.trade_id, metadata)
                self.transition(TradeState.RECOVERING, {
                    "recovered_from": self.current_state.value,
                    "recovery_action": recovery_action,
                })
            except Exception as recovery_error:
                logger.critical("Recovery failed for trade %s: %s", self.trade_id, recovery_error)
                self._escalate(recovery_error, recovery_action)
        else:
            logger.warning("No recovery_fn for action '%s' — escalating", recovery_action)
            self._escalate(error, recovery_action)

    def is_terminal(self) -> bool:
        """Return True if trade is in a terminal state."""
        return self.current_state in TERMINAL_STATES

    def _handle_partial_fill_emergency(self) -> None:
        """
        CRITICAL: Short leg filled, long leg failed = NAKED SHORT position.
        Maximum priority — force close short leg immediately.
        """
        logger.critical(
            "PARTIAL FILL EMERGENCY: Trade %s. Short leg may be naked. Initiating force close.",
            self.trade_id,
        )
        self.current_state = TradeState.PARTIAL_FILL_DETECTED
        self._save_state({"emergency": "partial_fill"})

        force_close_fn = self.recovery_fns.get("force_close_short_leg")
        if force_close_fn:
            try:
                force_close_fn(self.trade_id)
            except Exception as e:
                logger.critical("Force close failed for %s: %s", self.trade_id, e)

        if self.telegram_alert_fn:
            self.telegram_alert_fn(
                f"🚨 PARTIAL FILL EMERGENCY\n"
                f"Trade: {self.trade_id}\n"
                f"Short leg may be filled without long leg hedge.\n"
                f"Force close initiated. Verify in Alpaca dashboard immediately."
            )

    def _escalate(self, error: Exception, recovery_action: str) -> None:
        """Escalate to Ahmed when autonomous recovery is impossible."""
        self.current_state = TradeState.ESCALATED
        self._save_state({"escalation_error": str(error), "failed_recovery": recovery_action})

        if self.telegram_alert_fn:
            self.telegram_alert_fn(
                f"⚠️ TRADE ESCALATION REQUIRED\n"
                f"Trade: {self.trade_id}\n"
                f"Failed recovery: {recovery_action}\n"
                f"Error: {error}\n"
                f"Manual intervention required."
            )

    def _save_state(self, metadata: Optional[Dict] = None) -> None:
        """Persist current state to SQLite."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO trade_state_log (trade_id, state, metadata) VALUES (?, ?, ?)",
            (self.trade_id, self.current_state.value, json.dumps(metadata) if metadata else None)
        )
        conn.commit()
        conn.close()

    @classmethod
    def recover_from_crash(
        cls,
        trade_id: str,
        db_path: str,
        recovery_fns: Dict[str, Callable],
        telegram_alert_fn: Optional[Callable] = None,
    ) -> Optional["TradeSM"]:
        """
        On service restart: restore state machine from DB.
        Call for every trade in DB that is not in a terminal state.

        Returns:
            Restored TradeSM, or None if trade_id not found in DB.
        """
        if not os.path.exists(db_path):
            return None
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT state FROM trade_state_log WHERE trade_id=? ORDER BY id DESC LIMIT 1",
            (trade_id,)
        ).fetchone()
        conn.close()

        if not row:
            logger.warning("recover_from_crash: trade_id %s not found in DB", trade_id)
            return None

        sm = cls.__new__(cls)
        sm.trade_id = trade_id
        sm.db_path = db_path
        sm.recovery_fns = recovery_fns
        sm.telegram_alert_fn = telegram_alert_fn
        sm.current_state = TradeState(row[0])
        logger.info("Recovered trade %s to state %s", trade_id, sm.current_state.value)
        return sm

    @classmethod
    def get_open_trades(cls, db_path: str) -> list:
        """
        Return list of trade_ids that are not in terminal states.
        Use at service restart to recover all open trades.
        """
        if not os.path.exists(db_path):
            return []
        terminal_values = [s.value for s in TERMINAL_STATES]
        placeholders = ",".join("?" * len(terminal_values))
        conn = sqlite3.connect(db_path)
        rows = conn.execute(f"""
            SELECT DISTINCT trade_id FROM trade_state_log
            WHERE trade_id NOT IN (
                SELECT DISTINCT trade_id FROM trade_state_log
                WHERE state IN ({placeholders})
            )
        """, terminal_values).fetchall()
        conn.close()
        return [r[0] for r in rows]
