# Spec: roll_manager.py — Auto-Roll Manager for Alpha Execution

**Status:** Approved for build (respawn after timeout)
**Date:** 2026-05-XX
**Author:** GENESIS

---

## Purpose
Automatically roll options spreads that reach DTE ≤ 21 forward to a new ~40-DTE expiration instead of requiring manual intervention. Currently exit_monitor.py logs a ROLL FLAG and sends a Telegram alert but takes no action. This module handles the automated roll.

---

## Inputs
- `db_path: str` — Alpha Execution SQLite database path
- `alpaca_client: AlpacaClient` — authenticated Alpaca client
- `bot_token: str` — Telegram bot token for alerts
- `chat_id: str` — Ahmed's Telegram chat ID
- Called by `exit_monitor.evaluate_exits()` when DTE ≤ `DTE_ROLL_THRESHOLD` (21)

---

## Outputs
- `RollResult` dataclass: `{position_id, ticker, action, new_position_id, reason, error}`
- action values: `"rolled"` | `"skipped"` | `"failed"`
- Side-effects:
  - Closes old position in Alpaca (buy-to-close the spread)
  - Opens new spread at ~40 DTE via `place_spread_order`
  - Marks old DB record as `closed` with `close_reason="ROLLED"`
  - Creates new DB record for the replacement position
  - Sends Telegram alert on success or failure

---

## Contracts
**Guarantees:**
- Idempotent: rolling the same position_id twice within the same day is a no-op (dedup key in DB or in-memory set)
- Atomic DB: old position closed + new position opened in same write transaction; if new order fails, old position stays open
- Never rolls a position that was already closed/cancelled
- Max 1 roll per position per calendar day

**Does NOT guarantee:**
- Fill at a specific price (market conditions apply)
- New contract availability (if no 40-DTE contract exists, `skipped` with reason)
- Zero slippage

---

## Failure Modes
1. **Alpaca close order fails** → log error, keep old position open, return `failed`
2. **No suitable roll contract found** → return `skipped` with reason, send alert
3. **New spread order fails** → old position was already closed → attempt to reopen; if reopen fails → send P0 alert to Ahmed + SOVEREIGN, log to CHRONICLE
4. **DB error** → rollback, return `failed`, log exception
5. **Position already rolled today** → return `skipped` (dedup)
6. **Position not in DB** → return `failed` with reason

---

## Test Cases
1. Happy path: DTE=18, position open → roll executes, old closed, new created, Telegram sent
2. Already rolled today → returns `skipped`, no Alpaca calls
3. Alpaca close fails → old position stays open, returns `failed`
4. No roll contract available → returns `skipped` with descriptive reason
5. New order fails after close → P0 alert fired, CHRONICLE logged
6. Position already `closed` in DB → returns `skipped`
7. `evaluate_rolls()` with empty DB → returns empty list, no errors
8. Roll dedup: calling twice for same position_id → second call is skipped

---

## Integration
- Import in `exit_monitor.py`: replace the manual-only `_send_roll_alert` path with `roll_manager.evaluate_rolls()`
- New DB column needed: `rolled_at TEXT` on `positions` table (added via `init_db` migration guard)
- Config: uses existing `DTE_ROLL_THRESHOLD=21`, `TARGET_DTE=40` from `config.py`
