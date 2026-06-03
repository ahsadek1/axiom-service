# MAXIMUS — Thesis Autonomous Monitoring & Self-Healing

**Status:** ✅ Production Ready  
**Version:** 1.0.0  
**Deployment Date:** 2026-06-01  
**Last Updated:** 2026-06-01

---

## 📖 START HERE

**New to MAXIMUS?** Read in this order:

1. **[MAXIMUS_QUICK_REFERENCE.md](./MAXIMUS_QUICK_REFERENCE.md)** (5 min)
   - Quick commands to start/stop/monitor the system
   - Basic troubleshooting
   - What to expect during normal operation

2. **[MAXIMUS_QUICKSTART.md](./MAXIMUS_QUICKSTART.md)** (10 min)
   - Complete setup guide
   - Step-by-step verification
   - What to watch for

3. **[MAXIMUS_DEPLOYMENT_CHECKLIST.md](./MAXIMUS_DEPLOYMENT_CHECKLIST.md)** (detailed)
   - Pre-deployment verification
   - Post-deployment sign-off
   - Failure handling procedures

---

## 🎯 What MAXIMUS Does

MAXIMUS automatically monitors your Thesis system and keeps Ahmed informed:

### Daily at 9:45 AM (Pre-Market)
**Trading Ability Test** — Verifies the system can execute trades
- Buy 1 share of SPY
- Verify position appears in account
- Close the position
- Report: ✅ Pass or 🔴 Fail to Ahmed's Telegram

### Every 30 Minutes (During Trading Hours)
**Health Check & Self-Healing** — Ensures everything is working
- Checks: Database, Scheduler, Alpaca API, Entry Logic, Positions
- If something breaks: Auto-fixes 3 times before escalating
- Logs every check to file

### Daily at 12:15 PM
**Mid-Session Update** — Ahmed gets a status snapshot
- Trades executed so far
- Open positions & unrealized P&L
- Any errors or warnings
- System health status
- Sent to Ahmed's Telegram DM

### Daily at 4:15 PM (After Close)
**Post-Close Summary** — Daily wrap-up
- Total trades executed
- Total P&L
- Best & worst trade
- Failure summary
- Tomorrow readiness assessment
- Sent to Ahmed's Telegram DM

---

## 📁 Core Components

### 1. Job Scheduler (`thesis_cron_config.py`)
- Registers all 4 MAXIMUS jobs with APScheduler
- Handles timing and triggers
- Integrates with main.py startup

### 2. Health Monitor (`thesis_health_monitor.py`)
- Runs every 30 minutes
- Checks 5 critical components
- Auto-heals on failure
- Escalates after 3 failures

### 3. Status Reporter (`thesis_status_reporter.py`)
- Collects trade data from CHRONICLE
- Gets position data from Alpaca
- Formats messages for Telegram
- Sends at 12:15 PM & 4:15 PM

### 4. Trading Validator (`thesis_trading_validator.py`)
- Runs daily at 9:45 AM
- Tests complete order lifecycle
- Reports result to Ahmed
- Escalates on any failure

---

## 🚀 Quick Start

### 1. Verify Files Exist

```bash
cd /Users/ahmedsadek/nexus/thesis
ls thesis_cron_config.py thesis_health_monitor.py thesis_status_reporter.py thesis_trading_validator.py
# Should show all 4 files
```

### 2. Check Configuration

```bash
grep -E "ALPACA_KEY|AXIOM_BOT_TOKEN" /Users/ahmedsadek/nexus/thesis/.env
# Should show both set
```

### 3. Start Thesis Service

```bash
pkill -f "python.*thesis.*main.py" 2>/dev/null || true
sleep 3
cd /Users/ahmedsadek/nexus/thesis
python main.py > /tmp/thesis.log 2>&1 &
sleep 5
```

### 4. Verify Startup

```bash
# Check for MAXIMUS configuration
tail /tmp/thesis.log | grep "MAXIMUS\|Job scheduled"

# Verify service is healthy
curl http://localhost:8060/thesis/health | jq '.scheduler_ok'
```

**Expected:** Shows "✅ MAXIMUS monitoring jobs configured" and 7 total jobs

---

## 📊 Monitor System Health

### View Real-Time Health Checks

```bash
tail -f /Users/ahmedsadek/nexus/logs/thesis-health.log
```

You should see entries every 30 minutes during trading hours:
```
2026-06-01 10:30:00 [INFO] [JOB] Running health check...
2026-06-01 10:30:05 [INFO] [JOB] Health check complete: healthy
```

### View Status Reports

```bash
tail /Users/ahmedsadek/nexus/logs/thesis-status.log
```

You should see entries at 12:15 PM and 4:15 PM:
```
2026-06-01 12:15:00 [INFO] Sending mid-session update...
2026-06-01 12:15:02 [INFO] Sent mid-session update to Ahmed
```

### View Trading Validation

```bash
tail /Users/ahmedsadek/nexus/logs/thesis-trading-validator.log
```

You should see daily entries at 9:45 AM:
```
2026-06-01 09:45:00 [INFO] Running daily trading ability validation...
2026-06-01 09:45:05 [INFO] Trading validation PASS
```

---

## 🔧 Configuration

All settings are in `/Users/ahmedsadek/nexus/thesis/.env`:

```env
# Alpaca (required)
ALPACA_KEY=PKPGM3BRNYPGCF5Z56IAUZCZJL
ALPACA_SECRET=5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs

# Telegram (required)
AXIOM_BOT_TOKEN=8664199571:AAF5H9XFvhhjcbAiLuV-qul2humI3yWO2fo
AHMED_CHAT_ID=8573754783

# Database (required)
CHRONICLE_DB_PATH=/Users/ahmedsadek/nexus/data/chronicle.db

# Service (required)
THESIS_PORT=8060
THESIS_HOST=0.0.0.0
```

---

## 📋 Documentation Index

| Document | Purpose | When to Read |
|----------|---------|--------------|
| [MAXIMUS_QUICK_REFERENCE.md](./MAXIMUS_QUICK_REFERENCE.md) | Quick reference card | First (5 min) |
| [MAXIMUS_QUICKSTART.md](./MAXIMUS_QUICKSTART.md) | Setup guide | Second (10 min) |
| [MAXIMUS_DEPLOYMENT.md](./MAXIMUS_DEPLOYMENT.md) | Full configuration | Technical setup |
| [MAXIMUS_DEPLOYMENT_CHECKLIST.md](./MAXIMUS_DEPLOYMENT_CHECKLIST.md) | Verification steps | Before deployment |
| [MAXIMUS_EXECUTION_SUMMARY.md](./MAXIMUS_EXECUTION_SUMMARY.md) | What was implemented | Implementation review |
| [MAXIMUS_TESTING_PLAN.md](./MAXIMUS_TESTING_PLAN.md) | Testing procedures | Manual testing |
| [MAXIMUS_COMPLETION_REPORT.md](./MAXIMUS_COMPLETION_REPORT.md) | Project report | Project overview |
| [MAXIMUS_README.md](./MAXIMUS_README.md) | This file | Quick navigation |

---

## 🆘 Troubleshooting

### Service won't start?

```bash
# Check for port conflicts
lsof -i :8060

# Check for errors in startup log
tail -100 /tmp/thesis.log | grep -i error

# Try starting again
cd /Users/ahmedsadek/nexus/thesis && python main.py
```

### No health checks in logs?

```bash
# Verify it's during trading hours (9:30 AM - 4:00 PM ET)
date

# Check if scheduler is running
curl http://localhost:8060/thesis/health | jq '.scheduler_ok'
```

### No Telegram messages?

```bash
# Verify credentials
grep -E "AXIOM_BOT_TOKEN|8573754783" /Users/ahmedsadek/nexus/thesis/.env

# Test Telegram directly
curl -X POST "https://api.telegram.org/bot8664199571:AAF5H9XFvhhjcbAiLuV-qul2humI3yWO2fo/getMe"
```

### CHRONICLE not accessible?

```bash
# Check database exists
ls -la /Users/ahmedsadek/nexus/data/chronicle.db

# Test connection
python3 -c "import sqlite3; conn = sqlite3.connect('/Users/ahmedsadek/nexus/data/chronicle.db'); print('✅ OK')"
```

See [MAXIMUS_DEPLOYMENT_CHECKLIST.md](./MAXIMUS_DEPLOYMENT_CHECKLIST.md) for detailed troubleshooting.

---

## 🎯 Key Features

✅ **Autonomous** — No manual monitoring needed  
✅ **Transparent** — Daily Telegram updates keep Ahmed informed  
✅ **Self-Healing** — Auto-fixes failures before escalating  
✅ **Reliable** — 3-tier auto-healing before manual intervention  
✅ **Logged** — Every check and action is logged for audit trail  
✅ **Simple** — One command to start, runs in background  

---

## 📞 Support

**Having issues?**

1. Check [MAXIMUS_QUICK_REFERENCE.md](./MAXIMUS_QUICK_REFERENCE.md) troubleshooting section
2. Review the relevant log file:
   - Health issues → `/Users/ahmedsadek/nexus/logs/thesis-health.log`
   - Status report issues → `/Users/ahmedsadek/nexus/logs/thesis-status.log`
   - Trading test issues → `/Users/ahmedsadek/nexus/logs/thesis-trading-validator.log`
3. Check [MAXIMUS_DEPLOYMENT_CHECKLIST.md](./MAXIMUS_DEPLOYMENT_CHECKLIST.md) failure handling section
4. Review startup log → `/tmp/thesis.log`

---

## 🔄 Maintenance

### Daily
- Check Telegram messages arrive at 12:15 PM & 4:15 PM
- Scan logs for "CRITICAL" or "ERROR" entries
- Verify health checks running every 30 minutes

### Weekly
- Review CHRONICLE event_log for escalations
- Check trading validator passes daily at 9:45 AM
- Review any auto-healing actions taken

### Monthly
- Archive old log files
- Review patterns in health checks
- Check for any systemic issues

---

## 🚨 Emergency Procedures

### Stop MAXIMUS (but keep Thesis running)

```bash
# Comment out in main.py:
# maximus_success = apply_monitoring_configuration(...)

# Then restart
pkill -f "python.*thesis.*main.py"
cd /Users/ahmedsadek/nexus/thesis && python main.py > /tmp/thesis.log 2>&1 &
```

### Full system restart

```bash
# Stop everything
pkill -f "python.*thesis.*main.py"
sleep 5

# Start fresh
cd /Users/ahmedsadek/nexus/thesis && python main.py > /tmp/thesis.log 2>&1 &

# Verify startup
sleep 5
curl http://localhost:8060/thesis/health
```

---

## 📊 System Requirements

- Python 3.9+
- APScheduler 3.9.1+
- requests 2.28.0+
- Access to CHRONICLE database
- Valid Alpaca paper trading credentials
- Valid Axiom Telegram bot token
- Network access to Telegram API
- Network access to Alpaca API

---

## 🔐 Security

- ✅ All credentials stored in `.env` (not in code)
- ✅ Paper trading account only (no real money)
- ✅ Telegram messages use standard bot API
- ✅ No sensitive data in logs
- ✅ All API access authenticated

---

## 📝 File Structure

```
/Users/ahmedsadek/nexus/thesis/
├── thesis_cron_config.py               ← Job scheduler
├── thesis_health_monitor.py            ← Health & healing
├── thesis_status_reporter.py           ← Status reports
├── thesis_trading_validator.py         ← Trading test
├── main.py                             ← Integration point
├── .env                                ← Credentials
├── MAXIMUS_README.md                   ← This file
├── MAXIMUS_QUICK_REFERENCE.md          ← Quick ref
├── MAXIMUS_QUICKSTART.md               ← Setup guide
├── MAXIMUS_DEPLOYMENT.md               ← Architecture
├── MAXIMUS_DEPLOYMENT_CHECKLIST.md     ← Verification
├── MAXIMUS_EXECUTION_SUMMARY.md        ← Implementation
├── MAXIMUS_COMPLETION_REPORT.md        ← Project report
└── MAXIMUS_TESTING_PLAN.md             ← Testing guide

/Users/ahmedsadek/nexus/logs/
├── thesis-health.log                   ← Health checks
├── thesis-status.log                   ← Status reports
└── thesis-trading-validator.log        ← Trading tests
```

---

## ✅ Next Steps

1. **Read [MAXIMUS_QUICK_REFERENCE.md](./MAXIMUS_QUICK_REFERENCE.md)** (5 min)
2. **Follow [MAXIMUS_QUICKSTART.md](./MAXIMUS_QUICKSTART.md)** (10 min)
3. **Start Thesis service** (5 min)
4. **Verify logs** (2 min)
5. **Monitor Telegram** (ongoing)

---

## 📈 Success Metrics

You know MAXIMUS is working when:

- [ ] Service starts without errors
- [ ] Health endpoint returns `scheduler_ok: true`
- [ ] 7 total jobs configured (3 existing + 4 MAXIMUS)
- [ ] Health checks appear in logs every 30 minutes
- [ ] Telegram message received at 12:15 PM
- [ ] Telegram message received at 4:15 PM
- [ ] Trading validator runs at 9:45 AM with result
- [ ] No critical errors in logs

---

## 🎯 Summary

**MAXIMUS** is a production-ready autonomous monitoring and self-healing system for Thesis. It keeps Ahmed informed with daily Telegram updates while automatically fixing problems before they become critical.

**Status:** ✅ Ready to deploy  
**Quality:** Production-grade  
**Support:** Comprehensive documentation included  
**Time to deploy:** ~10 minutes  

---

**For detailed information, see [MAXIMUS_DEPLOYMENT_CHECKLIST.md](./MAXIMUS_DEPLOYMENT_CHECKLIST.md)**

**Questions? Check [MAXIMUS_QUICK_REFERENCE.md](./MAXIMUS_QUICK_REFERENCE.md) troubleshooting section**
