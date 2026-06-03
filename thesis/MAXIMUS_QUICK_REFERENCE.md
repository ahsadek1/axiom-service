# MAXIMUS — QUICK REFERENCE CARD

**THESIS Autonomous Monitoring & Self-Healing System**

---

## 🚀 START THE SYSTEM

```bash
cd /Users/ahmedsadek/nexus/thesis
python main.py > /tmp/thesis.log 2>&1 &
sleep 5
ps aux | grep thesis | grep -v grep
```

**Expected:** One `python main.py` process running

---

## 📊 WHAT RUNS AUTOMATICALLY

### Daily at 9:45 AM (Pre-Market)
**Trading Ability Test**
- Buy 1 SPY → Verify → Sell → Verify Closed
- Result: ✅ or 🔴 sent to your Telegram DM

### Every 30 Minutes (9:30 AM - 4:00 PM ET)
**Health Check & Self-Healing**
- Checks: CHRONICLE, APScheduler, Alpaca, Entry Logic, Positions
- Auto-fixes: Reconnect → Clear cache → Restart jobs
- Escalates: If 3 failures in a row

### Daily at 12:15 PM (Mid-Day)
**Mid-Session Status Report**
- Trades executed so far
- Open positions & unrealized P&L
- Any errors/flags
- System health status
- **Sent to:** Your Telegram DM

### Daily at 4:15 PM (After Close)
**Post-Close Summary**
- Total trades executed today
- Total P&L
- Best & worst trade
- Failure summary
- Tomorrow readiness
- **Sent to:** Your Telegram DM

---

## 📋 VERIFY DEPLOYMENT

```bash
# 1. Check service running
ps aux | grep thesis | grep -v grep

# 2. Check health
curl http://localhost:8060/thesis/health | jq '.scheduler_ok'
# Should be: true

# 3. Check jobs loaded
tail /tmp/thesis.log | grep "Job scheduled"
# Should show 7 jobs total (3 existing + 4 MAXIMUS)

# 4. Check logs exist
ls -la /Users/ahmedsadek/nexus/logs/thesis-*.log
# Should show 3 files: health, status, trading-validator
```

---

## 🔍 MONITOR LOGS

```bash
# Real-time health checks
tail -f /Users/ahmedsadek/nexus/logs/thesis-health.log

# Status reports
tail -f /Users/ahmedsadek/nexus/logs/thesis-status.log

# Trading validation (daily at 9:45 AM)
tail -f /Users/ahmedsadek/nexus/logs/thesis-trading-validator.log
```

---

## 📲 TELEGRAM MESSAGES

You'll receive automatic messages at:
- **9:45 AM** — Trading test result (Pass/Fail)
- **12:15 PM** — Mid-session trades & positions
- **4:15 PM** — Daily summary & P&L

If you don't see these, check:
```bash
# Verify bot token
grep AXIOM_BOT_TOKEN /Users/ahmedsadek/nexus/thesis/.env

# Test Telegram
curl -X POST "https://api.telegram.org/bot8664199571:AAF5H9XFvhhjcbAiLuV-qul2humI3yWO2fo/sendMessage" \
  -H "Content-Type: application/json" \
  -d '{"chat_id":"8573754783","text":"Test"}'
```

---

## 🛠️ AUTO-HEALING TIERS

If something breaks:

**Tier 1 (Auto):** Reconnect to services  
**Tier 2 (Auto):** Clear cache & reset connections  
**Tier 3 (Auto):** Restart APScheduler jobs  
**Tier 4 (Alert):** Escalate to Axiom + Cipher after 3 failures  

You'll get a Telegram alert if anything needs manual intervention.

---

## 🆘 TROUBLESHOOTING

### Service won't start?
```bash
# Kill old process
pkill -f "python.*thesis.*main.py"
sleep 3

# Check for port conflict
lsof -i :8060

# Start fresh
cd /Users/ahmedsadek/nexus/thesis && python main.py
```

### No health checks in logs?
```bash
# Check if it's trading hours (9:30 AM - 4:00 PM ET)
date

# Check if scheduler is running
curl http://localhost:8060/thesis/health | jq '.scheduler_ok'

# Check for errors
tail -50 /tmp/thesis.log | grep -i error
```

### No Telegram messages?
```bash
# Verify credentials
grep -E "AXIOM_BOT_TOKEN|8573754783" /Users/ahmedsadek/nexus/thesis/.env

# Test Telegram directly
curl -X POST "https://api.telegram.org/bot8664199571:AAF5H9XFvhhjcbAiLuV-qul2humI3yWO2fo/getMe"
```

### CHRONICLE not found?
```bash
# Verify database exists
ls -la /Users/ahmedsadek/nexus/data/chronicle.db

# Test connection
python3 -c "import sqlite3; conn = sqlite3.connect('/Users/ahmedsadek/nexus/data/chronicle.db'); print('✅ OK')"
```

---

## 📁 FILES & PATHS

```
/Users/ahmedsadek/nexus/thesis/
├── main.py                           (MAXIMUS integrated)
├── thesis_cron_config.py            (Job scheduler)
├── thesis_health_monitor.py         (Health checks)
├── thesis_status_reporter.py        (Telegram messages)
├── thesis_trading_validator.py      (Daily test)
└── .env                             (Credentials)

/Users/ahmedsadek/nexus/logs/
├── thesis-health.log                (Every 30 min)
├── thesis-status.log                (12:15 PM & 4:15 PM)
└── thesis-trading-validator.log     (9:45 AM daily)

/Users/ahmedsadek/nexus/data/
└── chronicle.db                     (Event log & database)
```

---

## 📞 EMERGENCY STOP

If you need to pause MAXIMUS without stopping Thesis:

```bash
# Comment out in main.py around line 285:
# maximus_success = apply_monitoring_configuration(...)

# Then restart:
pkill -f "python.*thesis.*main.py"
sleep 3
cd /Users/ahmedsadek/nexus/thesis && python main.py > /tmp/thesis.log 2>&1 &
```

---

## ✅ SUCCESS INDICATORS

You know MAXIMUS is working when:

- [ ] Service starts without errors
- [ ] Health endpoint returns `scheduler_ok: true`
- [ ] Logs show 7 total jobs configured
- [ ] Health check logs appear every 30 minutes
- [ ] You receive Telegram message at 12:15 PM
- [ ] You receive Telegram message at 4:15 PM
- [ ] Trading validator runs at 9:45 AM with result
- [ ] No critical errors in logs

---

## 🔧 CONFIGURATION

All settings in `/Users/ahmedsadek/nexus/thesis/.env`:

```env
ALPACA_KEY=PKPGM3BRNYPGCF5Z56IAUZCZJL
ALPACA_SECRET=5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs
AXIOM_BOT_TOKEN=8664199571:AAF5H9XFvhhjcbAiLuV-qul2humI3yWO2fo
AHMED_CHAT_ID=8573754783
CHRONICLE_DB_PATH=/Users/ahmedsadek/nexus/data/chronicle.db
THESIS_PORT=8060
THESIS_HOST=0.0.0.0
```

---

## 📚 FULL DOCUMENTATION

For detailed info:
- `MAXIMUS_QUICKSTART.md` — Setup guide
- `MAXIMUS_DEPLOYMENT.md` — Architecture & config
- `MAXIMUS_TESTING_PLAN.md` — Testing procedures
- `MAXIMUS_DEPLOYMENT_CHECKLIST.md` — Verification steps
- `MAXIMUS_EXECUTION_SUMMARY.md` — What was implemented

---

## 🎯 THE PROMISE

✅ **Autonomous** — No manual monitoring needed  
✅ **Self-Healing** — Auto-fixes before escalation  
✅ **Transparent** — Logs + Telegram updates keep you informed  
✅ **Reliable** — 3-tier escalation protocol ensures nothing breaks silently  
✅ **Simple** — One command to start, Telegram messages keep you updated  

---

**Status:** ✅ Production Ready  
**Configured For:** Ahmed Sadek  
**Service:** Thesis (http://localhost:8060)  
**Updated:** 2026-06-01
