# SQS Preflight Validation System — Deployment Guide

**Status:** Production-Ready  
**Created:** 2026-06-02  
**Mandate:** Daily automated system health validation before market open

---

## 🚀 Quick Start

### 1. Install Files

```bash
# Copy core modules
cp /path/to/preflight_runner.py /Users/ahmedsadek/nexus/
cp /path/to/health_checks.py /Users/ahmedsadek/nexus/
cp /path/to/recovery_actions.py /Users/ahmedsadek/nexus/

# Create LaunchAgents directory if needed
mkdir -p ~/Library/LaunchAgents

# Install launchd plist
cp /path/to/com.axiom.sqs-preflight.plist ~/Library/LaunchAgents/

# Set proper permissions
chmod 644 ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist
```

### 2. Install Python Dependencies

```bash
# psutil is required (for process health checks)
pip3 install psutil requests pytz

# Verify installations
python3 -c "import psutil, requests, pytz; print('✅ All dependencies installed')"
```

### 3. Create Required Directories

```bash
mkdir -p /Users/ahmedsadek/nexus/logs
mkdir -p /Users/ahmedsadek/nexus/LaunchAgents
chmod 755 /Users/ahmedsadek/nexus/logs
```

### 4. Verify Environment Variables

The system reads from `.env` files. Ensure these are set:

```bash
# Check alpha-execution/.env has:
export ALPACA_API_KEY="PKPGM3BRNYPGCF5Z56IAUZCZJL"
export ALPACA_SECRET_KEY="5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs"
export ALPACA_PAPER="true"
export AHMED_CHAT_ID="8573754783"
export AXIOM_BOT_TOKEN="8664199571:AAF5H9XFvhhjcbAiLuV-qul2humI3yWO2fo"
export FRED_API_KEY="5749ecf7ebd18e7f77e30ef2357f55b7"
```

### 5. Load launchd Service

```bash
# Load the service
launchctl load ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist

# Verify it loaded
launchctl list | grep sqs-preflight

# Check service is scheduled
launchctl list com.axiom.sqs-preflight
```

### 6. Test Run (Manual)

```bash
# Run preflight manually to test
python3 /Users/ahmedsadek/nexus/preflight_runner.py

# Watch logs
tail -f /Users/ahmedsadek/nexus/logs/sqs_preflight.log
```

---

## 📋 System Architecture

### Components

1. **preflight_runner.py** — Main orchestrator
   - Schedules and runs all checks
   - Coordinates recovery actions
   - Generates GO/NO-GO report
   - Sends notifications to Ahmed
   - Stores results in CHRONICLE

2. **health_checks.py** — 9 validation modules
   - Process health check
   - Database connectivity
   - SQS connectivity
   - Capital allocation tracking
   - Order submission capability
   - Position tracking sync
   - Alert system connectivity
   - Market data (FRED) access
   - Filesystem integrity

3. **recovery_actions.py** — Auto-fix logic
   - Restart failed processes
   - Repair databases
   - Clear stale locks
   - Reconnect services
   - Fix permissions

### Execution Flow

```
8:30 AM ET (Market opens 9:30 AM, preflight runs at 8:30 AM)
    ↓
[launchd triggers preflight_runner.py]
    ↓
Phase 1: Run health checks for all 9 engines (parallel)
    ├─ ATG_SWING      (8 checks)
    ├─ ATG_INTRADAY   (8 checks)
    ├─ AILS           (8 checks)
    ├─ ATM_SWING      (8 checks)
    ├─ ATM_MULTIWEEK  (8 checks)
    ├─ AMAT           (8 checks)
    ├─ ATEM           (8 checks)
    ├─ ATOM           (8 checks)
    └─ ATG_GAMMA      (8 checks)
    ↓
Phase 2: Analyze results
    ├─ Calculate overall status (READY/NOT_READY/DEGRADED)
    ├─ Identify failures
    └─ Generate remediation steps
    ↓
Phase 3: Attempt auto-recovery (if failures)
    ├─ Restart processes
    ├─ Repair databases
    ├─ Reconnect services
    └─ Re-test recovered services
    ↓
Phase 4: Generate report
    ├─ Per-engine status table
    ├─ Failure summaries
    ├─ Remediation steps
    └─ Confidence score
    ↓
[Send to Ahmed DM + store in CHRONICLE]
    ↓
✅ Complete (< 30 seconds)
```

---

## 🔍 Health Check Details

Each engine gets tested across these 9 dimensions:

### 1. Process Health
- Process running? ✓
- PID active? ✓
- Zombie? ✓
- CPU reasonable? ✓

**Failure example:** `"ATG_SWING: process_health — Process 'atg_swing' not running"`

**Recovery:** Restart via systemd or script

### 2. Database Connectivity
- capital_handler.db accessible? ✓
- Readable? ✓
- Writable? ✓
- Latency acceptable? ✓

**Failure example:** `"ATG_INTRADAY: database — Database corrupted"`

**Recovery:** PRAGMA integrity_check, REINDEX, VACUUM, or backup+reset

### 3. SQS Connectivity
- Engine service responding? ✓
- Health endpoint returns 200? ✓
- Latency < 2 seconds? ✓

**Failure example:** `"AILS: sqs — Cannot reach http://localhost:8008"`

**Recovery:** Restart engine service

### 4. Capital Allocation Tracking
- capital_handler.db has records? ✓
- Allocated ≈ Available? ✓
- Drift < 0.1%? ✓

**Failure example:** `"ATM_SWING: capital_allocation — Capital drift detected: 0.15%"`

**Recovery:** Apply automated reconciliation

### 5. Order Submission Capability
- Alpaca API reachable? ✓
- Account accessible? ✓
- Credentials valid? ✓
- Paper trading enabled? ✓

**Failure example:** `"ATM_MULTIWEEK: order_submission — Alpaca API timeout"`

**Recovery:** Verify credentials, check network

### 6. Position Tracking Sync
- Alpaca positions fetchable? ✓
- Count matches internal state? ✓
- No orphan positions? ✓

**Failure example:** `"AMAT: position_sync — Cannot fetch Alpaca positions (HTTP 401)"`

**Recovery:** Verify Alpaca credentials

### 7. Alert System Connectivity
- Telegram bot reachable? ✓
- Bot token valid? ✓
- Can send messages? ✓

**Failure example:** `"ATEM: alert_system — Telegram API returned HTTP 401"`

**Recovery:** Verify bot token

### 8. Market Data (FRED) Access
- FRED API reachable? ✓
- API key valid? ✓
- Can fetch data? ✓

**Failure example:** `"ATOM: market_data — FRED API returned HTTP 400"`

**Recovery:** Verify FRED API key

### 9. Filesystem Integrity
- Required paths exist? ✓
- All paths writable? ✓
- No permission errors? ✓

**Failure example:** `"ATG_GAMMA: filesystem — Unwritable paths: /Users/ahmedsadek/nexus/logs"`

**Recovery:** Create directory, fix permissions (chmod 755)

---

## 📊 Report Format

Ahmed receives a report in this format:

```
✅ SQS PREFLIGHT REPORT

Status: READY
Confidence: 98%
Engines: 9/9 OK
Exec Time: 8234 ms
Timestamp: 2026-06-02T08:31:42.123456-04:00

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

[If failures, includes:]
Failures (3):
1. ATG_SWING: database — Database corrupted
2. AILS: process_health — Process not running
3. ATM_SWING: capital_allocation — Capital drift 0.25%

Remediation Steps:
1. Restart failed engine processes: `restart_engines.sh`
2. Check database locks: `lsof | grep *.db`
3. Run capital reconciliation
```

---

## 🛠️ Management

### View Scheduled Status
```bash
launchctl list com.axiom.sqs-preflight
```

### Manual Run
```bash
python3 /Users/ahmedsadek/nexus/preflight_runner.py
```

### View Logs
```bash
# Main preflight log
tail -100 /Users/ahmedsadek/nexus/logs/sqs_preflight.log

# Stdout/stderr from launchd
tail /Users/ahmedsadek/nexus/logs/sqs_preflight_stdout.log
tail /Users/ahmedsadek/nexus/logs/sqs_preflight_stderr.log
```

### Reload Service (after changes)
```bash
launchctl unload ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist
launchctl load ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist
```

### Disable Service
```bash
launchctl unload ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist
```

### Enable Service
```bash
launchctl load ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist
```

---

## 🔧 Configuration

### Timing
Edit `com.axiom.sqs-preflight.plist`:
- Default: **8:30 AM ET Monday-Friday**
- Change `Hour` and `Minute` to adjust time
- Modify `Weekday` array to change days (1=Mon, 5=Fri)

### Engine List
Edit `preflight_runner.py`:
```python
ENGINES = [
    "ATG_SWING",      # Modify this list
    "ATG_INTRADAY",
    # ... etc
]
```

### Check Timeouts
Edit `health_checks.py`:
```python
timeout=3  # Seconds per HTTP request
timeout=2  # Seconds per database query
```

### Database Path
All three files use:
```python
CAPITAL_DB = Path("/Users/ahmedsadek/nexus/data/capital_handler.db")
```
If you change database location, update all three files.

---

## 📈 Monitoring

### CHRONICLE Database
Results are stored in `/Users/ahmedsadek/nexus/data/chronicle.db`:

```sql
-- View preflight results
SELECT * FROM preflight_results 
ORDER BY timestamp DESC 
LIMIT 10;

-- Check result columns
PRAGMA table_info(preflight_results);
```

### Query Examples
```sql
-- Get last preflight
SELECT timestamp, overall_status, confidence_score, engines_ok, engines_total
FROM preflight_results
ORDER BY timestamp DESC
LIMIT 1;

-- Get failure trends
SELECT DATE(timestamp) as date, overall_status, COUNT(*) as runs
FROM preflight_results
GROUP BY DATE(timestamp)
ORDER BY date DESC;

-- Get engine-specific failures
SELECT timestamp, engine_results_json
FROM preflight_results
WHERE critical_failures IS NOT NULL
LIMIT 5;
```

---

## 🚨 Troubleshooting

### Problem: Service not running at 8:30 AM
```bash
# Check if service is loaded
launchctl list | grep sqs-preflight

# Reload if needed
launchctl unload ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist
launchctl load ~/Library/LaunchAgents/com.axiom.sqs-preflight.plist

# Check system logs
log stream --predicate 'process == "launchd"'
```

### Problem: Python imports failing
```bash
# Verify PYTHONPATH in plist includes both:
# /Users/ahmedsadek/nexus
# /Users/ahmedsadek/nexus/shared

# Test manually
cd /Users/ahmedsadek/nexus
python3 -c "import preflight_runner, health_checks, recovery_actions"
```

### Problem: Telegram not receiving reports
```bash
# Verify bot token
echo $AXIOM_BOT_TOKEN

# Test Telegram API
curl https://api.telegram.org/bot<YOUR_TOKEN>/getMe

# Verify Ahmed's chat ID
echo $AHMED_CHAT_ID  # Should be 8573754783
```

### Problem: Database lock errors
```bash
# Check for stale locks
lsof | grep capital_handler.db

# Kill process holding lock (if needed)
kill <PID>

# Reset WAL file if stuck
rm /Users/ahmedsadek/nexus/data/capital_handler.db-shm
rm /Users/ahmedsadek/nexus/data/capital_handler.db-wal
```

---

## 📝 Maintenance

### Weekly
- Review preflight logs for patterns
- Check confidence scores trending down
- Verify all engines passing tests

### Monthly
- Review CHRONICLE preflight_results table
- Check for repeated failures on same engine
- Verify recovery actions are successful

### Before Market Events
- Run preflight manually before Fed announcements
- Run preflight on market open if anything looks off

---

## 🎯 Success Criteria

✅ **System Ready for Trading** when:
- All 9 engines report OK
- All 72 checks (9 × 8) pass
- Confidence score ≥ 95%
- Execution time < 30 seconds
- Report sent to Ahmed < 60 seconds before market open

⚠️ **Degraded** when:
- 7+ engines OK (80%+)
- Most critical paths clear
- Non-blocking failures auto-recovered

🚨 **NOT Ready** when:
- < 7 engines OK
- Critical failures unrepairable
- Alpaca or database unreachable
- Capital tracking drift > 0.5%

---

## 📞 Support

- **Logs location:** `/Users/ahmedsadek/nexus/logs/sqs_preflight*.log`
- **Results location:** `chronicle.db` → `preflight_results` table
- **Contact:** Report to Ahmed via Telegram immediately on NOT_READY
- **Manual fix script:** `restart_engines.sh` (create as needed)

---

## Version History

| Date | Change |
|------|--------|
| 2026-06-02 | Initial release: 9 engines, 9 checks, auto-recovery |
| TBD | Add health check customization per-engine |
| TBD | Add SLA escalation if repeated failures |

---

**Created by Axiom | Deployed 2026-06-02 | Updated 2026-06-02**
