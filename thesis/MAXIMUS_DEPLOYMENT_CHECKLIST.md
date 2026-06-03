# MAXIMUS DEPLOYMENT CHECKLIST

**Status:** Ready to Deploy  
**Date:** 2026-06-01  
**Target:** Thesis Service on http://localhost:8060

---

## PRE-DEPLOYMENT VERIFICATION

### ✅ Files Verified
- [x] `thesis_cron_config.py` — 244 lines, imports OK
- [x] `thesis_health_monitor.py` — 594 lines, imports OK
- [x] `thesis_status_reporter.py` — 487 lines, imports OK
- [x] `thesis_trading_validator.py` — 452 lines, imports OK
- [x] `main.py` — updated with MAXIMUS integration
- [x] `.env` — has ALPACA_KEY, ALPACA_SECRET, AXIOM_BOT_TOKEN

### ✅ Code Quality
- [x] All Python files syntax valid (`python -m py_compile`)
- [x] All imports successful (tested with `from ... import`)
- [x] No circular dependencies
- [x] Type hints present
- [x] Error handling in place
- [x] Logging configured

### ✅ Configuration
- [x] Alpaca credentials in `.env`
- [x] Telegram bot token in `.env`
- [x] Chronicle DB path configured
- [x] Thesis service URL configured
- [x] Time zone (America/New_York) set

---

## DEPLOYMENT STEPS

### Step 1: Verify Service Not Running (1 min)

```bash
# Kill any existing Thesis process
pkill -f "python.*thesis.*main.py" 2>/dev/null || true
sleep 3

# Verify killed
ps aux | grep thesis | grep -v grep
# Should show nothing
```

**Expected output:** No running process  
**Fail condition:** Process still running

---

### Step 2: Start Thesis Service (2 min)

```bash
cd /Users/ahmedsadek/nexus/thesis
python main.py > /tmp/thesis.log 2>&1 &
sleep 5

# Verify started
ps aux | grep "python.*thesis.*main.py" | grep -v grep
```

**Expected output:** 
```
ahmedsadek  12345  0.5  2.3  456789  123456   ??  Ss   21:45PM   0:10 python main.py
```

**Fail condition:** No process found

---

### Step 3: Check Startup Logs (2 min)

```bash
# View startup logs
tail -100 /tmp/thesis.log

# Look for these messages:
# - "CHRONICLE validated and tables writable"
# - "✅ MAXIMUS monitoring jobs configured"
# - "✓ Job scheduled: Daily trading ability validation (9:45 AM ET)"
# - "✓ Job scheduled: Health check (every 30 min)"
# - "✓ Job scheduled: Mid-session update (12:15 PM ET)"
# - "✓ Job scheduled: Post-close summary (4:15 PM ET)"
# - "THESIS service started on port 8060"
```

**Success criteria:** All MAXIMUS job messages present  
**Fail condition:** "MAXIMUS monitoring jobs configured" not found OR "Failed to configure"

---

### Step 4: Verify Health Endpoint (1 min)

```bash
# Test service health
curl http://localhost:8060/thesis/health 2>/dev/null | jq '.'

# Should return JSON with:
# - "scheduler_ok": true
# - "chronicle_ok": true
# - Other health checks
```

**Expected output:**
```json
{
  "status": "ok",
  "scheduler_ok": true,
  "chronicle_ok": true,
  "timestamp": "2026-06-01T21:45:00"
}
```

**Fail condition:** `scheduler_ok` is false OR endpoint doesn't respond

---

### Step 5: Verify APScheduler Jobs (1 min)

```bash
# Check that jobs were added to scheduler
grep "Job scheduled" /tmp/thesis.log

# Should see exactly 7 jobs:
# 1. ✓ Weekly thesis (existing)
# 2. ✓ Daily brief (existing)
# 3. ✓ Real-time monitor (existing)
# 4. ✓ Daily trading ability validation (MAXIMUS)
# 5. ✓ Health check (MAXIMUS)
# 6. ✓ Mid-session update (MAXIMUS)
# 7. ✓ Post-close summary (MAXIMUS)
```

**Expected output:**
```
✓ Job scheduled: Daily thesis generation (Sun 6:00 PM)
✓ Job scheduled: Daily brief (Mon-Fri 7:00 AM)
✓ Job scheduled: Real-time monitor (every 5 min)
✓ Job scheduled: Daily trading ability validation (9:45 AM ET)
✓ Job scheduled: Health check (every 30 min)
✓ Job scheduled: Mid-session update (12:15 PM ET)
✓ Job scheduled: Post-close summary (4:15 PM ET)
```

**Fail condition:** Any job missing or "Failed to configure"

---

### Step 6: Verify Logging Files Created (1 min)

```bash
# Check that log files are created
ls -la /Users/ahmedsadek/nexus/logs/thesis-*.log

# Should see:
# - thesis-health.log (exists and has content)
# - thesis-status.log (exists)
# - thesis-trading-validator.log (exists)
```

**Expected output:**
```
-rw-r--r--  1 ahmedsadek  staff  12345 Jun  1 21:45 thesis-health.log
-rw-r--r--  1 ahmedsadek  staff    567 Jun  1 21:45 thesis-status.log
-rw-r--r--  1 ahmedsadek  staff    890 Jun  1 21:45 thesis-trading-validator.log
```

**Fail condition:** Log files don't exist

---

### Step 7: Test Health Check Trigger (optional, ~35 min)

This is optional — health checks run every 30 minutes.  
To test immediately, manually trigger:

```bash
# Wait for health check to run (within 30 minutes of startup)
# Or test manually:
python3 << 'EOF'
import asyncio
import sys
sys.path.insert(0, '/Users/ahmedsadek/nexus/thesis')

from thesis_health_monitor import ThesisHealthMonitor

monitor = ThesisHealthMonitor(
    alpaca_key="PKPGM3BRNYPGCF5Z56IAUZCZJL",
    alpaca_secret="5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs",
    telegram_token="8664199571:AAF5H9XFvhhjcbAiLuV-qul2humI3yWO2fo"
)

result = asyncio.run(monitor.run_health_check())
print(f"Health check result: {result['overall_status']}")
EOF
```

**Expected output:** `Health check result: healthy`

---

## RUNTIME VERIFICATION

### During Trading Hours (9:30 AM - 4:00 PM ET)

#### 9:45 AM — Trading Validator Runs
```bash
# Check trading validator logs
tail /Users/ahmedsadek/nexus/logs/thesis-trading-validator.log

# Should show:
# [INFO] Running daily trading ability validation...
# [INFO] Account check passed
# [INFO] Order submission successful
# [INFO] Position verification passed
# [INFO] Position closure passed
# [INFO] Trading validation PASS
```

**Success:** All steps pass  
**Failure:** Any step shows error → Alert sent to Ahmed immediately

---

#### Every 30 Minutes — Health Check Runs
```bash
# Watch health logs
tail -f /Users/ahmedsadek/nexus/logs/thesis-health.log

# Should show every 30 min:
# 2026-06-01 10:30:00 [INFO] thesis_health: [JOB] Running health check...
# 2026-06-01 10:30:05 [INFO] thesis_health: [JOB] Health check complete: healthy
```

**Success:** Logs appear at regular 30-min intervals  
**Failure:** No logs OR status shows "critical" → Check details

---

#### 12:15 PM — Mid-Session Update Sent
```bash
# Check status logs
tail /Users/ahmedsadek/nexus/logs/thesis-status.log

# Should show:
# [INFO] Sending mid-session update...
# [INFO] Collected trades: 3
# [INFO] Collected positions: 1
# [INFO] Sent mid-session update to Ahmed
```

**Success:** Message appears in Ahmed's Telegram DM  
**Failure:** Log shows error OR no message arrives

---

#### 4:15 PM — Post-Close Summary Sent
```bash
# Check status logs (same file)
tail /Users/ahmedsadek/nexus/logs/thesis-status.log

# Should show:
# [INFO] Sending post-close summary...
# [INFO] Daily stats collected
# [INFO] Sent post-close summary to Ahmed
```

**Success:** Message appears in Ahmed's Telegram DM  
**Failure:** Log shows error OR no message arrives

---

## FAILURE HANDLING

### If Service Fails to Start

```bash
# Check startup logs for errors
tail -200 /tmp/thesis.log | grep -E "Error|Failed|Exception|Traceback"

# Common issues:
# 1. Port 8060 already in use
#    → pkill -f "python.*thesis"
#    → Check for other services: lsof -i :8060

# 2. CHRONICLE not accessible
#    → Check database: ls -la /Users/ahmedsadek/nexus/data/chronicle.db
#    → Verify permissions: sudo sqlite3 /Users/ahmedsadek/nexus/data/chronicle.db "SELECT 1"

# 3. Missing environment variables
#    → grep ALPACA_KEY /Users/ahmedsadek/nexus/thesis/.env
#    → Check for typos in .env
```

---

### If MAXIMUS Jobs Don't Configure

```bash
# Check logs for configuration errors
grep -i "MAXIMUS\|monitoring configuration\|failed" /tmp/thesis.log

# Verify imports work
cd /Users/ahmedsadek/nexus/thesis
python3 -c "from thesis_cron_config import apply_monitoring_configuration; print('OK')"

# Check main.py for integration
grep "apply_monitoring_configuration" /Users/ahmedsadek/nexus/thesis/main.py
# Should show the call

# If still failing:
# 1. Check APScheduler version: pip show APScheduler
# 2. Check for scheduler initialization: grep "_scheduler = " /tmp/thesis.log
# 3. Verify credentials in .env aren't corrupted
```

---

### If Health Checks Don't Run

```bash
# Verify time is within trading hours (9:30 AM - 4:00 PM ET)
date

# If within hours, check logs
tail -50 /Users/ahmedsadek/nexus/logs/thesis-health.log

# Check if APScheduler is running
curl http://localhost:8060/thesis/health | jq '.scheduler_ok'
# Should be true

# Manual trigger to test
python3 -c "
import asyncio, sys
sys.path.insert(0, '/Users/ahmedsadek/nexus/thesis')
from thesis_health_monitor import ThesisHealthMonitor
m = ThesisHealthMonitor(alpaca_key='...', alpaca_secret='...')
print(asyncio.run(m.run_health_check()))
"
```

---

### If Telegram Messages Don't Arrive

```bash
# Verify bot token is correct
grep AXIOM_BOT_TOKEN /Users/ahmedsadek/nexus/thesis/.env

# Test Telegram API directly
curl -X POST "https://api.telegram.org/bot8664199571:AAF5H9XFvhhjcbAiLuV-qul2humI3yWO2fo/sendMessage" \
  -H "Content-Type: application/json" \
  -d '{"chat_id":"8573754783","text":"Test message"}'

# Should return:
# {"ok":true,"result":{"message_id":...}}

# If this fails:
# 1. Check internet connectivity
# 2. Verify bot token hasn't been revoked (@BotFather)
# 3. Verify Ahmed's chat ID (should be 8573754783)
# 4. Check Telegram spam settings
```

---

## ROLLBACK PROCEDURE

If MAXIMUS needs to be disabled:

```bash
# 1. Comment out the integration in main.py
# Find and comment these lines (around line 285):
#   maximus_success = apply_monitoring_configuration(...)
#   if not maximus_success: ...

# 2. Restart service
pkill -f "python.*thesis.*main.py"
sleep 3
cd /Users/ahmedsadek/nexus/thesis && python main.py > /tmp/thesis.log 2>&1 &

# 3. Verify only 3 original jobs run (not 7)
grep "Job scheduled" /tmp/thesis.log
# Should see only:
# - Weekly thesis
# - Daily brief
# - Real-time monitor
```

---

## POST-DEPLOYMENT MONITORING

### Daily Checks

```bash
# 1. Check that all jobs run
grep "Job scheduled" /tmp/thesis.log | wc -l
# Should be 7

# 2. Monitor health logs for patterns
tail -20 /Users/ahmedsadek/nexus/logs/thesis-health.log | grep -E "healthy|critical|recovering"

# 3. Check for escalations
sqlite3 /Users/ahmedsadek/nexus/data/chronicle.db \
  "SELECT COUNT(*) FROM event_log WHERE level='CRITICAL' AND component LIKE 'thesis_%';"

# 4. Verify Telegram messages arrived (check Ahmed's DM)
# Messages should appear at 12:15 PM and 4:15 PM
```

---

## SIGN-OFF CHECKLIST

After deployment, verify and initial:

- [ ] **Service Running** — `ps aux | grep thesis`
- [ ] **MAXIMUS Jobs Configured** — 4 jobs appear in startup log
- [ ] **Health Endpoint OK** — `curl http://localhost:8060/thesis/health`
- [ ] **Log Files Created** — 3 thesis-*.log files exist
- [ ] **No Errors in Logs** — `tail /tmp/thesis.log | grep -i error` → no output
- [ ] **Health Check Logged** — Wait 30 min OR test manually
- [ ] **Trading Validator Ran** — Check 9:45 AM (next trading day)
- [ ] **Telegram Messages Arrived** — Check 12:15 PM & 4:15 PM
- [ ] **CHRONICLE Accessible** — Database can be queried
- [ ] **Production Ready** — All checks pass, no issues

---

## DEPLOYMENT TIMELINE

| Step | Duration | Cumulative | Status |
|------|----------|-----------|--------|
| Verify files & config | 2 min | 2 min | ✅ |
| Stop old service | 1 min | 3 min | Ready |
| Start new service | 2 min | 5 min | Ready |
| Check startup logs | 2 min | 7 min | Ready |
| Verify health endpoint | 1 min | 8 min | Ready |
| Verify jobs loaded | 1 min | 9 min | Ready |
| Verify log files | 1 min | 10 min | Ready |
| **TOTAL DEPLOYMENT** | **~10 min** | | Ready |

**Runtime verification:** Ongoing (health checks, status messages, trading validation)

---

## SUPPORT CONTACTS

**For issues:**
1. Check logs: `/Users/ahmedsadek/nexus/logs/thesis-*.log`
2. Review CHRONICLE: `/Users/ahmedsadek/nexus/data/chronicle.db`
3. Contact Ahmed: Telegram DM or `@Axiom15Bot`

**Documentation:**
- `MAXIMUS_QUICKSTART.md` — Quick reference
- `MAXIMUS_DEPLOYMENT.md` — Detailed configuration
- `MAXIMUS_TESTING_PLAN.md` — Testing procedures
- `MAXIMUS_EXECUTION_SUMMARY.md` — What was implemented

---

**Status:** ✅ Ready to Deploy  
**Date:** 2026-06-01  
**Configuration:** Ahmed Sadek  
**Quality Check:** All systems verified
