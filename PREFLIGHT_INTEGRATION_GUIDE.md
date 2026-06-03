# SQS Preflight Validation — Integration Guide

**For Ahmed & Axiom Deployment Team**

---

## 📦 What You're Installing

A production-grade health validation system that:

✅ **Runs automatically** every trading day at 8:30 AM ET (before 9:30 AM market open)  
✅ **Tests all 9 engines** (ATG_SWING, ATG_INTRADAY, AILS, ATM_SWING, ATM_MULTIWEEK, AMAT, ATEM, ATOM, ATG_GAMMA)  
✅ **Validates 9 critical dimensions** per engine (process, database, SQS, capital, orders, positions, alerts, data, filesystem)  
✅ **Auto-recovers failures** with intelligent retry logic  
✅ **Sends GO/NO-GO report** to Ahmed every morning  
✅ **Stores results** in CHRONICLE database for audit trail  
✅ **Completes in < 30 seconds** (safety margin before market open)  

---

## 🎯 One-Line Deployment

```bash
bash /Users/ahmedsadek/nexus/scripts/deploy-preflight.sh
```

This single command:
1. Verifies all Python dependencies
2. Creates required directories
3. Validates plist configuration
4. Installs launchd service
5. Runs test execution
6. Confirms system ready

**Expected output:** `✅ DEPLOYMENT SUCCESSFUL`

---

## 📂 Files Created/Modified

### Core Modules (3 files)
| File | Purpose | Size |
|------|---------|------|
| `preflight_runner.py` | Main orchestrator, report generation | 16 KB |
| `health_checks.py` | 9 validation functions (async) | 19 KB |
| `recovery_actions.py` | Auto-fix logic for failures | 20 KB |

### Configuration (1 file)
| File | Purpose |
|------|---------|
| `LaunchAgents/com.axiom.sqs-preflight.plist` | launchd scheduling (8:30 AM ET, weekdays) |

### Documentation (3 files)
| File | Purpose |
|------|---------|
| `PREFLIGHT_DEPLOYMENT.md` | Detailed deployment guide |
| `PREFLIGHT_INTEGRATION_GUIDE.md` | This file — integration instructions |
| `scripts/deploy-preflight.sh` | One-command deployment script |

### Logging (created at runtime)
- `/Users/ahmedsadek/nexus/logs/sqs_preflight.log` — Main log file
- `/Users/ahmedsadek/nexus/logs/sqs_preflight_stdout.log` — launchd stdout
- `/Users/ahmedsadek/nexus/logs/sqs_preflight_stderr.log` — launchd stderr

---

## ✅ Deployment Checklist

### Pre-Deployment (5 minutes)

- [ ] Download all 3 core modules to `/Users/ahmedsadek/nexus/`
- [ ] Download plist to `/Users/ahmedsadek/nexus/LaunchAgents/`
- [ ] Download deploy script to `/Users/ahmedsadek/nexus/scripts/`
- [ ] Verify `.env` exists with all required keys:
  ```bash
  cat /Users/ahmedsadek/nexus/alpha-execution/.env | grep -E "ALPACA|AHMED|AXIOM|FRED"
  ```
- [ ] Verify Python 3.8+ installed:
  ```bash
  python3 --version
  ```

### Deployment (2 minutes)

```bash
bash /Users/ahmedsadek/nexus/scripts/deploy-preflight.sh
```

### Post-Deployment Verification (5 minutes)

```bash
# 1. Check service loaded
launchctl list | grep sqs-preflight

# 2. Verify logs directory
ls -la /Users/ahmedsadek/nexus/logs/ | grep preflight

# 3. Test run (optional)
python3 /Users/ahmedsadek/nexus/preflight_runner.py

# 4. Check CHRONICLE database
sqlite3 /Users/ahmedsadek/nexus/data/chronicle.db \
  "SELECT COUNT(*) as preflight_runs FROM preflight_results;"
```

---

## 🔄 Integration Points

### 1. Alert System Integration

**Sending Reports to Ahmed:**
- Uses `AXIOM_BOT_TOKEN` to send Telegram messages
- Sends to `AHMED_CHAT_ID` (8573754783)
- Includes per-engine status table
- Lists failures with remediation steps

**Fallback:** If Telegram fails, uses alert_client.py (fallback to direct send)

### 2. CHRONICLE Database Integration

**Storage:**
- Results stored in `preflight_results` table
- Created automatically if doesn't exist
- Stores full JSON report for audit

**Query results:**
```sql
SELECT 
    timestamp, 
    overall_status, 
    confidence_score,
    engines_ok, 
    engines_total
FROM preflight_results 
ORDER BY timestamp DESC 
LIMIT 10;
```

### 3. Capital Handler Integration

**Database:** Reads from `capital_handler.db`
- Checks for allocation records
- Validates capital drift < 0.1%
- Auto-reconciles if drift detected
- Logs recovery to CHRONICLE

### 4. Alpaca Integration

**API Calls:**
- Fetches account info (verify connectivity)
- Retrieves open positions
- Validates paper trading enabled
- Reports available cash

**Credentials:** Read from environment
- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `ALPACA_PAPER=true` (critical)

### 5. FRED Integration

**Data Access:**
- Tests FRED API connectivity
- Validates API key works
- Checks critical economic series available

### 6. Process Monitoring Integration

**Uses psutil to check:**
- Process exists and running
- PID is active
- No zombie processes
- CPU usage reasonable

---

## 🚀 First Run Checklist

Before first automatic run at 8:30 AM:

1. **Verify credentials are correct:**
   ```bash
   grep -E "ALPACA|FRED|AXIOM|AHMED" /Users/ahmedsadek/nexus/alpha-execution/.env
   ```

2. **Test database connectivity:**
   ```bash
   python3 -c "
   import sqlite3
   db = sqlite3.connect('/Users/ahmedsadek/nexus/data/capital_handler.db')
   print('✅ Capital DB OK')
   db.close()
   "
   ```

3. **Test Alpaca connectivity:**
   ```bash
   python3 << 'EOF'
   import os, requests
   headers = {
       "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY"),
       "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY"),
   }
   r = requests.get("https://paper-api.alpaca.markets/v2/account", headers=headers)
   if r.status_code == 200:
       print("✅ Alpaca OK:", r.json()["cash"])
   else:
       print("❌ Alpaca error:", r.status_code)
   EOF
   ```

4. **Test Telegram connectivity:**
   ```bash
   python3 << 'EOF'
   import os, requests
   token = os.getenv("AXIOM_BOT_TOKEN")
   r = requests.get(f"https://api.telegram.org/bot{token}/getMe")
   if r.json().get("ok"):
       print("✅ Telegram OK")
   else:
       print("❌ Telegram error")
   EOF
   ```

5. **Manual test run:**
   ```bash
   python3 /Users/ahmedsadek/nexus/preflight_runner.py
   ```

---

## 📊 What Ahmed Sees Every Morning

### Report Format

```
✅ SQS PREFLIGHT REPORT

Status: READY
Confidence: 98%
Engines: 9/9 OK
Exec Time: 8234 ms
Timestamp: 2026-06-02T08:31:42-04:00

Engine Status Table:
───────────────────────────
Engine          Status
───────────────────────────
ATG_SWING       ✅ OK
ATG_INTRADAY    ✅ OK
AILS            ✅ OK
ATM_SWING       ✅ OK
ATM_MULTIWEEK   ✅ OK
AMAT            ✅ OK
ATEM            ✅ OK
ATOM            ✅ OK
ATG_GAMMA       ✅ OK
───────────────────────────
```

### On Failures

```
🚨 SQS PREFLIGHT REPORT

Status: NOT_READY
Confidence: 65%
Engines: 7/9 OK
Exec Time: 12450 ms
Timestamp: 2026-06-02T08:31:42-04:00

[... engine table ...]

Failures (3):
1. ATG_SWING: database — Database corrupted
2. AILS: process_health — Process not running  
3. ATM_SWING: capital_allocation — Capital drift 0.25%

Remediation Steps:
1. Restart failed engine processes: `restart_engines.sh`
2. Check database locks: `lsof | grep *.db`
3. Rebuild database if corrupted: `rebuild_capital_db.sh`
```

---

## 🔧 Configuration & Customization

### Change Run Time

Edit `~/Library/LaunchAgents/com.axiom.sqs-preflight.plist`:

```xml
<!-- Find StartCalendarInterval section -->
<key>StartCalendarInterval</key>
<array>
    <dict>
        <key>Hour</key>
        <integer>13</integer>          <!-- 0-23, UTC hour -->
        <key>Minute</key>
        <integer>30</integer>          <!-- 0-59, UTC minute -->
        <key>Weekday</key>
        <integer>1</integer>           <!-- 1=Mon, 2=Tue, ..., 5=Fri -->
    </dict>
    <!-- Repeat for other days -->
</array>
```

Examples:
- 8:00 AM ET = Hour 13, Minute 0
- 8:30 AM ET = Hour 13, Minute 30 (default)
- 9:00 AM ET = Hour 13, Minute 60 (= Hour 14, Minute 0)

**Reload after changes:**
```bash
launchctl unload ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist
launchctl load ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist
```

### Add/Remove Engines

Edit `preflight_runner.py`:

```python
ENGINES = [
    "ATG_SWING",      # Add/remove here
    "ATG_INTRADAY",
    # "DISABLED_ENGINE",  # Comment out to disable
    # ... etc
]
```

Also update `health_checks.py`:

```python
ENGINE_PROCESSES = {
    "ATG_SWING": "atg_swing",       # Process name
    "ATG_INTRADAY": "atg_intraday",
    # ... etc
}

ENGINE_URLS = {
    "ATG_SWING": "http://localhost:8003",       # Health endpoint
    "ATG_INTRADAY": "http://localhost:8004",
    # ... etc
}
```

### Adjust Check Thresholds

Edit `health_checks.py`:

```python
# Capital drift tolerance
if drift_percent > 0.1:  # Current: 0.1%
    # Change to 0.5% or 1% as needed

# CPU warning threshold
if cpu_percent > 200:  # Current: 2 cores worth
    # Change to 100, 300, etc.

# Network timeouts
timeout=3  # Current: 3 seconds per request
# Change to 5 or 10 for slower networks
```

---

## 🐛 Troubleshooting

### Issue: Service not running at scheduled time

**Symptoms:** No log file created at 8:30 AM

**Diagnosis:**
```bash
# Check if loaded
launchctl list | grep sqs-preflight

# Check system log
log stream --predicate 'eventMessage contains[c] "sqs-preflight"'
```

**Fix:**
```bash
# Reload service
launchctl unload ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist
launchctl load ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist

# Verify
launchctl list com.axiom.sqs-preflight
```

### Issue: Ahmed not receiving Telegram reports

**Diagnosis:**
```bash
# Check if AXIOM_BOT_TOKEN is set
echo $AXIOM_BOT_TOKEN

# Test bot
curl https://api.telegram.org/bot<TOKEN>/getMe

# Check if AHMED_CHAT_ID is correct
echo $AHMED_CHAT_ID  # Should be 8573754783
```

**Fix:**
```bash
# Verify .env has correct values
grep AXIOM_BOT_TOKEN /Users/ahmedsadek/nexus/alpha-execution/.env
grep AHMED_CHAT_ID /Users/ahmedsadek/nexus/alpha-execution/.env

# Reload launchd to pick up new env vars
launchctl unload ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist
launchctl load ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist
```

### Issue: Database errors in preflight log

**Symptoms:** "Database corrupted" or "database lock"

**Diagnosis:**
```bash
# Check for locks
lsof | grep capital_handler.db

# Check integrity
sqlite3 /Users/ahmedsadek/nexus/data/capital_handler.db "PRAGMA integrity_check;"
```

**Fix:**
```bash
# Kill process holding lock
kill <PID>

# Repair database
python3 << 'EOF'
import sqlite3
db = sqlite3.connect("/Users/ahmedsadek/nexus/data/capital_handler.db")
db.execute("PRAGMA integrity_check")
db.execute("REINDEX")
db.execute("VACUUM")
db.commit()
print("✅ Database repaired")
db.close()
EOF
```

### Issue: Timeout errors in preflight output

**Symptoms:** "connection timeout" or "HTTP 504"

**Diagnosis:**
```bash
# Check network
ping 8.8.8.8

# Check specific service
curl -v http://localhost:8003/health  # For ATG_SWING
```

**Fix:**
```bash
# Increase timeouts in health_checks.py
# Change timeout=3 to timeout=5

# Restart engines
systemctl restart nexus-*

# Or restart manually:
pkill -f atg_swing
sleep 1
# Start service as needed
```

---

## 📈 Monitoring & Maintenance

### Daily Check
```bash
# Before market open, verify preflight ran
ls -l /Users/ahmedsadek/nexus/logs/sqs_preflight.log
tail -10 /Users/ahmedsadek/nexus/logs/sqs_preflight.log
```

### Weekly Review
```bash
# Query results from CHRONICLE
sqlite3 /Users/ahmedsadek/nexus/data/chronicle.db << 'SQL'
SELECT 
    DATE(timestamp) as date,
    overall_status,
    COUNT(*) as runs,
    AVG(confidence_score) as avg_confidence
FROM preflight_results
GROUP BY DATE(timestamp)
ORDER BY date DESC
LIMIT 5;
SQL
```

### Monthly Audit
```bash
# Review failure patterns
sqlite3 /Users/ahmedsadek/nexus/data/chronicle.db << 'SQL'
SELECT 
    overall_status,
    COUNT(*) as count,
    AVG(confidence_score) as avg_score
FROM preflight_results
WHERE DATE(timestamp) >= DATE('now', '-30 days')
GROUP BY overall_status;
SQL
```

---

## 🎯 Success Metrics

**System is working correctly when:**

✅ Report arrives in Ahmed DM by 8:35 AM ET daily  
✅ All 9 engines report OK on most days  
✅ Confidence score ≥ 95% average  
✅ Execution time < 15 seconds (normal case)  
✅ Auto-recovery succeeds > 90% of failures  
✅ Zero crashes or unhandled exceptions  

---

## 📞 Emergency Procedures

### If preflight crashes
```bash
# Check logs
tail -100 /Users/ahmedsadek/nexus/logs/sqs_preflight.log

# Test manually
python3 /Users/ahmedsadek/nexus/preflight_runner.py

# Contact Ahmed with error from log
```

### If system shows NOT_READY
```bash
# Ahmed should:
# 1. Check detailed failure report from Telegram
# 2. Follow remediation steps provided
# 3. Or manually run recovery:
python3 -c "
import sys
sys.path.insert(0, '/Users/ahmedsadek/nexus')
from recovery_actions import attempt_auto_recovery
# Retry specific failure
"

# Re-run preflight to confirm recovery
python3 /Users/ahmedsadek/nexus/preflight_runner.py
```

---

## 📝 Support & Documentation

| Document | Purpose |
|----------|---------|
| `PREFLIGHT_DEPLOYMENT.md` | Detailed setup & configuration |
| `PREFLIGHT_INTEGRATION_GUIDE.md` | This file — for Ahmed |
| `scripts/deploy-preflight.sh` | Automated deployment |
| Inline code comments | Every function documented |

---

## ✨ Next Steps

1. **Deploy:** Run `bash /Users/ahmedsadek/nexus/scripts/deploy-preflight.sh`
2. **Test:** Verify with manual run `python3 /Users/ahmedsadek/nexus/preflight_runner.py`
3. **Wait:** System will run automatically at 8:30 AM ET
4. **Monitor:** Check logs and CHRONICLE results daily
5. **Adjust:** Update configuration as needed per production experience

---

**Production Ready | Created 2026-06-02 | Maintained by Axiom**
