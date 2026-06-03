# PROTOCOLS.md — PROBE Review Protocol

## How to Conduct a Review

### Step 1: Receive Request
POST /review with service_name, service_version, review_cycle_id, code_files, spec_content.
Return 202 immediately. Never block the caller.

### Step 2: Create CHRONICLE Entry
Write IN_PROGRESS row to critique_bank before calling the brain.
If CHRONICLE is down, cache locally and continue.

### Step 3: Build Brain Prompt
System prompt: PROBE specialization (security/logic focus)
User prompt: service_name + spec_content + all code_files concatenated

### Step 4: Call DeepSeek V3
3 retries with exponential backoff (1s, 2s, 4s).
If all 3 fail: mark FAILED in CHRONICLE, report CRITICAL to SOVEREIGN.

### Step 5: Parse Findings
Extract JSON findings array from brain response.
Normalize severity to P0/P1/P2/P3.
Compute p-counts and overall_assessment (FAIL/CONDITIONAL/PASS).

### Step 6: Write to CHRONICLE
Update critique_bank row: status=COMPLETED, all p-counts, full_report JSON.

### Step 7: Report to SOVEREIGN
sovereign_comms.report() with EscalationLevel.INFO:
{review_cycle_id, service_name, overall_assessment, p0_count, p1_count, chronicle_id}

## Severity Rules
- FAIL: p0_count > 0 (any security critical finding)
- CONDITIONAL: p1_count > 3 (more than 3 logic bugs)
- PASS: all other cases

## Brain Failure Protocol
1. Log error at WARNING level with attempt number
2. Wait exponential backoff
3. After 3 failures: mark CHRONICLE row FAILED, report CRITICAL to SOVEREIGN
4. Increment brain_failures_today counter
5. Alert Ahmed via Telegram fallback if bus also down
