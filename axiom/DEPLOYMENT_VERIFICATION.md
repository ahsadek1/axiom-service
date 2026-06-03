# DEPLOYMENT VERIFICATION CHECKLIST

**Target:** 192.168.1.141 (Ubuntu Axiom Node)  
**Date:** 2026-06-02  
**Status:** READY FOR EXECUTION

---

## PRE-DEPLOYMENT VERIFICATION

### Local Tests (Completed ✅)
```bash
cd /Users/ahmedsadek/nexus/axiom
python3 test_trading_fixes.py
```

**Result:** ✅ 8/8 tests passed (100% success rate)

Tests covered:
- FIX #1: Axiom service watchdog with auto-restart
- FIX #2: FRED VIXCLS caching (3-source fallback)
- FIX #3: Tiered halt gates (Level 0-3 with sizing)
- FIX #4: Alert escalation (sustained HALT threshold)
- FIX #5: Halt recovery state machine (6 states + sanity checks)
- FIX #6: EOD order reconciliation (stale order detection)
- FIX #7: Execution/signal feedback loop
- FIX #8: Health monitoring with auto-restart

### Environment Verification (Completed ✅)
```
✅ AXIOM_SECRET set
✅ NEXUS_SECRET set
✅ POLYGON_API_KEY set
✅ FRED_API_KEY set
✅ TELEGRAM_BOT_TOKEN set
✅ AHMED_CHAT_ID set
✅ ALPACA_KEY set
✅ ALPACA_SECRET set
✅ ORACLE_URL set
✅ ORACLE_SECRET set
```

### Code Quality (Completed ✅)
- [x] All 8 fixes implemented
- [x] All files syntax-checked
- [x] No circular imports
- [x] All dependencies available
- [x] Logging configured
- [x] Error handling complete

---

## DEPLOYMENT PROCEDURE

### Step 1: Sync Code Files
```bash
# Create deployment package
cd /Users/ahmedsadek/nexus/axiom

# Sync new/modified files
scp -o StrictHostKeyChecking=no \
    resilience/halt_management.py \
    test_trading_fixes.py \
    ubuntu@192.168.1.141:~/axiom/resilience/
```

### Step 2: Backup Current Configuration
```bash
ssh ubuntu@192.168.1.141 << 'EOF'
  cd ~/axiom
  cp main.py main.py.backup.$(date +%s)
  cp .env .env.backup.$(date +%s)
  echo "Backup created"
EOF
```

### Step 3: Stop Current Service
```bash
ssh ubuntu@192.168.1.141 "pkill -f 'uvicorn.*axiom' || true"
sleep 2
echo "Service stopped"
```

### Step 4: Start New Service
```bash
ssh ubuntu@192.168.1.141 << 'EOF'
  cd ~/axiom
  nohup python3 -m uvicorn main:app \
    --host 0.0.0.0 \
    --port 8001 \
    > axiom.log 2>&1 &
  
  sleep 3
  ps aux | grep -i uvicorn | grep -v grep && echo "✅ Service started" || echo "❌ Service failed"
EOF
```

### Step 5: Verify Service Health
```bash
# Check health endpoint
ssh ubuntu@192.168.1.141 "curl -s http://localhost:8001/health | jq '.status'"

# Expected: "ok" or "starting"
# Not expected: Error, timeout, or connection refused
```

### Step 6: Run Remote Tests
```bash
ssh ubuntu@192.168.1.141 << 'EOF'
  cd ~/axiom
  python3 test_trading_fixes.py 2>&1 | tail -20
EOF
```

### Step 7: Monitor Startup
```bash
# Check logs every 5 seconds for 30 seconds
for i in {1..6}; do
  ssh ubuntu@192.168.1.141 "tail -5 ~/axiom/axiom.log"
  sleep 5
done
```

---

## POST-DEPLOYMENT VERIFICATION

### Health Checks (First 5 Minutes)
```bash
# Every 30 seconds, verify service is healthy
for i in {1..10}; do
  echo "Check $i..."
  ssh ubuntu@192.168.1.141 "curl -s http://localhost:8001/health" | jq .
  sleep 30
done
```

**Expected output:**
```json
{
  "status": "ok",
  "_service_mode": "active",
  "pool_size": [...],
  "vix": [value],
  "regime": {...}
}
```

### Key Endpoint Tests

**1. Test /health endpoint:**
```bash
curl -s http://192.168.1.141:8001/health | jq '.status'
# Expected: "ok"
```

**2. Test /assess with tiered gates:**
```bash
# Create test request with different VIX levels
curl -X POST http://192.168.1.141:8001/assess \
  -H "X-Axiom-Secret: [secret]" \
  -H "Content-Type: application/json" \
  -d '{
    "ticker": "SPY",
    "vix": 25.0,
    "dte": 30,
    "strategy": "credit"
  }'

# Expected: sizing_mult=0.5 (ELEVATED level)
```

**3. Test halt gate at different VIX levels:**
```bash
# Level 0 (NORMAL): VIX < 20
curl -X POST ... -d '{"ticker": "SPY", "vix": 15.0}'
# Expected: sizing_mult=1.0

# Level 1 (ELEVATED): 20 ≤ VIX < 30
curl -X POST ... -d '{"ticker": "SPY", "vix": 25.0}'
# Expected: sizing_mult=0.5

# Level 2 (CRITICAL): 30 ≤ VIX < 50
curl -X POST ... -d '{"ticker": "SPY", "vix": 35.0}'
# Expected: sizing_mult=0.0

# Level 3 (MANUAL): VIX ≥ 50
curl -X POST ... -d '{"ticker": "SPY", "vix": 55.0}'
# Expected: sizing_mult=0.0, can_enter_new=false
```

### Monitoring Dashboard
```bash
# Create monitoring loop (runs for 10 minutes)
echo "Monitoring post-deployment for 10 minutes..."
START=$(date +%s)
while [ $(($(date +%s) - START)) -lt 600 ]; do
  echo "[$(date '+%H:%M:%S')] Health check..."
  ssh ubuntu@192.168.1.141 "curl -s http://localhost:8001/health" | jq '.status'
  sleep 30
done
echo "Monitoring complete"
```

---

## FAILURE SCENARIOS & RECOVERY

### Scenario 1: Service Fails to Start
**Symptoms:** Connection refused on port 8001

**Recovery:**
```bash
# Check logs
ssh ubuntu@192.168.1.141 "tail -30 ~/axiom/axiom.log"

# Verify Python environment
ssh ubuntu@192.168.1.141 "python3 -c 'import uvicorn; print(uvicorn.__version__)'"

# Restore backup if needed
ssh ubuntu@192.168.1.141 "cp ~/axiom/main.py.backup.* ~/axiom/main.py"
ssh ubuntu@192.168.1.141 "systemctl restart axiom" # or manual start
```

### Scenario 2: Service Starts but in STANDBY
**Symptoms:** _SERVICE_MODE = "standby" + _standby_reason

**Recovery:**
```bash
# Check standby reason
ssh ubuntu@192.168.1.141 "curl -s http://localhost:8001/health" | jq '._standby_reason'

# Common reasons:
#   - VIX data unavailable → Check FRED API key, Polygon connectivity
#   - Regime classification failed → Check data integrity, restart scheduler

# Fix:
ssh ubuntu@192.168.1.141 "pkill -f 'uvicorn.*axiom'; sleep 2; cd ~/axiom && python3 -m uvicorn main:app --host 0.0.0.0 --port 8001 &"
sleep 5
ssh ubuntu@192.168.1.141 "curl -s http://localhost:8001/health" | jq '._service_mode'
```

### Scenario 3: Tiered Gates Not Triggering
**Symptoms:** Sizing multiplier not changing with VIX

**Recovery:**
```bash
# Verify halt_management.py loaded
ssh ubuntu@192.168.1.141 "grep -n 'TieredHaltGate' ~/axiom/main.py"

# Check /assess endpoint response
curl -X POST http://192.168.1.141:8001/assess \
  -H "X-Axiom-Secret: [secret]" \
  -H "Content-Type: application/json" \
  -d '{"ticker": "SPY", "vix": 35.0}' | jq '.sizing_mult'

# If still 1.0 at VIX=35 (should be 0.0):
#   - Restart service
#   - Check logs for import errors
#   - Verify halt_management.py syntax
```

---

## ROLLBACK PROCEDURE

If deployment fails or causes issues:

```bash
# 1. Stop new service
ssh ubuntu@192.168.1.141 "pkill -f 'uvicorn.*axiom'"

# 2. Restore backup
ssh ubuntu@192.168.1.141 "cp ~/axiom/main.py.backup.* ~/axiom/main.py"

# 3. Restart old service
ssh ubuntu@192.168.1.141 "cd ~/axiom && python3 -m uvicorn main:app --host 0.0.0.0 --port 8001 &"

# 4. Verify
ssh ubuntu@192.168.1.141 "curl -s http://localhost:8001/health" | jq '.status'

# 5. Document incident
# Create incident report in CHRONICLE
```

---

## SIGN-OFF

### Deployment Readiness Checklist
- [x] All 8 fixes implemented
- [x] All tests passed (8/8)
- [x] Code quality verified
- [x] Environment variables confirmed
- [x] Backup procedure documented
- [x] Rollback procedure documented
- [x] Health checks defined
- [x] Monitoring dashboard defined
- [x] Failure scenarios documented

### Ready for Deployment: **✅ YES**

**Prerequisites Met:**
- ✅ No breaking API changes
- ✅ All fallbacks implemented
- ✅ Graceful degradation verified
- ✅ Manual override always available
- ✅ CHRONICLE logging integrated

**Estimated Deployment Time:** 10-15 minutes  
**Estimated Validation Time:** 10 minutes  
**Estimated Total Downtime:** <5 minutes (service restart)

---

**Deployment Owner:** Axiom SubAgent  
**Approval Authority:** Ahmed Sadek (SOVEREIGN)  
**Date:** 2026-06-02  
**Status:** ✅ READY FOR EXECUTION
