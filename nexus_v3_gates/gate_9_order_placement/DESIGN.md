# GATE 9: Order Placement — Design Document

## Overview

GATE 9 takes the execution envelope from GATE 7 (order JSON with legs, limit price, strategy context) and submits it to the broker via AlpacaClient, monitors fills, and records positions to SQLite.

**Purpose:** Bridge GATE 7's execution planning to actual broker submission + fill tracking.

---

## Architecture

### Input
Order JSON envelope (from GATE 7):
```json
{
  "order_id": "sage-nvda-bull-call-20260513-001",
  "contract_type": "spread|single",
  "underlying": "NVDA",
  "strategy": "bull_call_spread",
  "expiry": "2025-05-22",
  "legs": [
    {"symbol": "NVDA250522C00900000", "side": "buy", "ratio_qty": 1},
    {"symbol": "NVDA250522C00950000", "side": "sell", "ratio_qty": 1}
  ],
  "qty": 1,
  "order_type": "limit",
  "limit_price": 1.50,
  "macro_regime": "Risk-On",
  "risk_per_position": 1000,
  "submission_time": "2026-05-13T09:31:45Z"
}
```

### Process Flow

```
┌─────────────────┐
│  Order Envelope │ (from GATE 7)
│  (validated)    │
└────────┬────────┘
         │
         ▼
    ┌────────────────────────────┐
    │ GATE 9: Order Placement    │
    ├────────────────────────────┤
    │ 1. Place Order via Alpaca  │
    │    (idempotency via COID)  │
    │ 2. Poll Fills (3 min loop) │
    │ 3. Record Position (SQLite)│
    │ 4. Return Verdict          │
    └────┬───────────┬───────────┘
         │           │
         ▼           ▼
    ┌─PASS──┐  ┌──CONDITIONAL──┐  ┌──FAIL──┐
    │ Fully │  │ Partial Fill  │  │ Unfill │
    │ Filled│  │ After 3 min   │  │ or     │
    │ Order │  │ (escalate to  │  │ Error  │
    │       │  │ Risk Mgmt)    │  │        │
    └───────┘  └───────────────┘  └────────┘
```

---

## Verdict Logic

| Condition | Verdict | Action | Escalation |
|-----------|---------|--------|-----------|
| Order filled, qty ≥ expected | **PASS** | Record position, log success | None |
| Order partially filled (70-99%) after 3-min poll | **CONDITIONAL** | Record position at partial qty, flag for Risk Mgmt | Send to Risk Mgmt queue |
| Order not filled or error after 3-min poll | **FAIL** | Cancel order if cancellable, record error, alert | Escalate to SOVEREIGN |
| Network timeout during submission | **FAIL** | Attempt recovery via client_order_id lookup | Escalate if lookup fails |
| Order rejected by Alpaca (validation) | **FAIL** | Log error code, alert ops | Escalate immediately |

---

## Components

### 1. OrderPlacementGate Class

```python
class OrderPlacementGate:
    """
    Submits validated order envelopes to Alpaca.
    Polls fills asynchronously.
    Records positions to SQLite.
    Returns verdict: PASS / CONDITIONAL / FAIL
    """
    
    def __init__(self, alpaca_client, db_path, logger=None)
    def place_order(self, order_envelope) -> tuple[str, dict]
    def poll_fills(self, order_id, alpaca_order_id, timeout_sec=180)
    def record_position(self, order_envelope, alpaca_order, fill_status)
    def execute(self, order_envelope) -> dict
```

### 2. Position Schema (SQLite)

```sql
CREATE TABLE positions (
  order_id TEXT PRIMARY KEY,
  client_order_id TEXT UNIQUE,
  underlying TEXT,
  strategy TEXT,
  legs_json TEXT,
  side TEXT,
  qty INT,
  fill_qty INT,
  status TEXT,  -- PENDING, FILLED, PARTIAL, FAILED
  entry_price REAL,
  entry_time TIMESTAMP,
  fill_price REAL,
  fill_time TIMESTAMP,
  macro_regime TEXT,
  risk_limit REAL,
  alpaca_order_id TEXT,
  verdict TEXT,  -- PASS, CONDITIONAL, FAIL
  notes TEXT
);
```

### 3. Fill Polling

**Interval:** 3 minutes (configurable)
**Timeout:** 180 seconds (configurable)
**Strategy:**
- Submit order, get Alpaca order_id
- Poll `/v2/orders/{order_id}` at 0s, 30s, 60s, ..., 180s
- On fill: record immediately, return PASS
- On partial (≥70%): log as CONDITIONAL, escalate
- On timeout: attempt cancel if cancellable, return FAIL

### 4. Idempotency

**Key:** `client_order_id` (passed to AlpacaClient.place_spread_order)
**Format:** `{agent}-{ticker}-{strategy}-{timestamp}`
**Max 48 chars** (Alpaca limit)
**Recovery:**
- On 422 during submission: call `get_order_by_client_id()` to recover existing order
- On network timeout: call `get_order_by_client_id()` before resubmitting
- Prevents duplicate orders on restart

---

## Error Handling

### Alpaca API Errors
- **400/422:** Validation error → log details, return FAIL
- **401/403:** Auth error → alert ops immediately
- **429:** Rate limit → backoff 30s, retry once
- **500/502:** Server error → backoff exponential, retry up to 3x
- **Connection timeout:** Network error → attempt COID recovery, if none found, return FAIL

### State Machine
```
Submitted (submitted_time=now)
  ├─ Filled (recorded to DB, PASS)
  ├─ Partial (≥70%, recorded, CONDITIONAL)
  ├─ Expired/Cancelled (FAIL)
  └─ Error/Timeout (FAIL)
```

---

## Logging & Audit Trail

- **Submission:** Log order_id, symbol, qty, limit_price, client_order_id
- **Poll:** Log poll #, status, fill_qty, fill_price (every poll)
- **Fill:** Log final status, fill_price, fill_time, verdict
- **Error:** Log error code, message, attempted recovery action
- **Position:** Write to DB with full context (macro regime, risk limit, strategy)

---

## Testing Strategy

### Unit Tests (30 tests)
1. Order submission (success, 422 recovery, network timeout)
2. Fill polling (immediate, partial, timeout, cancel)
3. Position recording (insert, update, idempotency)
4. Verdict logic (PASS/CONDITIONAL/FAIL conditions)
5. Schema validation (positions table structure)

### Integration Tests (10+ tests)
1. Full happy path (submit → poll → fill → record)
2. Partial fill scenario (70%, escalate)
3. Network recovery (COID lookup on timeout)
4. Error handling (validation rejection, auth failure)

### Coverage
- **Target:** 95%+ of gate_9_order_placement.py
- **Focus:** Error paths, idempotency, verdict logic

---

## Future Enhancements (Phase 2)

- **Repair Agents:** Auto-cancel & resubmit on prolonged partial fills
- **Fill Analytics:** Track fill rate distribution, slippage vs. limit_price
- **Streaming Orders:** WebSocket-based fill updates (vs. polling)
- **Multi-leg Optimization:** Dynamic leg adjustment if one leg fills before others

---

## References

- **GATE 7:** Execution Constructor (produces order_envelope)
- **AlpacaClient:** alpaca_client.py (`place_spread_order`, `get_order_by_client_id`, `get_order`)
- **Schema:** positions.sql (defined in this gate)
- **Spec:** NEXUS V3 Blueprint (GATE 9 phase 1)
