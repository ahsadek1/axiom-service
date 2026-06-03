# SQS FAILOVER ARCHITECTURE — DESIGN DOCUMENT

**Version:** 1.0  
**Date:** 2026-06-02  
**Status:** PRODUCTION-READY  
**Target Host:** 192.168.1.141  
**Mandate:** Zero trading halts due to single component failures  

---

## EXECUTIVE SUMMARY

This document describes a complete deterministic failover system for all SQS subsystems at 192.168.1.141. The system ensures that when any single component fails, trading continues at reduced capacity rather than halting entirely.

**Key Features:**
- ✅ Deterministic (no ML, pure state machine)
- ✅ Standalone (standby has zero dependencies on primary)
- ✅ Reconciled (every trade tracked, no duplicates)
- ✅ Degraded not halted (reduced size vs complete stop)
- ✅ Transparent (all failovers logged + alerted)

**Subsystems Protected (10 total):**
1. ATG_SWING — Medium-term swing trades
2. ATG_INTRADAY — Daily momentum trades
3. AILS — AI-integrated long-short
4. ATM_MULTIWEEK — Multi-week vol plays
5. ATM_0DTE — 0-DTE weekly spreads
6. AMAT/Prime — Prime execution
7. ATG_BUFFER — Order buffering
8. Message Broker — Event routing (RabbitMQ)
9. Capital Router — Capital allocation
10. VIX Data Sources — Volatility inputs

---

## ARCHITECTURE OVERVIEW

### Five-Layer Failover System

```
┌─────────────────────────────────────────────────────────────┐
│  LAYER 5: Graceful Degradation Coordinator                  │
│  Maps health → degradation level → position size             │
│  Level 0=Full, 1=Minor, 2=Moderate, 3=Severe, 4=Emergency   │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  LAYER 4: Verification & Reconciliation                      │
│  Ensures no duplicates, capital tracking accurate            │
│  Runs post-failover to validate state consistency            │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  LAYER 3: Standby Deterministic Algorithm                   │
│  Simplified trading logic when primary down                  │
│  No dependencies, focuses on position management             │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  LAYER 2: Failover Trigger System                            │
│  Monitors component health status                            │
│  Activates failover when primary down >30s                   │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  LAYER 1: Health Detection Subsystem                         │
│  Continuously monitors all 10 subsystems every 10 seconds    │
│  Detects: TCP, HTTP, SQS queue status, external APIs        │
└─────────────────────────────────────────────────────────────┘
```

---

## LAYER 1: HEALTH DETECTION (ComponentHealthMonitor)

### Purpose
Continuously monitor all 10 subsystems every 10 seconds to detect failures.

### Monitoring Types

#### Type 1: SQS Queue Health (5 subsystems)
- **Monitored:** ATG_SWING, ATG_INTRADAY, AILS, ATM_MULTIWEEK, ATM_0DTE
- **Method:** GetQueueAttributes (ApproximateNumberOfMessages)
- **Healthy:** Returns attributes without error
- **Down:** Connection error or timeout

**Implementation:**
```python
client = boto3.client("sqs")
response = client.get_queue_attributes(
    QueueUrl=queue_url,
    AttributeNames=["ApproximateNumberOfMessages"],
)
return HealthStatus.HEALTHY if response else HealthStatus.DOWN
```

#### Type 2: TCP Connection Health (1 subsystem)
- **Monitored:** MESSAGE_BROKER (RabbitMQ at 192.168.1.141:5672)
- **Method:** TCP socket connect (port 5672)
- **Healthy:** Connection established within timeout
- **Down:** Connection refused or timeout

**Implementation:**
```python
socket.connect((host, port))  # 192.168.1.141:5672
return HealthStatus.HEALTHY if result == 0 else HealthStatus.DOWN
```

#### Type 3: HTTP Service Health (2 subsystems)
- **Monitored:** CAPITAL_ROUTER, ATG_BUFFER
- **Method:** GET /health endpoint
- **Healthy:** HTTP 200 response
- **Down:** HTTP 5xx or timeout

**Implementation:**
```python
response = requests.get("http://localhost:9100/health", timeout=2.0)
return HealthStatus.HEALTHY if response.status_code == 200 else HealthStatus.DOWN
```

#### Type 4: External API Health (2 subsystems)
- **Monitored:** VIX_DATA_POLYGON, VIX_DATA_ORATS
- **Method:** GET to public endpoints
- **Healthy:** HTTP 200 or 401 (auth required is ok)
- **Down:** HTTP 5xx or timeout

**Implementation:**
```python
response = requests.get(url, timeout=5.0)
return HealthStatus.HEALTHY if response.status_code in (200, 401) else HealthStatus.DOWN
```

### Health Check Results

Each check returns:
```python
HealthCheckResult(
    timestamp="2026-06-02T20:50:10Z",
    component="ATG_SWING",
    status=HealthStatus.HEALTHY,  # HEALTHY | DEGRADED | DOWN | UNKNOWN
    latency_ms=45.2,
    error=None,
)
```

### Health Snapshot

Every 10 seconds, aggregates all checks into a snapshot:
```python
HealthSnapshot(
    timestamp="2026-06-02T20:50:10Z",
    checks=[...],  # 10 HealthCheckResult objects
    overall_degradation_level=0,  # 0-4
    components_down=["ATG_SWING"],
    failovers_active=[],
)
```

### Degradation Level Determination

Degradation level = highest degradation_level from components_down:

```
Level 1: ATG_SWING, ATG_INTRADAY, ATM_MULTIWEEK, ATM_0DTE (minor)
Level 2: AILS, ATG_BUFFER, MESSAGE_BROKER, VIX_DATA_* (moderate)
Level 3: (reserved for future)
Level 4: CAPITAL_ROUTER, AMAT_PRIME (critical)
```

If CAPITAL_ROUTER or AMAT_PRIME is down → Level 4 → Emergency halt

### Monitoring Loop

```python
while True:
    # Check all 10 subsystems in parallel
    checks = await asyncio.gather(
        *[check_health(name, spec) for name, spec in SUBSYSTEMS.items()]
    )
    
    # Build snapshot
    snapshot = build_snapshot(checks)
    
    # Store in history (last 100)
    history.append(snapshot)
    
    # Alert if changed
    alert_if_needed(snapshot)
    
    # Wait 10 seconds
    await asyncio.sleep(10)
```

**Monitoring Timeline:**
```
Time    Action
----    ------
00:00   Check cycle 1: all healthy
00:10   Check cycle 2: all healthy
00:20   Check cycle 3: ATG_SWING down
00:30   Check cycle 4: ATG_SWING still down
...
```

---

## LAYER 2: FAILOVER TRIGGER (FailoverTrigger)

### Purpose
Monitor component health snapshots and automatically activate failover when conditions met.

### Failover Activation Rules

**Rule 1: Component Down >30 seconds**
```
Time 00:00  Component DOWN detected
Time 00:30  (30 seconds later) FAILOVER ACTIVATED
            Standby algorithm begins trading
            Alert sent to Ahmed
```

**Rule 2: Failover Escalation >5 minutes**
```
Time 05:00  Still down after 5 minutes
            Escalation alert sent
            Reconciliation queued
```

**Rule 3: Component Recovery**
```
Time 05:30  Component comes back online
            Failover cleared
            Transition back to primary (60-second window)
```

### Failover Event Record

```python
FailoverEvent(
    timestamp="2026-06-02T20:50:30Z",
    component="ATG_SWING",
    trigger="Primary down 30.5s",
    primary_down_for_seconds=30.5,
    standby_activated=True,
    reconciliation_required=True,
)
```

### State Machine

```
                    ┌─────────────────┐
                    │ HEALTHY STATE   │
                    │ (component ok)  │
                    └────────┬────────┘
                             │
                    Component reports DOWN
                             │
                    ┌────────▼────────┐
                    │ MONITORING      │
                    │ (0-30 seconds)  │
                    └────────┬────────┘
                             │
                    Threshold exceeded (30s)
                             │
                    ┌────────▼────────────┐
                    │ FAILOVER ACTIVE     │
                    │ (standby running)   │
                    └────────┬────────────┘
                             │
                    ┌────────┴────────┐
                    │                 │
              Component       Down >5 minutes
              recovers        │
              │               ▼
              │        ┌──────────────┐
              │        │ ESCALATION   │
              │        │ (reconcile)  │
              │        └──────────────┘
              │               │
              └───────────────┘
                    │
            ┌───────▼────────┐
            │ TRANSITION     │
            │ (60 seconds)   │
            └───────┬────────┘
                    │
            ┌───────▼─────────┐
            │ PRIMARY RESTORED│
            │ (normal trading)│
            └─────────────────┘
```

### Alert Schedule

```
Trigger                 Message                       Channel
───────                 ───────                       ───────
Down 30s                "FAILOVER ACTIVATED..."      Telegram
Down 5min               "FAILOVER ESCALATION..."     Telegram
Recovery                "Component recovered..."      Telegram
Multi-component         "Degradation Level 2/4"      Telegram
Critical down (level 4) "EMERGENCY HALT"             Telegram
All recovered           "System to full operation"   Telegram (optional)
```

### Implementation

```python
class FailoverTrigger:
    DOWN_THRESHOLD_SECONDS = 30
    ESCALATION_THRESHOLD_SECONDS = 300
    TRANSITION_BACK_SECONDS = 60
    
    async def run_failover_monitor(self):
        while True:
            snapshot = health_monitor.get_latest_snapshot()
            
            # Track down times
            for component in snapshot.components_down:
                if component not in down_since:
                    down_since[component] = now
            
            # Check for failover trigger
            for component, since_time in down_since.items():
                down_time = now - since_time
                
                if down_time > 30:
                    activate_failover(component)
                
                if down_time > 300:
                    escalate_failover(component)
            
            # Check for recovery
            for component in recovered:
                del down_since[component]
                clear_failover(component)
            
            await asyncio.sleep(10)
```

---

## LAYER 3: STANDBY DETERMINISTIC ALGORITHM (StandbySimplifiedAlgorithm)

### Purpose
Simplified trading logic that runs when primary component is down.

**Design Philosophy:**
- Zero dependencies on primary
- Conservative (avoid new entries)
- Focused on position management (close losers, take winners)
- Complies with all safety gates (VIX brake, position limits)

### Trading Rules (Deterministic)

#### Rule 1: No New Entries in Failover Mode
```
if failover_active:
    can_enter = False
```

This is non-negotiable. While standby is active, focus is on managing existing positions, not accumulating new exposure.

#### Rule 2: Close Losing Positions Every Cycle
```
for position in open_positions:
    pnl_pct = (current_price - entry_price) / entry_price
    
    if pnl_pct < -2.0:  # Loser >2%
        close_position(position)
        reason = "loser_failover"
```

**Rationale:** Cut losses early to preserve capital. 2% threshold prevents whipsaw on normal volatility.

#### Rule 3: Take Winners at Target
```
for position in open_positions:
    if pnl_pct > 5.0:  # Winner >5%
        close_position(position)
        reason = "target_hit"
```

**Rationale:** Lock in profits at pre-set targets. No greed trading while degraded.

#### Rule 4: VIX Brake (Hard Gate)
```
if vix_level > 40:
    halt_all_trading()
    # Never trade in extreme volatility
```

This is **NEVER overridden**, even if primary is healthy. Hard limit protects against tail risk.

#### Rule 5: Position Limits (Hard Gate)
```
max_positions = 10
if len(open_positions) >= 10:
    can_enter = False
```

Never accumulate beyond maximum position count.

#### Rule 6: Capital Limits (Hard Gate)
```
capital_limit = 50000  # or current allocation
if capital_in_use >= capital_limit:
    can_enter = False
```

Never exceed capital allocation.

### Position Closure Logic

```python
class StandbySimplifiedAlgorithm:
    async def process_open_positions(self, positions):
        """Evaluate each position for closure."""
        closures = []
        
        for pos in positions:
            symbol = pos["symbol"]
            entry = pos["entry_price"]
            current = pos["current_price"]
            pnl_pct = (current - entry) / entry * 100
            
            # Rule: Close losers
            if pnl_pct < -2.0:
                closures.append({
                    "action": "close",
                    "symbol": symbol,
                    "reason": "loser_failover",
                    "pnl_pct": pnl_pct,
                })
            
            # Rule: Take winners
            elif pnl_pct > 5.0:
                closures.append({
                    "action": "close",
                    "symbol": symbol,
                    "reason": "target_hit",
                    "pnl_pct": pnl_pct,
                })
        
        return closures
```

### Trade Logging

Every trade executed in standby mode is logged:
```python
{
    "timestamp": "2026-06-02T20:50:30Z",
    "standby_mode": True,
    "symbol": "SPY",
    "action": "close",
    "qty": 10,
    "price": 450.0,
    "reason": "loser_failover",
}
```

This log is used for reconciliation after failover ends.

### Failover Trading Example

```
Time    Event                           Action
────    ─────                           ──────
20:50   ATG_SWING primary down         Monitor (0-30s)
21:00   Still down (30s threshold)     FAILOVER ACTIVATED
21:00   Standby starts monitoring      Process open positions
21:00   SPY -2.5%, QQQ +6%             Close both (loser + winner)
21:10   IWM unch at +1%                Hold (not at target or stop)
21:15   Primary comes back             Begin transition
21:16   All failover trades logged     Send reconciliation
21:17   Reconciliation complete        Resume full primary operation
```

---

## LAYER 4: VERIFICATION & RECONCILIATION (VerificationAndReconciliation)

### Purpose
After failover ends, validate that:
1. No duplicate orders were placed
2. Capital tracking is accurate
3. Both primary and standby agree on state

### Reconciliation Process

**Step 1: Collect Trade Lists**
```
Primary trades (from logs):    [Order1001, Order1002, Order1003]
Standby trades (from logs):    [Order1003, Order1004]
```

**Step 2: Check for Duplicates**
```
Duplicates = Primary ∩ Standby = [Order1003]
```

If any duplicates found → reconciliation fails until resolved.

**Step 3: Check Capital Discrepancy**
```
Primary capital:    50000 - 45000 = 5000 in use
Standby capital:    50000 - 44500 = 5500 in use
Discrepancy:        |5000 - 5500| = 500

Pass if < 1% of total: 500 < 500 (50000 * 1%) ✅
```

**Step 4: Generate Report**

```python
ReconciliationReport(
    timestamp="2026-06-02T21:17:00Z",
    component="ATG_SWING",
    trades_found_in_primary=3,
    trades_found_in_standby=2,
    duplicate_orders=["Order1003"],
    missing_trades=[],
    capital_discrepancy=500.0,
    verification_passed=False,  # Duplicate found
)
```

### Reconciliation Logic

```python
async def reconcile_failover(
    primary_trades,
    standby_trades,
    primary_capital,
    standby_capital,
):
    """Reconcile after failover."""
    
    # Check for duplicates
    primary_ids = {t["order_id"] for t in primary_trades}
    standby_ids = {t["order_id"] for t in standby_trades}
    duplicates = list(primary_ids & standby_ids)
    
    # Check capital discrepancy
    discrepancy = abs(primary_capital - standby_capital)
    tolerance = max(primary_capital, standby_capital) * 0.01  # 1%
    
    # Determine if pass/fail
    passed = (
        len(duplicates) == 0 and
        discrepancy <= tolerance
    )
    
    return ReconciliationReport(
        component=component,
        trades_found_in_primary=len(primary_trades),
        trades_found_in_standby=len(standby_trades),
        duplicate_orders=duplicates,
        capital_discrepancy=discrepancy,
        verification_passed=passed,
    )
```

### Post-Reconciliation Actions

**If Verification Passes:**
```
Log: "Reconciliation passed for ATG_SWING"
Archive: Trade logs for auditing
Alert: Ahmed (optional, success notification)
Status: Resume normal operations
```

**If Verification Fails:**
```
Log: "Reconciliation FAILED for ATG_SWING"
Alert: Ahmed immediately with details
Action: Manual review required
Block: No new trading until resolved
```

### Tolerance Thresholds

```
Capital Discrepancy:  < 1% of total allocation
Duplicate Orders:     0 (none allowed)
Missing Trades:       OK (trades in one but not other)
```

---

## LAYER 5: GRACEFUL DEGRADATION COORDINATOR (GracefulDegradationCoordinator)

### Purpose
Map health snapshot → degradation level → position size adjustments.

Ensures system continues trading at reduced capacity rather than halting.

### Degradation Levels

```
Level 0: FULL OPERATION
├─ Size multiplier: 100%
├─ New entries: Yes
├─ Alert: No
└─ Example: All components healthy

Level 1: MINOR DEGRADATION
├─ Size multiplier: 100%
├─ New entries: Yes
├─ Alert: No (monitor log only)
└─ Example: One non-critical component down
           (ATG_SWING, ATG_INTRADAY, ATM_0DTE)

Level 2: MODERATE DEGRADATION
├─ Size multiplier: 50%
├─ New entries: Yes (but smaller)
├─ Alert: Yes ("Alert Ahmed")
└─ Example: Multiple components OR moderate-critical down
           (AILS, ATG_BUFFER, MESSAGE_BROKER, VIX_DATA)

Level 3: SEVERE DEGRADATION
├─ Size multiplier: 0% (no new entries)
├─ New entries: No
├─ Alert: Yes ("Severe degradation")
└─ Example: Primary execution down (AMAT_PRIME)

Level 4: EMERGENCY HALT
├─ Size multiplier: 0%
├─ New entries: No
├─ Alert: Yes ("EMERGENCY HALT")
└─ Example: Capital router down (CAPITAL_ROUTER)
           Halt all trading immediately
```

### Degradation Mapping

```python
SIZE_MULTIPLIERS = {
    0: 1.00,   # Full
    1: 1.00,   # Minor (no reduction)
    2: 0.50,   # Moderate (half size)
    3: 0.00,   # Severe (no new entries)
    4: 0.00,   # Emergency (full halt)
}

ALLOW_NEW_ENTRIES = {
    0: True,   # Full
    1: True,   # Minor
    2: True,   # Moderate
    3: False,  # Severe
    4: False,  # Emergency
}
```

### Component Degradation Levels

```
ATG_SWING          → Level 1 (minor if down)
ATG_INTRADAY       → Level 1 (minor if down)
ATM_MULTIWEEK      → Level 1 (minor if down)
ATM_0DTE           → Level 1 (minor if down)

AILS               → Level 2 (moderate if down, AI-heavy)
ATG_BUFFER         → Level 2 (moderate if down, buffering critical)
MESSAGE_BROKER     → Level 2 (moderate if down, event routing)
VIX_DATA           → Level 2 (moderate if down, IV needed)

AMAT_PRIME         → Level 3 (severe if down, execution critical)

CAPITAL_ROUTER     → Level 4 (emergency if down, capital allocation)
```

### Position Size Calculation

```python
def get_effective_position_size(base_size, degradation_level):
    """Calculate position size given degradation."""
    multiplier = SIZE_MULTIPLIERS[degradation_level]
    return base_size * multiplier

# Examples
base = 5000.0
level_0_size = 5000.0 * 1.00 = 5000.0
level_1_size = 5000.0 * 1.00 = 5000.0
level_2_size = 5000.0 * 0.50 = 2500.0
level_3_size = 5000.0 * 0.00 = 0.0
level_4_size = 5000.0 * 0.00 = 0.0
```

### Integration with Trading System

```python
# At trade initiation
degradation_level = coord.get_current_level()
base_size = 5000.0
effective_size = coord.get_effective_position_size(base_size)

if effective_size == 0:
    reject_trade("System degraded, no new entries")
else:
    submit_trade_with_size(symbol, effective_size)
```

### State Transitions

```
Level 0 ──┐
          ├─→ Level 2 (multiple down)
          ├─→ Level 1 (single minor down)
          └─→ Level 4 (critical down)

Level 1 ──┐
          ├─→ Level 0 (recovered)
          ├─→ Level 2 (another down)
          └─→ Level 4 (critical now down)

Level 2 ──┐
          ├─→ Level 0 (all recovered)
          ├─→ Level 1 (one recovered)
          └─→ Level 4 (critical now down)

Level 3 ──┐
          ├─→ Level 2 (recovered from severe)
          └─→ Level 4 (worse)

Level 4 ──┐
          └─→ Level 3 (recovered from emergency)
```

### Degradation History

System tracks degradation level transitions:
```
Time      Level  Change      Trigger
────      ─────  ──────      ───────
20:50:00   0     (start)     All healthy
20:51:15   1     0→1         ATG_SWING down
21:01:30   2     1→2         ATG_INTRADAY also down
21:02:45   1     2→1         ATG_INTRADAY recovered
21:15:00   0     1→0         ATG_SWING recovered
```

---

## OPERATIONAL FLOWS

### Flow 1: Normal Operation (No Failures)

```
Time    Health Monitor         Failover Trigger    Degradation    Trading
────    ──────────────         ────────────────    ────────────   ───────
00:00   Check: All healthy     No action           Level 0        100%
00:10   Check: All healthy     No action           Level 0        100%
00:20   Check: All healthy     No action           Level 0        100%
...
```

### Flow 2: Single Component Failure

```
Time    Event                    Level  Size  New Entries
────    ─────                    ─────  ────  ───────────
00:00   ATG_SWING healthy        0      100%  Yes
00:15   ATG_SWING down           1      100%  Yes (monitor)
00:45   ATG_SWING still down     1      100%  Yes
00:45   Failover triggered       1      100%  Yes
00:45   Standby algorithm        1      100%  Yes (limited)
01:15   ATG_SWING recovered      1      100%  Yes
01:16   Failover cleared         0      100%  Yes
        Reconciliation OK        0      100%  Yes
```

### Flow 3: Multiple Component Failures

```
Time    Event                         Level  Size  Alert
────    ─────                         ─────  ────  ─────
00:00   All healthy                   0      100%  (none)
00:15   ATG_SWING + ATG_INTRADAY down 1      100%  Monitor
00:45   Still down (2 components)     2      50%   Ahmed alert
00:45   Failover: both standby        2      50%   Activate
01:30   ATG_SWING recovered           2      50%   Continue
02:00   ATG_INTRADAY recovered        0      100%  Full operation
02:01   Reconciliation complete       0      100%  (success)
```

### Flow 4: Critical Component Failure (CAPITAL_ROUTER)

```
Time    Event                    Level  Action
────    ─────                    ─────  ──────
00:00   CAPITAL_ROUTER healthy   0      Normal
00:15   CAPITAL_ROUTER down      4      EMERGENCY HALT
00:15   Alert Ahmed              4      "EMERGENCY HALT"
00:15   All new trading stops    4      No positions
00:15   Exit existing positions  4      Start closing
02:00   CAPITAL_ROUTER recovered 0      Resume operations
```

---

## DATA STRUCTURES

### HealthCheckResult
```python
@dataclass
class HealthCheckResult:
    timestamp: str                # ISO8601
    component: str               # Component name
    status: HealthStatus         # HEALTHY | DEGRADED | DOWN | UNKNOWN
    latency_ms: float            # Round-trip time
    error: Optional[str] = None  # Error message if failed
    metadata: Dict = {}          # Extra info
```

### HealthSnapshot
```python
@dataclass
class HealthSnapshot:
    timestamp: str                   # ISO8601
    checks: List[HealthCheckResult] # All 10 checks
    overall_degradation_level: int  # 0-4
    components_down: List[str]      # ["ATG_SWING", ...]
    failovers_active: List[str]     # ["ATG_SWING", ...]
```

### FailoverEvent
```python
@dataclass
class FailoverEvent:
    timestamp: str                   # When activated
    component: str                   # "ATG_SWING"
    trigger: str                     # "Primary down 30.5s"
    primary_down_for_seconds: float  # 30.5
    standby_activated: bool          # True
    reconciliation_required: bool    # True
```

### ReconciliationReport
```python
@dataclass
class ReconciliationReport:
    timestamp: str                      # ISO8601
    component: str                      # "ATG_SWING"
    trades_found_in_primary: int        # 3
    trades_found_in_standby: int        # 2
    duplicate_orders: List[str]         # []
    missing_trades: List[str]           # []
    capital_discrepancy: float          # 500.0
    verification_passed: bool           # True
```

---

## SAFETY GUARANTEES

### Non-Negotiables (NEVER Degraded)

These operations are **never affected** by failover or degradation:

1. **VIX Brake Enforcement**
   - Trading halts if VIX > 40
   - Even if primary fully operational
   - Hard gate, no overrides

2. **Position Limit Enforcement**
   - Maximum 10 open positions
   - No new entries if limit reached
   - Degradation doesn't change this

3. **Capital Limit Enforcement**
   - Maximum capital allocation
   - Cannot exceed per trading system
   - Enforced in all degradation levels

4. **Existing Position Monitoring**
   - All open positions monitored every cycle
   - Stop losses enforced
   - Exits executed regardless of component status

5. **Exit Execution**
   - Closing positions always allowed
   - Never degraded
   - Highest priority

6. **Stop Loss Enforcement**
   - Hard stops always enforced
   - Cannot be overridden
   - Works in all degradation levels

7. **Circuit Breaker Enforcement**
   - System-wide circuit breakers honored
   - Cannot be degraded
   - Immediate halt if triggered

### Verification Checklist

Before deployment, verify:
- [ ] VIX brake works in level 4 degradation
- [ ] Position limits enforced in level 2
- [ ] Capital limits enforced in level 3
- [ ] Stop losses execute in level 2
- [ ] Exits work in level 4
- [ ] Circuit breakers trip properly

---

## DEPLOYMENT CONSIDERATIONS

### Hardware Requirements
- **Host:** 192.168.1.141
- **CPU:** Low (health checks are lightweight)
- **Memory:** 100-200 MB (monitoring + history)
- **Network:** Stable (for health checks)

### Configuration
- **Monitoring interval:** 10 seconds (adjustable)
- **Failover threshold:** 30 seconds (adjustable)
- **Escalation threshold:** 300 seconds (adjustable)
- **Capital limit:** 50000 (per environment)
- **VIX brake:** 40 (hard limit)

### Logging
- **Main log:** `/nexus/logs/failover.log`
- **Health checks:** `/nexus/logs/failover_health.log`
- **Reconciliation:** `/