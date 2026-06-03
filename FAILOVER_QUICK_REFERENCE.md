# SQS FAILOVER — QUICK REFERENCE

**Status:** ✅ PRODUCTION READY  
**Components:** 5 layers  
**Subsystems Protected:** 10  
**Lines of Code:** 700+  
**Test Coverage:** 40+ test cases  

---

## QUICK START (5 MINUTES)

### 1. Copy Code
```bash
cp /Users/ahmedsadek/nexus/sqs_failover_architecture.py \
   /Users/ahmedsadek/nexus/failover/
```

### 2. Verify Dependencies
```bash
python3 -c "import asyncio, requests, boto3; print('✅ OK')"
```

### 3. Start Service
```bash
python3 /Users/ahmedsadek/nexus/failover/failover_service.py &
```

### 4. Check Logs
```bash
tail -f /Users/ahmedsadek/nexus/logs/failover.log
```

---

## 10 SUBSYSTEMS MONITORED

| # | Name | Type | Check | Degradation |
|---|------|------|-------|-------------|
| 1 | ATG_SWING | SQS | GetQueueAttributes | Level 1 |
| 2 | ATG_INTRADAY | SQS | GetQueueAttributes | Level 1 |
| 3 | AILS | SQS | GetQueueAttributes | Level 2 |
| 4 | ATM_MULTIWEEK | SQS | GetQueueAttributes | Level 1 |
| 5 | ATM_0DTE | SQS | GetQueueAttributes | Level 1 |
| 6 | AMAT_PRIME | SQS | GetQueueAttributes | Level 3 |
| 7 | ATG_BUFFER | HTTP | GET /health | Level 2 |
| 8 | MESSAGE_BROKER | TCP | Port 5672 | Level 2 |
| 9 | CAPITAL_ROUTER | HTTP | GET /health | Level 4 |
| 10 | VIX_DATA | HTTP | GET /api | Level 2 |

---

## WHAT HAPPENS WHEN COMPONENT FAILS

### Timeline

```
Time 0:00   Component DOWN
Time 0:30   FAILOVER ACTIVATED (standby trading starts)
Time 5:00   ESCALATION ALERT (if still down)
Time N:NN   Component RECOVERS
Time N:NN   RECONCILIATION (verify no duplicates)
Time N:NN   FULL OPERATION (back to primary)
```

### What Standby Algorithm Does

✅ Closes losing positions (>2% loss)  
✅ Takes winning positions (>5% gain)  
❌ NO new entries (focus on management)  
✅ Respects VIX brake (stops if VIX > 40)  
✅ Respects position limits (max 10)  
✅ Respects capital limits (max allocation)  

---

## DEGRADATION LEVELS

| Level | Size | New Entries | Example |
|-------|------|-------------|---------|
| 0 | 100% | Yes | All healthy |
| 1 | 100% | Yes | 1 minor down |
| 2 | 50% | Yes | Multiple down |
| 3 | 0% | No | AMAT_PRIME down |
| 4 | 0% | No | CAPITAL_ROUTER down → HALT |

---

## MONITORING COMMANDS

### Check System Status
```bash
python3 << 'EOF'
import sys
sys.path.insert(0, '/Users/ahmedsadek/nexus')
from sqs_failover_architecture import SQSFailoverOrchestrator
orch = SQSFailoverOrchestrator()
print(orch.get_system_status())
EOF
```

### Check Health
```bash
python3 << 'EOF'
import sys
sys.path.insert(0, '/Users/ahmedsadek/nexus')
from sqs_failover_architecture import SQSFailoverOrchestrator
orch = SQSFailoverOrchestrator()
print(orch.get_health_report())
EOF
```

### Check Failovers
```bash
python3 << 'EOF'
import sys
sys.path.insert(0, '/Users/ahmedsadek/nexus')
from sqs_failover_architecture import SQSFailoverOrchestrator
orch = SQSFailoverOrchestrator()
print(orch.get_failover_report())
EOF
```

### Watch Logs
```bash
tail -f /Users/ahmedsadek/nexus/logs/failover.log
```

---

## ALERTS

### What Gets Alerted (Telegram to Ahmed)

🔴 **FAILOVER ACTIVATED** (component down 30s)
```
Component: ATG_SWING
Down: 30.5s
Standby engaged
```

🔴🔴 **FAILOVER ESCALATION** (component down 5min)
```
Component: ATG_SWING
Down: 5.2 minutes
Reconciliation queued
```

⚠️ **DEGRADATION LEVEL 2** (multiple down)
```
Components down: ATG_SWING, ATG_INTRADAY
Degradation level: 2/4
Size: 50% | New entries: Yes
```

🚨 **EMERGENCY HALT** (CAPITAL_ROUTER down)
```
Component: CAPITAL_ROUTER
Status: DOWN
Action: All trading HALTED
```

✅ **SYSTEM RECOVERED** (all back to normal)
```
All components healthy
Degradation level: 0/4
Size: 100% | New entries: Yes
```

---

## TESTING

### Run All Tests
```bash
cd /Users/ahmedsadek/nexus
pytest tests/test_sqs_failover.py -v
```

### Run Specific Test
```bash
pytest tests/test_sqs_failover.py::TestComponentHealthMonitor -v
```

### Simulate Failure
```bash
python3 << 'EOF'
import sys, asyncio, time
sys.path.insert(0, '/Users/ahmedsadek/nexus')
from sqs_failover_architecture import FailoverTrigger, ComponentHealthMonitor, HealthSnapshot

async def test():
    monitor = ComponentHealthMonitor()
    trigger = FailoverTrigger(monitor)
    trigger.set_alert_fn(lambda msg: print(f"📨 {msg}"))
    
    # Simulate 35s down time
    trigger.component_down_since["ATG_SWING"] = time.time() - 35
    
    snapshot = HealthSnapshot(
        timestamp="test",
        checks=[],
        overall_degradation_level=1,
        components_down=["ATG_SWING"],
    )
    
    await trigger._process_snapshot(snapshot)
    print(f"Active failovers: {trigger.active_failovers}")

asyncio.run(test())
EOF
```

---

## TROUBLESHOOTING

### Service Not Starting

**Problem:** Process exits immediately  
**Fix:** Check logs
```bash
tail -20 /Users/ahmedsadek/nexus/logs/failover_service.log
```

**Common issues:**
- Missing `/Users/ahmedsadek/nexus/failover/` directory
- Missing environment variables (TELEGRAM_BOT_TOKEN)
- Python import error

### No Alerts Sent

**Problem:** Failover triggers but no Telegram message  
**Check:**
```bash
# Verify token in environment
echo $TELEGRAM_BOT_TOKEN

# Or set it
export TELEGRAM_BOT_TOKEN="YOUR_TOKEN"
```

### False Positives (Components Incorrectly Marked Down)

**Problem:** Component reports DOWN but it's actually healthy  
**Cause:** Network timeout or slow response  
**Fix:**
```python
# In sqs_failover_architecture.py, adjust timeout
"timeout_ms": 5000,  # Increase from 2000
```

### Reconciliation Fails (Duplicate Orders Found)

**Problem:** After failover, reconciliation reports duplicates  
**Investigation:**
```bash
# Check trade logs
cat /Users/ahmedsadek/nexus/logs/failover_reconciliation.log

# Manual review
python3 << 'EOF'
import sys
sys.path.insert(0, '/Users/ahmedsadek/nexus')
from sqs_failover_architecture import SQSFailoverOrchestrator
orch = SQSFailoverOrchestrator()
for report in orch.get_reconciliation_reports():
    print(report)
EOF
```

---

## KEY FILES

| File | Purpose |
|------|---------|
| `sqs_failover_architecture.py` | Main failover code (700+ lines) |
| `FAILOVER_DEPLOYMENT_CHECKLIST.md` | Step-by-step deployment guide |
| `SQS_FAILOVER_ARCHITECTURE_DESIGN.md` | Complete architecture docs |
| `FAILOVER_QUICK_REFERENCE.md` | This file (quick lookup) |
| `tests/test_sqs_failover.py` | 40+ test cases |
| `failover/failover_service.py` | Service entry point |
| `logs/failover.log` | Main log file |
| `logs/failover_service.log` | Service startup log |

---

## CONFIGURATION

### Monitoring Interval
```python
# In failover_service.py
await orchestrator.health_monitor.run_monitoring_loop(interval_seconds=10)
```

### Failover Thresholds
```python
# In sqs_failover_architecture.py
DOWN_THRESHOLD_SECONDS = 30
ESCALATION_THRESHOLD_SECONDS = 300
TRANSITION_BACK_SECONDS = 60
```

### Degradation Levels
```python
# In GracefulDegradationCoordinator
SIZE_MULTIPLIERS = {
    0: 1.00,  # Full
    1: 1.00,  # Minor
    2: 0.50,  # Moderate
    3: 0.00,  # Severe
    4: 0.00,  # Emergency
}
```

---

## SAFETY GATES (NEVER DEGRADED)

These always work, no matter what:

✅ VIX brake (stops if VIX > 40)  
✅ Position limits (max 10)  
✅ Capital limits (max allocation)  
✅ Stop losses (always enforced)  
✅ Exits (always allowed)  
✅ Circuit breakers (always trip)  

---

## DASHBOARD (Simple)

```bash
#!/bin/bash
while true; do
    clear
    echo "=== SQS FAILOVER STATUS ==="
    python3 << 'EOF'
import sys
sys.path.insert(0, '/Users/ahmedsadek/nexus')
from sqs_failover_architecture import SQSFailoverOrchestrator
orch = SQSFailoverOrchestrator()
status = orch.get_system_status()
print(f"Degradation: {status['degradation_level']}/4")
print(f"Trading: {'ALLOWED' if status['is_trading_allowed'] else 'HALTED'}")
print(f"Size: {status['size_multiplier']:.0%}")
health = orch.get_health_report()
print(f"Healthy: {health['healthy_components']}/{health['total_components']}")
if health['components']:
    print("Down: " + ", ".join(health['components_down']))
EOF
    sleep 5
done
```

---

## METRICS TO MONITOR

- **Failover count** (should be 0 normally)
- **Failover duration** (target: <5 min recovery)
- **Reconciliation success rate** (target: 100%)
- **False positive rate** (target: <5%)
- **Alert latency** (target: <2s to Telegram)

---

## SUCCESS CRITERIA (Post-Deployment)

✅ All 10 subsystems monitored every 10s  
✅ Failover triggers within 30s of failure  
✅ Standby algorithm starts automatically  
✅ Graceful degradation adjusts size  
✅ Reconciliation detects duplicates  
✅ Alerts arrive on Telegram  
✅ Capital Router survives other failures  
✅ VIX brake works in all levels  
✅ No position doubling  
✅ System recovers to full operation  

---

## EMERGENCY PROCEDURES

### Stop the Failover Service
```bash
pkill -f failover_service.py
```

### Disable (if problematic)
```bash
systemctl --user disable nexus-failover-monitor.service
systemctl --user stop nexus-failover-monitor.service
```

### Restart
```bash
python3 /Users/ahmedsadek/nexus/failover/failover_service.py &
```

### Check if Running
```bash
ps aux | grep failover_service.py | grep -v grep
```

---

## CONTACT

**Issues/Questions:**
- Check `/Users/ahmedsadek/nexus/logs/failover.log`
- Alert Ahmed immediately if Level 4 (CAPITAL_ROUTER down)
- Never override VIX brake (hard gate)

**Maintenance:**
- Review reconciliation reports weekly
- Adjust thresholds if false positives exceed 5%
- Monitor log file size (rotate at 100MB)

---

**Version:** 1.0  
**Date:** 2026-06-02  
**Mandate:** Zero trading halts due to single component failures  
**Status:** ✅ PRODUCTION READY
