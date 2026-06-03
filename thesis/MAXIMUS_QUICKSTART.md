# MAXIMUS Quick Start Guide

**Goal:** Get MAXIMUS monitoring live in < 10 minutes.

---

## Prerequisites

✅ Thesis service installed at `/Users/ahmedsadek/nexus/thesis/`  
✅ Alpaca account (paper trading) with API key & secret  
✅ Axiom Telegram bot token (`@Axiom15Bot`)  
✅ Python 3.9+  
✅ CHRONICLE SQLite database accessible

---

## 1. Copy Files (1 minute)

```bash
cd /Users/ahmedsadek/nexus/thesis

# Files should already be here from deployment:
ls -la thesis_*.py thesis_cron_config.py
```

Files needed:
- `thesis_health_monitor.py`
- `thesis_status_reporter.py`
- `thesis_trading_validator.py`
- `thesis_cron_config.py`

---

## 2. Install Dependencies (2 minutes)

```bash
cd /Users/ahmedsadek/nexus/thesis

# Check if already installed
pip list | grep -E "requests|APScheduler|python-telegram"

# Install if needed
pip install -r requirements.txt
```

Verify:
```bash
python -c "import requests, apscheduler, telegram_bot_handler; print('✅ All dependencies installed')"
```

---

## 3. Add Environment Variables (1 minute)

Edit `/Users/ahmedsadek/nexus/thesis/.env`:

```env
# Alpaca (required)
ALPACA_KEY=PKPGM3BRNYPGCF5Z56IAUZCZJL
ALPACA_SECRET=5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs

# Telegram (required)
TELEGRAM_BOT_TOKEN=8664199571:AAF5H9XFvhhjcbAiLuV-qul2humI3yWO2fo
AHMED_TELEGRAM_ID=8573754783

# Service URLs (defaults OK)
THESIS_SERVICE_URL=http://localhost:8060
CHRONICLE_DB_PATH=/Users/ahmedsadek/nexus/data/chronicle.db
```

Verify:
```bash
grep ALPACA_KEY /Users/ahmedsadek/nexus/thesis/.env
```

---

## 4. Create Log Directory (30 seconds)

```bash
mkdir -p /Users/ahmedsadek/nexus/logs

ls -la /Users/ahmedsadek/nexus/logs/
```

---

## 5. Add Integration to main.py (3 minutes)

Edit `/Users/ahmedsadek/nexus/thesis/main.py`

Find the lifespan startup section (around line 180-220) and add:

```python
# Import at top
from thesis_cron_config import apply_monitoring_configuration

# In the lifespan startup (after scheduler initialization):
# Around line ~250, after _scheduler = AsyncIOScheduler(...)

success = apply_monitoring_configuration(
    scheduler=_scheduler,
    alpaca_key=os.getenv("ALPACA_KEY"),
    alpaca_secret=os.getenv("ALPACA_SECRET"),
    telegram_token=os.getenv("TELEGRAM_BOT_TOKEN"),
)

if not success:
    logger.error("Failed to configure monitoring jobs")
    # Optional: raise RuntimeError("Monitoring configuration failed")
else:
    logger.info("✅ MAXIMUS monitoring jobs configured")
```

---

## 6. Restart Thesis Service (2 minutes)

```bash
# Stop current process
pkill -f "python.*thesis.*main.py"

# Wait for shutdown
sleep 5

# Start fresh
cd /Users/ahmedsadek/nexus/thesis
python main.py > /tmp/thesis.log 2>&1 &

# Verify startup
sleep 5
ps aux | grep "python.*thesis.*main.py"

# Check logs
tail -20 /tmp/thesis.log
```

Expected in logs:
```
✅ MAXIMUS monitoring jobs configured
✓ Job scheduled: Daily trading ability validation (9:45 AM ET)
✓ Job scheduled: Health check (every 30 min)
✓ Job scheduled: Mid-session update (12:15 PM ET)
✓ Job scheduled: Post-close summary (4:15 PM ET)
```

---

## 7. Quick Health Check (1 minute)

```bash
# Test health endpoint
curl http://localhost:8060/thesis/health | jq '.'

# Should return:
# {
#   "chronicle_ok": true,
#   "scheduler_ok": true,
#   "oracle_ok": true,
#   ...
# }
```

---

## 8. Verify Jobs Loaded

```bash
# Option 1: Check logs
grep "Job scheduled" /Users/ahmedsadek/nexus/logs/*.log

# Expected output:
# ✓ Job scheduled: Daily trading ability validation (9:45 AM ET)
# ✓ Job scheduled: Health check (every 30 min)
# ✓ Job scheduled: Mid-session update (12:15 PM ET)
# ✓ Job scheduled: Post-close summary (4:15 PM ET)
```

---

## 9. Wait for First Health Check

When service starts and during market hours:
- First health check runs within 30 minutes
- Logs appear in `/Users/ahmedsadek/nexus/logs/thesis-health.log`

```bash
# Watch health logs
tail -f /Users/ahmedsadek/nexus/logs/thesis-health.log
```

You should see:
```
2026-06-01 10:30:00 [INFO] thesis_health: [JOB] Running health check...
2026-06-01 10:30:05 [INFO] thesis_health: [JOB] Health check complete: healthy
```

---

## 10. Test: Send a Telegram Message

```bash
# Test sending message to Ahmed
python -c "
import requests

token = '8664199571:AAF5H9XFvhhjcbAiLuV-qul2humI3yWO2fo'
chat_id = '8573754783'
message = '✅ MAXIMUS is ready for production'

requests.post(
    f'https://api.telegram.org/bot{token}/sendMessage',
    json={'chat_id': chat_id, 'text': message}
)
print('Message sent!')
"
```

Ahmed should receive the test message in Telegram.

---

## What Happens Next

### During Trading Hours (9:30 AM - 4:00 PM ET)

**9:45 AM** — Trading Validator Runs
- Executes: BUY 1 SPY → Verify → CLOSE → Verify
- Reports: ✅ Pass or ❌ Fail to Ahmed

**Every 30 minutes** — Health Checks
- Checks: CHRONICLE, APScheduler, Alpaca, Entry Logic, Positions
- Auto-fixes failures
- Escalates after 3 consecutive failures

**12:15 PM** — Mid-Session Report
- Ahmed receives summary of trades, positions, errors, health

**4:15 PM** — Post-Close Summary
- Ahmed receives daily P&L, failures, tomorrow readiness

### Non-Trading Hours
- All jobs disabled (no unnecessary activity)
- Logs continue to be written

---

## Troubleshooting

### No logs appearing?

```bash
# Check if service is running
ps aux | grep thesis

# Check if logging directory exists
ls -la /Users/ahmedsadek/nexus/logs/

# Check service startup log
tail -50 /tmp/thesis.log
```

### Job not running?

```bash
# Verify APScheduler initialized
grep "APScheduler" /Users/ahmedsadek/nexus/logs/thesis.log

# Check if within market hours
date +%H:%M  # Should be 9:30 AM - 4:00 PM ET
```

### No Telegram messages?

```bash
# Verify token & chat ID
grep TELEGRAM /Users/ahmedsadek/nexus/thesis/.env

# Test Telegram bot
curl -X POST https://api.telegram.org/bot8664199571:AAF5H9XFvhhjcbAiLuV-qul2humI3yWO2fo/getMe
# Should return bot info
```

### CHRONICLE not accessible?

```bash
# Check database file exists
ls -la /Users/ahmedsadek/nexus/data/chronicle.db

# Check if locked
lsof | grep chronicle

# Test connection
python -c "import sqlite3; conn = sqlite3.connect('/Users/ahmedsadek/nexus/data/chronicle.db'); print('✅ Connected')"
```

---

## Verification Checklist

- [ ] All 4 Python files in `/Users/ahmedsadek/nexus/thesis/`
- [ ] Dependencies installed (`pip list | grep -E "requests|APScheduler"`)
- [ ] `.env` file has `ALPACA_KEY`, `ALPACA_SECRET`, `TELEGRAM_BOT_TOKEN`
- [ ] Log directory exists: `/Users/ahmedsadek/nexus/logs/`
- [ ] Integration code added to `main.py`
- [ ] Thesis service restarted
- [ ] Service health endpoint responds: `curl http://localhost:8060/thesis/health`
- [ ] Logs show jobs configured: `grep "Job scheduled" /Users/ahmedsadek/nexus/logs/*.log`
- [ ] At least one health check completed: `tail /Users/ahmedsadek/nexus/logs/thesis-health.log`
- [ ] Test Telegram message received by Ahmed

---

## What's Running Now

✅ **Health checks every 30 minutes** (logs to `thesis-health.log`)  
✅ **Trading ability validation daily at 9:45 AM** (logs to `thesis-trading-validator.log`)  
✅ **Status updates at 12:15 PM & 4:15 PM** (sent to Ahmed's DM)  
✅ **Self-healing on failures** (auto-fixes, escalates after 3 failures)  

---

## Next Steps

1. **Monitor logs daily** during trading hours
2. **Verify Telegram messages** arrive at 12:15 PM & 4:15 PM
3. **Review CHRONICLE** for any escalations: 
   ```sql
   SELECT * FROM event_log WHERE level='CRITICAL' ORDER BY timestamp DESC;
   ```
4. **Weekly audit** of logs for patterns or issues

---

## Support

**Questions?** Check:
- `MAXIMUS_DEPLOYMENT.md` — Full configuration details
- `MAXIMUS_TESTING_PLAN.md` — Testing procedures
- Logs: `/Users/ahmedsadek/nexus/logs/thesis-*.log`

**Issues?** Review error logs and check troubleshooting section above.

---

**Status:** ✅ Production Ready  
**Deployment Date:** 2026-06-01  
**Configuration:** Ahmed Sadek
