# MAXIMUS Testing Plan

## Overview

Complete testing strategy for MAXIMUS autonomous monitoring system.

---

## Phase 1: Unit Tests (No Market Hours Required)

### Test 1.1: Health Monitor — CHRONICLE Check

**File:** `test_health_monitor_chronicle.py`

```python
import asyncio
from thesis_health_monitor import ThesisHealthMonitor

async def test_chronicle_healthy():
    monitor = ThesisHealthMonitor()
    result = await monitor._check_chronicle()
    
    assert result.component == "chronicle"
    assert result.status.value in ["healthy", "degraded", "critical"]
    print(f"✅ CHRONICLE check: {result.message}")

asyncio.run(test_chronicle_healthy())
```

**Expected:** `status=healthy` with row count

---

### Test 1.2: Health Monitor — Alpaca Check

**File:** `test_health_monitor_alpaca.py`

```python
import asyncio
import os
from thesis_health_monitor import ThesisHealthMonitor

async def test_alpaca_healthy():
    alpaca_key = os.getenv("ALPACA_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET")
    
    monitor = ThesisHealthMonitor(
        alpaca_key=alpaca_key,
        alpaca_secret=alpaca_secret
    )
    result = await monitor._check_alpaca()
    
    assert result.component == "alpaca"
    assert result.status.value == "healthy"
    assert result.details.get("buying_power", 0) > 0
    print(f"✅ Alpaca check: {result.message}")

asyncio.run(test_alpaca_healthy())
```

**Expected:** `status=healthy` with equity/cash/buying_power

---

### Test 1.3: Status Reporter — Message Formatting

**File:** `test_status_reporter_formatting.py`

```python
from thesis_status_reporter import ThesisStatusReporter

def test_mid_session_format():
    reporter = ThesisStatusReporter()
    
    trades = [
        {"symbol": "SPY", "direction": "BUY", "entry_price": 450, "pnl": 100},
        {"symbol": "QQQ", "direction": "SELL", "entry_price": 350, "pnl": -50},
    ]
    positions = [
        {"symbol": "IWM", "qty": 10, "avg_fill_price": 200, "unrealized_plpc": 5.2},
    ]
    errors = ["API timeout", "Connection reset"]
    health = {
        "chronicle_ok": True,
        "scheduler_ok": True,
        "oracle_ok": True,
    }
    
    message = reporter._format_mid_session_message(trades, positions, errors, health)
    
    assert "THESIS MID-SESSION UPDATE" in message
    assert "2 Trades Executed" in message or "📈" in message
    assert "1 Open Positions" in message or "💼" in message
    print(f"✅ Message formatting:\n{message}")

test_mid_session_format()
```

**Expected:** Properly formatted message with emoji and data

---

### Test 1.4: Trading Validator — Account Access

**File:** `test_trading_validator_account.py`

```python
import asyncio
import os
from thesis_trading_validator import ThesisTradingValidator

async def test_account_access():
    alpaca_key = os.getenv("ALPACA_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET")
    
    validator = ThesisTradingValidator(
        alpaca_key=alpaca_key,
        alpaca_secret=alpaca_secret
    )
    result = await validator._check_account_access()
    
    assert result["status"] == "✅"
    assert "Equity" in result["message"]
    print(f"✅ Account access: {result['message']}")

asyncio.run(test_account_access())
```

**Expected:** `status=✅` with equity information

---

## Phase 2: Integration Tests (Before Market Hours)

### Test 2.1: Full Health Check Suite

**Time:** Before 9:30 AM ET

```bash
cd /Users/ahmedsadek/nexus/thesis
python -m pytest tests/test_health_suite.py -v
```

**What it checks:**
1. CHRONICLE connectivity
2. APScheduler status (if service running)
3. Alpaca connectivity
4. Entry logic endpoint
5. Position data consistency

**Expected:** All checks return either `healthy` or `degraded` (never hang)

---

### Test 2.2: Status Reporter Integration

**Time:** Before market hours (9:00 AM - 9:30 AM)

```bash
cd /Users/ahmedsadek/nexus/thesis
python tests/test_status_reporter_integration.py
```

**What it does:**
1. Fetch sample data from CHRONICLE
2. Fetch sample positions from Alpaca
3. Format mid-session and post-close messages
4. **NOT SENT** — just formatted and logged

**Expected:** No errors in formatting or data fetch

---

### Test 2.3: Trading Validator — Non-Destructive

**Time:** Before market hours

```bash
cd /Users/ahmedsadek/nexus/thesis
python tests/test_trading_validator_dry_run.py
```

**What it does:**
1. Check account access
2. Verify order submission API reachable
3. Verify position query API reachable
4. **Does NOT submit real orders**

**Expected:** All API endpoints responsive

---

## Phase 3: Market Hours Tests (9:30 AM - 4:00 PM ET)

### Test 3.1: Trading Validator — Live Test

**Time:** 9:35 AM - 9:40 AM ET (early market, before mid-session report)

**Action:**

```bash
cd /Users/ahmedsadek/nexus/thesis
python -m thesis_trading_validator
```

**What it does:**
1. Submits REAL order: BUY 1 SPY
2. Waits 1 second
3. Verifies position appears
4. Submits REAL order: SELL 1 SPY
5. Waits 1 second
6. Verifies position closed
7. Reports result to Ahmed

**Expected Output:**
```json
{
  "status": "pass",
  "steps": [
    {"name": "Account Access", "status": "✅", "message": "..."},
    {"name": "Submit Test Order (SPY)", "status": "✅", "message": "Order submitted: ..."},
    {"name": "Verify Position", "status": "✅", "message": "Position verified: 1 SPY"},
    {"name": "Close Test Position", "status": "✅", "message": "Position closed successfully"},
    {"name": "Verify Position Closed", "status": "✅", "message": "Position confirmed closed"}
  ]
}
```

**Ahmed receives:** ✅ THESIS TRADING VALIDATOR — 09:35 AM ET

---

### Test 3.2: Health Check Automatic Runs

**Time:** Continuously during 9:30 AM - 4:00 PM

**What happens:**
- Every 30 minutes, health check runs automatically
- Results logged to `/Users/ahmedsadek/nexus/logs/thesis-health.log`
- If any component fails, auto-fix attempts run
- If 3 consecutive failures, escalation sent to Ahmed

**Verification:**

```bash
# Watch health logs
tail -f /Users/ahmedsadek/nexus/logs/thesis-health.log

# Expected entries every 30 min:
# [2026-06-01 10:00:00] Health check complete: healthy...
# [2026-06-01 10:30:00] Health check complete: healthy...
# [2026-06-01 11:00:00] Health check complete: healthy...
```

---

### Test 3.3: Mid-Session Report Delivery

**Time:** 12:15 PM ET

**What happens:**
1. Status reporter automatically runs
2. Fetches trades from CHRONICLE
3. Fetches positions from Alpaca
4. Fetches recent errors
5. Sends formatted message to Ahmed

**Ahmed receives at 12:15 PM:**
```
📊 THESIS MID-SESSION UPDATE
12:15 PM ET

📈 Trades Executed: N
💼 Open Positions: M
⚠️ Errors (Last 4h): K
✅ System Health: [details]
```

**Verification:**
```bash
tail -f /Users/ahmedsadek/nexus/logs/thesis-status.log
# [2026-06-01 12:15:00] Sending mid-session update...
# [2026-06-01 12:15:05] Mid-session update sent successfully.
```

---

### Test 3.4: Post-Close Summary Delivery

**Time:** 4:15 PM ET

**What happens:**
1. Status reporter automatically runs
2. Calculates daily P&L
3. Summarizes daily failures
4. Checks tomorrow readiness
5. Sends formatted message to Ahmed

**Ahmed receives at 4:15 PM:**
```
📊 THESIS POST-CLOSE SUMMARY
4:15 PM ET

Daily Results:
  • Trades: N
  • Total P&L: $XXXX.XX
  • Avg P&L/Trade: $XXX.XX

Thesis Context:
  • Regime: RISK-ON/RISK-OFF
  • VIX: XX.X
  • Market Health: HEALTHY

Tomorrow Readiness: ✅ All systems ready
```

**Verification:**
```bash
tail -f /Users/ahmedsadek/nexus/logs/thesis-status.log
# [2026-06-01 16:15:00] Sending post-close summary...
# [2026-06-01 16:15:05] Post-close summary sent successfully.
```

---

## Phase 4: Failure Scenarios (If Applicable)

### Test 4.1: CHRONICLE Unavailable

**Simulate:**
```bash
# Stop CHRONICLE (or disconnect network)
sudo systemctl stop postgres  # if using postgres
# or move the SQLite file temporarily
mv /Users/ahmedsadek/nexus/data/chronicle.db \
   /Users/ahmedsadek/nexus/data/chronicle.db.bak
```

**Expected:**
1. First health check fails → logs error, attempts auto-fix
2. Auto-fix Tier 0: tries to reconnect
3. Second health check fails → logs, auto-fix Tier 1
4. Third health check fails → logs, auto-fix Tier 2
5. Fourth health check fails → escalation triggered
   - Ahmed receives: 🚨 THESIS HEALTH ESCALATION (CHRONICLE)
   - Entry logic pauses

**Restore:**
```bash
mv /Users/ahmedsadek/nexus/data/chronicle.db.bak \
   /Users/ahmedsadek/nexus/data/chronicle.db
```

**Verify recovery:**
- Next health check succeeds
- Failure count resets to 0
- Entry logic resumes

---

### Test 4.2: Alpaca API Timeout

**Simulate:**
```bash
# Add network latency or disconnect temporarily
# OR modify thesis_health_monitor.py to use invalid endpoint for testing
```

**Expected:**
1. Alpaca health check fails with timeout
2. Auto-fix attempts reconnection
3. If persistent, escalation after 3 failures

---

### Test 4.3: Order Execution Fails

**Simulate:**
During trading validator, if order submission fails

**Expected:**
1. Validator logs error
2. Reports FAIL to Ahmed with error details
3. Attempts cleanup of any partial position
4. Ahmed can manually investigate

---

## Phase 5: Regression Tests (After Deployment)

### Daily Checklist

**Every trading day at:**

| Time | Check |
|------|-------|
| 9:30 AM | Service started, APScheduler running |
| 9:45 AM | Trading validator ran (check Telegram) |
| 10:00 AM | First health check complete (check logs) |
| 12:15 PM | Mid-session report delivered (check Telegram) |
| 3:00 PM | Health checks still running every 30 min |
| 4:15 PM | Post-close summary delivered (check Telegram) |
| 4:20 PM | All health checks complete, no escalations |

---

### Weekly Checklist

Every Monday:

```bash
# 1. Check error logs
grep ERROR /Users/ahmedsadek/nexus/logs/thesis-*.log | tail -20

# 2. Query CHRONICLE for escalations
sqlite3 /Users/ahmedsadek/nexus/data/chronicle.db << EOF
SELECT timestamp, component, message 
FROM event_log 
WHERE level = 'CRITICAL' 
  AND component IN ('thesis_health', 'thesis_status', 'trading_validator')
ORDER BY timestamp DESC 
LIMIT 10;
EOF

# 3. Check Alpaca account equity
curl -H "APCA-API-KEY-ID: PKPGM3BRNYPGCF5Z56IAUZCZJL" \
     -H "APCA-API-SECRET-KEY: 5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs" \
     https://paper-api.alpaca.markets/v2/account | jq '.equity'
```

---

## Test Results Template

**Date:** 2026-06-01

| Phase | Test | Status | Notes |
|-------|------|--------|-------|
| 1.1 | CHRONICLE Health | ✅ PASS | Connected, 42 rows |
| 1.2 | Alpaca Health | ✅ PASS | Equity: $100,000 |
| 1.3 | Status Message Format | ✅ PASS | Proper emoji/structure |
| 1.4 | Account Access | ✅ PASS | Account accessible |
| 2.1 | Full Health Suite | ✅ PASS | All components healthy |
| 2.2 | Status Integration | ✅ PASS | No format errors |
| 2.3 | Validator Dry Run | ✅ PASS | APIs responsive |
| 3.1 | Trading Validator Live | ✅ PASS | Order executed & closed |
| 3.2 | Health Check Runs | ✅ PASS | Logs show every 30 min |
| 3.3 | Mid-Session Report | ✅ PASS | Delivered to Ahmed |
| 3.4 | Post-Close Summary | ✅ PASS | Delivered to Ahmed |
| 4.1 | CHRONICLE Failure | N/A | Test on demand |
| 4.2 | Alpaca Timeout | N/A | Test on demand |
| 4.3 | Order Failure | N/A | Test on demand |

---

## Notes

- Tests should be run in order
- Phase 1 & 2 can run before market hours
- Phase 3 tests must run during market hours (9:30 AM - 4:00 PM ET)
- Phase 4 tests only if specific failures need investigation
- Phase 5 is ongoing regression testing

---

## Approval

**Configuration signed off:** Ahmed Sadek  
**Test date:** 2026-06-01  
**Production ready:** Yes
