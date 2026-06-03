# MAXIMUS — THESIS AUTONOMOUS MONITORING & SELF-HEALING

## Configuration & Deployment Guide

**Status:** Production-ready

**Components:**
1. `thesis_health_monitor.py` — System health checks & autonomous self-healing
2. `thesis_status_reporter.py` — Automated status reporting to Ahmed
3. `thesis_trading_validator.py` — Daily trading ability verification
4. `thesis_cron_config.py` — APScheduler job configuration

---

## Overview

MAXIMUS adds automated monitoring, self-healing, and status reporting to Thesis:

### Automated Status Reports (Non-Trading Hours Only)

| Time | Audience | Content |
|------|----------|---------|
| **12:15 PM EDT** | Ahmed DM | Trades executed, open positions, errors, health status |
| **4:15 PM EDT** | Ahmed DM | Total trades, P&L, failures, tomorrow readiness |

### Self-Health Check System

- **Frequency:** Every 30 minutes during trading hours (9:30 AM - 4:00 PM ET)
- **Components Checked:**
  - CHRONICLE database connectivity & writability
  - APScheduler job status
  - Alpaca API connectivity & trading ability
  - Entry logic responsiveness
  - Position data consistency

- **Self-Healing Tiers:**
  - **Tier 0:** Reconnect to service
  - **Tier 1:** Clear cache & reset connections
  - **Tier 2:** Restart APScheduler jobs
  - **Tier 3:** Escalate to Axiom + Cipher (after 3 consecutive failures per component)

### Trading Ability Verification

- **Time:** 9:45 AM EDT (pre-market)
- **Test:** Buy 1 SPY, verify position, close position, verify closed
- **Report:** Pass/fail to Ahmed with detailed step-by-step results
- **Escalation:** Immediate alert if any step fails

---

## Installation

### 1. Copy Files to Thesis Directory

```bash
cp thesis_health_monitor.py /Users/ahmedsadek/nexus/thesis/
cp thesis_status_reporter.py /Users/ahmedsadek/nexus/thesis/
cp thesis_trading_validator.py /Users/ahmedsadek/nexus/thesis/
cp thesis_cron_config.py /Users/ahmedsadek/nexus/thesis/
```

### 2. Update Thesis Requirements

Add to `/Users/ahmedsadek/nexus/thesis/requirements.txt`:

```
requests>=2.28.0
APScheduler>=3.9.1
python-telegram-bot>=20.0
```

Install:

```bash
cd /Users/ahmedsadek/nexus/thesis
pip install -r requirements.txt
```

### 3. Create Log Directory

```bash
mkdir -p /Users/ahmedsadek/nexus/logs
```

---

## Configuration

### Environment Variables

Add to `.env` file in Thesis directory:

```env
# Alpaca (required)
ALPACA_KEY=PKPGM3BRNYPGCF5Z56IAUZCZJL
ALPACA_SECRET=5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs

# Telegram (required for status reports)
TELEGRAM_BOT_TOKEN=8664199571:AAF5H9XFvhhjcbAiLuV-qul2humI3yWO2fo

# Ahmed's Telegram ID (for DM)
AHMED_TELEGRAM_ID=8573754783

# Service URLs
THESIS_SERVICE_URL=http://localhost:8060
CHRONICLE_DB_PATH=/Users/ahmedsadek/nexus/data/chronicle.db
```

### Integration with main.py

Add to `thesis/main.py` in the lifespan startup section:

```python
from thesis_cron_config import apply_monitoring_configuration

# After scheduler initialization and before starting scheduler:

# Configure monitoring jobs
success = apply_monitoring_configuration(
    scheduler=_scheduler,
    alpaca_key=os.getenv("ALPACA_KEY"),
    alpaca_secret=os.getenv("ALPACA_SECRET"),
    telegram_token=os.getenv("TELEGRAM_BOT_TOKEN"),
)

if not success:
    logger.error("Failed to configure monitoring jobs")
    raise RuntimeError("Monitoring configuration failed")
```

### CHRONICLE Schema Requirement

Ensure CHRONICLE has the following tables:

```sql
-- thesis table (should already exist)
CREATE TABLE IF NOT EXISTS thesis (
    id INTEGER PRIMARY KEY,
    name TEXT,
    generated_at TEXT,
    updated_at TEXT,
    regime TEXT,
    vix_level REAL,
    regime_health TEXT,
    sentiment_health TEXT,
    engine_version TEXT,
    position_count INTEGER
);

-- trades table (for reporting)
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    symbol TEXT,
    direction TEXT,
    entry_price REAL,
    exit_price REAL,
    entry_time TEXT,
    exit_time TEXT,
    pnl REAL
);

-- event_log table (for error tracking)
CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY,
    timestamp TEXT,
    level TEXT,
    component TEXT,
    message TEXT,
    details TEXT
);
```

---

## Job Schedule

### Job 1: Daily Trading Validation (9:45 AM ET)

**Cron:** `0 45 9 * * * America/New_York`

**Handler:** `ThesisTradingValidator.run_daily_validation()`

**Steps:**
1. Check account access (equity > 0)
2. Submit test order (BUY 1 SPY)
3. Verify position appears in account
4. Close position
5. Verify closed

**On Failure:**
- Cleanup test position
- Send immediate alert to Ahmed
- Log to CHRONICLE

---

### Job 2: Health Check (Every 30 minutes during trading hours)

**Trigger:** `IntervalTrigger(minutes=30)` (filtered for 9:30 AM - 4:00 PM ET)

**Handler:** `ThesisHealthMonitor.run_health_check()`

**Checks:**
1. CHRONICLE connectivity & writability
2. APScheduler job status
3. Alpaca API & trading ability
4. Entry logic responsiveness
5. Position data consistency

**On Failure:**
- Auto-fix Tier 0: Reconnect
- Auto-fix Tier 1: Clear cache
- Auto-fix Tier 2: Restart jobs
- Auto-fix Tier 3: Escalate (after 3 consecutive failures)

**Logging:** `/Users/ahmedsadek/nexus/logs/thesis-health.log`

---

### Job 3: Mid-Session Update (12:15 PM ET)

**Cron:** `0 15 12 * * * America/New_York`

**Handler:** `ThesisStatusReporter.send_mid_session_update()`

**Reports:**
- Trades executed so far (last 5)
- Open positions (count + total unrealized P&L)
- Recent errors (last 4 hours)
- System health status

**Recipient:** Ahmed (8573754783) via Telegram DM

**Logging:** `/Users/ahmedsadek/nexus/logs/thesis-status.log`

---

### Job 4: Post-Close Summary (4:15 PM ET)

**Cron:** `0 15 16 * * * America/New_York`

**Handler:** `ThesisStatusReporter.send_post_close_summary()`

**Reports:**
- Total trades executed today
- Total P&L (if applicable)
- Best & worst trades
- Daily failure summary
- Tomorrow readiness (all systems healthy?)
- Current thesis context (regime, VIX, health)

**Recipient:** Ahmed (8573754783) via Telegram DM

**Logging:** `/Users/ahmedsadek/nexus/logs/thesis-status.log`

---

## Escalation Protocol

### Tier 3 Escalation (After 3 consecutive failures)

When a component fails 3 times in a row:

1. **Auto-fix attempts exhausted** → Stop retrying
2. **Alert to Ahmed** → Send Telegram message with:
   - Component name
   - Failure status
   - Detailed error message
   - Timestamp
3. **Log to CHRONICLE** → Save to `event_log` table with level=CRITICAL
4. **Pause entries** → Thesis entry logic pauses until fixed
5. **Manual intervention** → Ahmed/Axiom/Cipher diagnose and fix

---

## Testing

### Test Health Monitor

```bash
cd /Users/ahmedsadek/nexus/thesis
python -m thesis_health_monitor
```

Expected output:
```json
{
  "overall_status": "healthy",
  "checks": [
    {
      "component": "chronicle",
      "status": "healthy",
      "message": "Connected and writable...",
      "details": {"rows": 42},
      "timestamp": "..."
    },
    ...
  ],
  "autofix_summary": {...},
  "escalation_triggered": false
}
```

### Test Status Reporter

```bash
cd /Users/ahmedsadek/nexus/thesis
python -m thesis_status_reporter
```

This will send test messages to Ahmed's DM.

### Test Trading Validator

```bash
cd /Users/ahmedsadek/nexus/thesis
python -m thesis_trading_validator
```

This will:
1. Execute a real test trade (BUY 1 SPY)
2. Verify it appears
3. Close it
4. Verify closed
5. Report result to Ahmed

**WARNING:** This uses real Alpaca paper trading account. Only run during market hours.

---

## Monitoring & Logs

### Log Files

| Log File | Purpose |
|----------|---------|
| `/Users/ahmedsadek/nexus/logs/thesis-health.log` | Health check results & auto-fix attempts |
| `/Users/ahmedsadek/nexus/logs/thesis-status.log` | Status report sends & failures |
| `/Users/ahmedsadek/nexus/logs/thesis-trading-validator.log` | Trading validation results |

### View Real-Time Logs

```bash
# Health logs
tail -f /Users/ahmedsadek/nexus/logs/thesis-health.log

# Status logs
tail -f /Users/ahmedsadek/nexus/logs/thesis-status.log

# Trading validator logs
tail -f /Users/ahmedsadek/nexus/logs/thesis-trading-validator.log
```

### CHRONICLE Logging

All events logged to CHRONICLE `event_log` table:

```sql
SELECT * FROM event_log 
WHERE component IN ('thesis_health', 'thesis_status', 'trading_validator')
ORDER BY timestamp DESC
LIMIT 20;
```

---

## Deployment Checklist

- [ ] Copy all 4 Python files to `/Users/ahmedsadek/nexus/thesis/`
- [ ] Add requirements to `requirements.txt` and install
- [ ] Create `/Users/ahmedsadek/nexus/logs` directory
- [ ] Add environment variables to `.env`
- [ ] Add integration code to `thesis/main.py` lifespan
- [ ] Verify CHRONICLE schema has required tables
- [ ] Test each component individually (see Testing section)
- [ ] Start Thesis service and verify jobs appear in APScheduler
- [ ] Check that jobs run at scheduled times
- [ ] Receive test Telegram messages from Ahmed at 12:15 PM & 4:15 PM
- [ ] Verify health checks running every 30 minutes
- [ ] Confirm trading validation runs at 9:45 AM

---

## Production Readiness

### Before Going Live

1. **Test all components** during non-market hours
2. **Verify Telegram integration** works end-to-end
3. **Check CHRONICLE connectivity** from all running processes
4. **Verify Alpaca key/secret** are correct (paper trading account)
5. **Review logs** for any errors
6. **Run trading validator** once during market hours to confirm order execution works

### Failure Modes & Recovery

| Failure | Recovery |
|---------|----------|
| CHRONICLE unreachable | Auto-fix Tier 0: reconnect, Tier 1: clear cache, Tier 3: escalate |
| Alpaca API down | Auto-fix: reconnect with fresh auth, escalate if persistent |
| APScheduler hung | Auto-fix: restart jobs, escalate if repeatedly hangs |
| Telegram unavailable | Log error, continue health checks, retry on next cycle |
| Order execution fails | Alert Ahmed immediately, pause entries |

---

## Integration Notes

### APScheduler Job IDs

```python
"thesis_trading_validation"    # 9:45 AM
"thesis_health_check"          # Every 30 minutes
"thesis_mid_session_update"    # 12:15 PM
"thesis_post_close_summary"    # 4:15 PM
```

### Environment Variables Used

- `ALPACA_KEY` — API key ID
- `ALPACA_SECRET` — API secret key
- `TELEGRAM_BOT_TOKEN` — Axiom bot token
- `AHMED_TELEGRAM_ID` — Ahmed's Telegram chat ID (default: 8573754783)
- `THESIS_SERVICE_URL` — Local service URL (default: http://localhost:8060)
- `CHRONICLE_DB_PATH` — CHRONICLE SQLite path

### Telegram Messages

All messages sent to Ahmed's DM using Axiom bot (@Axiom15Bot).

Format: Markdown with emoji indicators:
- ✅ = OK / Healthy
- ⚠️ = Warning / Degraded
- 🔴 = Critical / Error
- 📊 = Report/Data
- 🚨 = Escalation

---

## Support & Debugging

### Enable Debug Logging

Add to Python code:
```python
logging.basicConfig(level=logging.DEBUG)
```

### Check Job Status

```bash
# From Thesis service health endpoint
curl http://localhost:8060/thesis/health
```

### Manual Job Trigger

```python
import asyncio
from thesis_health_monitor import ThesisHealthMonitor

monitor = ThesisHealthMonitor()
result = asyncio.run(monitor.run_health_check())
print(result)
```

---

## Version History

| Date | Version | Changes |
|------|---------|---------|
| 2026-06-01 | 1.0.0 | Initial production release |

---

## Author

**Ahmed Sadek** — Autonomous trading monitoring system  
**Configured for:** Thesis Service (http://localhost:8060)  
**Deployment Date:** 2026-06-01
