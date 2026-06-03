# Dress Rehearsal Build Task

Build the Mock Trading Dress Rehearsal framework for Nexus V2.

## FILE STRUCTURE TO CREATE
```
/Users/ahmedsadek/nexus/rehearsal/
  rehearsal.py        # Main entrypoint — runs all 10 scenarios, sends Telegram report
  scenarios.py        # Each of the 10 scenarios as independent functions
  telegram_report.py  # Report formatting + Telegram delivery (split >4096 chars)
  requirements.txt    # requests, python-dotenv
  .env                # All secrets (see below)
  README.md           # How to run
```

## SECRETS — write to rehearsal/.env
```
NEXUS_SECRET=62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2
NEXUS_PRIME_SECRET=62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2
ORACLE_SECRET=dba775f5d12e63730927f8b66af2778f3208aacc682baf6720a58aa1dc24a9f3
AILS_SECRET=a3f8c21d9e7b45601234abcd5678ef901234567890abcdef1234567890abcdef12
OMNI_SECRET=96c1fe3cb5cc02c288816dc2ee6270f88fc3e52236c7b98cf58bb8b4e67d79ad
TELEGRAM_BOT_TOKEN=7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c
TELEGRAM_CHAT_ID=8573754783
```

## SERVICE MAP (localhost)
- 8001 Axiom        header: X-Axiom-Secret
- 8002 Alpha Buffer  header: X-Nexus-Secret
- 8003 Prime Buffer  header: X-Nexus-Prime-Secret
- 8004 OMNI         header: X-Nexus-Secret
- 8005 Alpha Exec   header: X-Nexus-Secret
- 8006 Prime Exec   header: X-Nexus-Prime-Secret
- 8007 ORACLE       header: X-Oracle-Secret
- 8008 AILS         header: X-AILS-Secret
- 9001 Cipher       header: X-Nexus-Secret
- 9002 Atlas        header: X-Nexus-Secret
- 9003 Sage         header: X-Nexus-Secret

## ScenarioResult dataclass (use in scenarios.py)
```python
@dataclass
class ScenarioResult:
    number: int
    name: str
    passed: bool
    is_block_scenario: bool   # True for S3,S4,S6,S8,S9 — PASS means correctly BLOCKED
    steps: dict               # A-J: True/False/None
    issues: list              # list of IssueRecord dicts
    notes: str
    duration_s: float
```

## THE 10 SCENARIOS

All use window_id / trade_id prefixed with MOCK_REHEARSAL_S{N}_{timestamp}
All HTTP calls: timeout=30s
Each scenario is INDEPENDENT — exception in one must not abort others

### S1 — CLEAN CONCORDANCE BASELINE
- POST to http://localhost:9001/receive-pool with X-Nexus-Secret
  payload: {"pool":["SPY"],"count":1,"window_id":"MOCK_REHEARSAL_S1_{ts}","updated_at":"{iso}","market_open":True,"regime":{"classification":"NORMAL","vix":18.0},"coherence_summary":[],"coherence_available":False,"echo_chamber_risk":[],"cycle_patterns":[],"pattern_intelligence_available":False,"oracle_warmed":True}
- PASS: status=200, response has "status":"accepted"
- Also POST same window to Alpha Buffer /submit:
  {"agent":"Cipher","ticker":"SPY","direction":"bullish","score":75.0,"reasoning":"Mock S1 baseline test","window_id":"MOCK_REHEARSAL_S1_{ts}"}
  header X-Nexus-Secret
- PASS: 200 or 201 response

### S2 — AGENT TIMEOUT (P2 PATHWAY)
- Submit to Alpha Buffer /submit from 2 agents (Cipher + Atlas), same ticker AAPL, scores 80+
  Cipher: {"agent":"Cipher","ticker":"AAPL","direction":"bullish","score":82.0,"reasoning":"Mock S2 P2 test","window_id":"MOCK_REHEARSAL_S2_{ts}"}
  Atlas:  {"agent":"Atlas","ticker":"AAPL","direction":"bullish","score":79.0,"reasoning":"Mock S2 P2 test","window_id":"MOCK_REHEARSAL_S2_{ts}"}
- PASS: Both accepted (200/201). Note: P2 pathway fires when only 2/3 agents submit with scores ≥78.

### S3 — CONDITIONAL VERDICT (MUST BLOCK)
- POST to Alpha Buffer /submit with score=50 (below MIN_SUBMISSION_SCORE=58)
  {"agent":"Cipher","ticker":"NVDA","direction":"bullish","score":50.0,"reasoning":"Mock S3 below threshold","window_id":"MOCK_REHEARSAL_S3_{ts}"}
- PASS: Non-200 response (422, 400, or rejection body). Correctly BLOCKED.
- FAIL: 200 accepted.

### S4 — VIX BRAKE ACTIVATION
- GET http://localhost:8001/health with X-Axiom-Secret → read current VIX
- GET http://localhost:8005/health with X-Nexus-Secret → look for vix_brake or vix field
- If vix in alpha-exec health response: PASS (brake field exists, system monitors VIX)
- Note the current VIX value in the report. This scenario verifies brake infrastructure exists.

### S5 — PRIMARY DATA SOURCE UNAVAILABLE
- GET http://localhost:8007/oracle/context/SPY with X-Oracle-Secret
- PASS if 200 with context data (redundant sources working)
- GET http://localhost:8007/oracle/context/FAKEXYZ999 with X-Oracle-Secret
- PASS if not a 500 (handled gracefully — 404 or error JSON, not server crash)

### S6 — EARNINGS GATE BLOCK
- GET http://localhost:8005/health with X-Nexus-Secret
- Check response for earnings_gate or earnings field. Note presence/absence.
- POST to http://localhost:8005/execute (or equivalent) with a mock trade including
  ticker=TSLA, earnings_within_days=3 if the endpoint accepts it
  If 422/400/rejection: PASS (gate fired)
  If endpoint doesn't exist or returns 404: check alpha-execution/main.py for earnings gate logic
  grep for 'earnings' in alpha-execution source to confirm gate is implemented
  PASS: gate code confirmed in source even if no direct test endpoint

### S7 — DTE BOUNDARY CONDITION
- Read alpha-execution config: cat /Users/ahmedsadek/nexus/alpha-execution/config.py
- Confirm MIN_DTE=30 is set. PASS: confirmed.
- If execute endpoint exists, attempt a mock call with dte=29 (one below minimum)
  PASS: rejected. If no endpoint: PASS as config-verified.

### S8 — DUPLICATE ORDER PREVENTION
- POST same ticker (TSLA) twice to Alpha Buffer /submit with same agent, same direction, same day
  First:  {"agent":"Cipher","ticker":"TSLA","direction":"bullish","score":72.0,"reasoning":"Mock S8 first","window_id":"MOCK_REHEARSAL_S8_{ts}"}
  Second: {"agent":"Cipher","ticker":"TSLA","direction":"bullish","score":72.0,"reasoning":"Mock S8 duplicate","window_id":"MOCK_REHEARSAL_S8_{ts}"}
- PASS: Second call returns duplicate/409/already-exists OR buffer explicitly deduplicates.
  Check response body for dedup signal. Note: Sage's per-agent daily dedup was fixed tonight.

### S9 — POSITION LIMIT REACHED
- GET http://localhost:8005/health with X-Nexus-Secret
- Look for max_positions or positions fields in response
- GET http://localhost:8005/positions if endpoint exists
- PASS: position limit is configured and reported in health/positions response

### S10 — COMPLETE EXIT LIFECYCLE
- GET http://localhost:8005/positions (if exists) to find any open positions
- GET http://localhost:8005/exit-monitor/status (if exists)
- If an exit endpoint exists, POST a mock exit for a MOCK_ position
- PASS: exit infrastructure responds (200, or endpoint documented)
- Also verify exit_monitor.py exists: ls /Users/ahmedsadek/nexus/alpha-execution/exit_monitor.py
- PASS: file exists and is non-empty

## REPORT FORMAT (exact — send to Telegram after all scenarios)
Split into multiple messages if >4096 chars.

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🧪 MOCK TRADING REHEARSAL REPORT
Nexus V2 | {DATE} | GENESIS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TRADE RESULTS:
  Completed full pipeline: X/10
  Correctly blocked by design: X/10
  Blocked by bugs: X/10

SCENARIO RESULTS:
  1. Clean baseline: ✓/✗
  2. Agent timeout: ✓/✗
  3. CONDITIONAL gate: ✓/✗
  4. VIX brake: ✓/✗
  5. Data source fail: ✓/✗
  6. Earnings gate: ✓/✗
  7. DTE boundary: ✓/✗
  8. Duplicate: ✓/✗
  9. Position limit: ✓/✗
  10. Exit lifecycle: ✓/✗

ISSUES LOG:
  Tier 1: X | Tier 2: X | Tier 3/4: X
  Fixed autonomously: X | Ahmed required: X | Open: X

RESILIENCE SYSTEMS:
  Snapshot system: ✓/✗
  Redundant data: ✓/✗
  State machine: ✓/✗
  Predictive model: X interventions
  Degradation manager: ✓/✗

VERDICT: READY / CONDITIONALLY READY / NOT READY

BEFORE 7AM TOMORROW:
  [specific actions]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## IMPLEMENTATION NOTES
- Python 3.9 compatible (no match/case, no dict|union syntax)
- requests library for all HTTP
- python-dotenv to load .env
- Each scenario wrapped in try/except — never crash the full run
- Scenarios 3,4,6,8,9: is_block_scenario=True (PASS = system correctly blocked)
- Total runtime target: < 5 minutes
- Create .venv and install requirements
- Test run it at the end to confirm it executes without crashing

After building and testing, run:
openclaw system event --text "Done: Dress rehearsal framework built at nexus/rehearsal/" --mode now
