# ALERT_VERIFICATION_RULES.md

**Purpose:** Define verification success criteria and methods for each alert type.

Each alert that fires requires **automatic verification** after Ahmed ACKs it. Verification confirms that the underlying issue was actually fixed, not just acknowledged.

---

## Verification Lifecycle

```
Alert fires
├─ Ahmed receives + ACKs
├─ Verification triggered automatically
├─ System checks concrete evidence of fix
├─ Result: VERIFIED, FAILED_VERIFICATION, or MANUAL_REQUIRED
└─ Log outcome + timestamp
```

---

## Alert Type Definitions & Verification Rules

### 1. ORPHAN_POSITION

**What triggers it:**
- Position exists in Nexus tracking but no matching buy/sell order in Alpaca
- Zombie position that cannot be exited via normal channels

**Success Criteria:**
- Position has been closed in Alpaca (no longer appears in open positions list)
- OR position is manually documented as acknowledged/exempt

**Verification Method:**
```
Call: GET /v2/positions (Alpaca API)
Check: position_id NOT in response body
Success: HTTP 200 + position absent from list
Failure: HTTP 200 + position still present
Timeout: 5 minutes
```

**Evidence Required:**
- Alpaca /positions API call result
- Timestamp of position closure
- Order ID if position was closed via order

**Manual Override:**
- If position is legitimately locked (settlement, regulatory hold, etc.), mark as ACKNOWLEDGED

---

### 2. AGENT_HUNG

**What triggers it:**
- Agent process heartbeat missed for >30 seconds
- Last log message is >60s old
- Agent assumed to be stalled or crashed

**Success Criteria:**
- Process is running (appears in `ps`)
- Heartbeat message logged within last 30 seconds
- Next task execution completed successfully

**Verification Method:**
```
Check 1: Process Status
  Command: ps aux | grep <agent_name>
  Success: Process running + PID matches last known PID
  
Check 2: Recent Heartbeat
  Command: tail -20 <agent_log_file> | grep -i "heartbeat\|alive\|tick"
  Success: Most recent message is <30s old
  
Check 3: Task Execution
  Command: Check latest task completion timestamp in logs
  Success: New task completed within last 2 minutes
  
Timeout: 2 minutes (parallelizable checks)
```

**Evidence Required:**
- PID of running process
- Timestamp of most recent heartbeat/log
- Last task completion time

**Manual Override:**
- If agent is in graceful shutdown, mark as ACKNOWLEDGED

---

### 3. CODE_FAILURE

**What triggers it:**
- Tests failing in CI/CD pipeline
- Deployment rejected due to code quality / test failures
- Service crash due to unhandled exception in new code

**Success Criteria:**
- All tests pass (including new/changed tests)
- Code deployed to production
- Service running without crashing for >5 minutes after deploy

**Verification Method:**
```
Check 1: Test Status
  Command: pytest <changed_files> -v
  Success: All tests pass, exit code 0
  
Check 2: Deployment Status
  Command: git log --oneline | head -1 (get last deploy SHA)
  Command: Check Railway/deployment status for that SHA
  Success: Deployment marked SUCCEEDED, service running
  
Check 3: Service Health
  Command: curl <service_health_endpoint>
  Success: HTTP 200 + no error indicators in response
  
Timeout: 10 minutes (includes CI/CD wait + deployment time)
```

**Evidence Required:**
- Test run output (stdout from pytest)
- Git commit SHA of deployed code
- Service version from /health endpoint
- Uptime since deployment (no crashes)

**Manual Override:**
- If code change is intentionally rollback, mark as ACKNOWLEDGED

---

### 4. CONFIG_MISMATCH

**What triggers it:**
- Configuration value differs across services that should be synchronized
- MAX_POSITIONS, MAX_RISK_PER_TRADE, or other critical params diverge
- Security/operational parameter inconsistency detected

**Success Criteria:**
- Configuration value is identical across all 9 Nexus services
- OR inconsistency is documented as intentional + approved

**Verification Method:**
```
Check: Configuration Consistency
  Services: [alpha-execution, alpha-ingestion, alpha-monitor, 
             prime-execution, prime-ingestion, prime-monitor,
             axiom, concordance, chronicle]
  
  For each service, fetch:
    Command: curl -s <service_api>/config (or read config file)
    Extract: value of mismatched parameter
    
  Success: value == expected_value for ALL services
  Failure: value != expected_value for ANY service
  
  Timeout: 3 minutes
```

**Evidence Required:**
- Parameter name and value from each service
- Config source file/endpoint
- Timestamp of config check

**Manual Override:**
- If mismatch is intentional (A/B test, canary, etc.), document reason + expiry

---

### 5. POLICY_VIOLATION

**What triggers it:**
- Agent violated operational constraint (e.g., ordered more than MAX_POSITIONS)
- Compliance rule breach detected (spending limit, market hours, etc.)
- Agent requested forbidden action (manual override, emergency liquidation, etc.)

**Success Criteria:**
- Agent compliance log shows violation was CORRECTED
- Next N requests from agent are POLICY_COMPLIANT
- No further violations logged in the last 5 minutes

**Verification Method:**
```
Check 1: Violation Logged
  Command: grep -i "violation\|policy\|breach" <agent_compliance_log>
  Success: violation entry found + marked CORRECTED
  
Check 2: Corrected Behavior
  Command: grep -A5 <violation_entry> <agent_compliance_log>
  Success: Next 3+ requests have compliance_status = PASS
  
Check 3: No Recent Re-violations
  Command: grep -i "violation" <agent_compliance_log> | tail -20
  Success: No new violations in last 5 minutes
  
Timeout: 5 minutes (waits for next 3 requests)
```

**Evidence Required:**
- Violation log entry + timestamp
- Corrective action taken
- Timestamps of 3+ compliant requests after violation

**Manual Override:**
- If violation was intentional emergency action (approved by Ahmed), mark ACKNOWLEDGED

---

### 6. THREAT_DETECTED

**What triggers it:**
- Agent sending suspicious requests (possible compromise/exploit)
- Rate abnormality detected (10x normal request volume)
- Unauthorized action attempt (liquidation, high-risk order, etc.)

**Success Criteria:**
- Agent has been quarantined (isolated from live execution)
- No new suspicious requests sent after quarantine
- Investigation logged + root cause documented

**Verification Method:**
```
Check 1: Agent Quarantine Status
  Command: Check agent_quarantine table in Chronicle
  Query: SELECT quarantine_status FROM agents WHERE agent_name = '<agent>'
  Success: quarantine_status = TRUE
  
Check 2: Request Activity
  Command: Check latest requests from agent in request_log
  Success: No requests sent in last 5 minutes OR all requests marked QUARANTINED
  
Check 3: Investigation Status
  Command: Check threat_investigation table in Chronicle
  Success: threat_id marked INVESTIGATED + root_cause documented
  
Timeout: 2 minutes
```

**Evidence Required:**
- Quarantine status + timestamp
- Last request timestamp from agent
- Investigation report + root cause determination

**Manual Override:**
- If threat was false positive, unquarantine + document why

---

## Verification State Machine

```
After ACK received:
  State 1: VERIFICATION_TRIGGERED
    └─ Determine alert type
    └─ Route to appropriate checker
    
  State 2: VERIFICATION_IN_PROGRESS
    └─ Run checker (async, polling, or webhook)
    └─ Collect evidence
    
  State 3: VERIFICATION_COMPLETE (one of three outcomes)
    ├─ VERIFIED: Evidence confirms issue is fixed
    │  └─ Mark alert RESOLVED
    │  └─ Log success + timestamp
    │  
    ├─ FAILED_VERIFICATION: Evidence shows issue NOT fixed
    │  └─ Re-escalate to Ahmed
    │  └─ Send: "ACK received but action didn't work. Issue still present: <evidence>"
    │  └─ Stay in ESCALATE state
    │  
    └─ MANUAL_REQUIRED: Cannot auto-verify (e.g., manual order closure needed)
       └─ Flag for manual review
       └─ Extend SLA: notify Ahmed "awaiting manual confirmation"
       └─ Require explicit: "VERIFIED" response from Ahmed before closing
```

---

## Timeout Handling

Each alert type has a **verification timeout** (see above). If verification cannot complete within that timeout:

**Action on Timeout:**
1. Log timeout event: `"Verification timeout for <alert_type> after <timeout_mins> minutes"`
2. Escalate with partial results: `"Could not fully verify. Last check result: <status>. Manual review required."`
3. Keep alert in ESCALATE state until manual resolution

---

## Verification Logging

Every verification attempt is logged to Chronicle `verification_log` table:

```sql
CREATE TABLE verification_log (
  id INTEGER PRIMARY KEY,
  alert_id TEXT NOT NULL,
  alert_type TEXT NOT NULL,
  attempt_number INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  result VARCHAR(50) NOT NULL,  -- VERIFIED, FAILED, MANUAL, TIMEOUT
  evidence TEXT,                 -- JSON: details of what was checked
  root_cause TEXT,               -- If FAILED, why it failed
  created_at TEXT NOT NULL
);
```

Example log entry:
```json
{
  "alert_id": "genesis-1715857200-1",
  "alert_type": "ORPHAN_POSITION",
  "result": "VERIFIED",
  "evidence": {
    "check_type": "alpaca_positions_api",
    "position_id": "aapl-long-1000",
    "api_call": "GET /v2/positions",
    "http_status": 200,
    "position_found": false,
    "timestamp": "2026-05-16T17:34:00Z"
  },
  "completed_at": "2026-05-16T17:34:12Z"
}
```

---

## Future Extensions

1. **Webhook-based verification** — Alert issuer provides callback URL for verification result
2. **Multi-stage verification** — Some alerts require 2+ stages (e.g., close position + confirm no rebalance)
3. **Verification retry logic** — Auto-retry on transient failures (timeouts, service unavailable)
4. **Conditional verification** — Skip verification for known false alarms or acknowledged classes
5. **Verification SLA tracking** — Alert if verification takes longer than expected

---

**Author:** GENESIS 🌱  
**Date:** 2026-05-16  
**Status:** ACTIVE
