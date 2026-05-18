# AXIOM Inspector General Module Specification

**Status:** SPEC (awaiting Ahmed approval)  
**Author:** GENESIS  
**Date:** 2026-05-16  
**Mandate:** Ahmed Sadek (May 16, 2026)  

---

## Purpose

Implement AXIOM's Inspector General role: autonomous, continuous audit of Nexus system integrity across 10 domains. Detect anomalies, escalate critical issues to SOVEREIGN/GENESIS within SLA, and maintain operational trust.

The module runs as a background scheduler within the Axiom service. It does not block pool curation or risk assessment — it runs in parallel on independent cycles.

---

## Inputs

### Runtime State
- **app_state dict:** pool, regime, last_vix, regime_last_updated, settings
- **Axiom database:** axiom.db → audit_log table (trades, regimes, pool refreshes)
- **Alpaca paper account:** /accounts endpoint to fetch current positions and capital
- **Scheduler jobs:** list of scheduled tasks from APScheduler

### External APIs
- 9 Nexus services: health endpoints + auth verification
- 3 agent webhooks: Cipher, Sage, Atlas (GET reachability + POST response time test)
- External data APIs: Polygon, ORATS, Alpha Vantage, FRED

### Configuration
- Thresholds from axiom/config.py:
  - MAX_POSITIONS, MAX_RISK_PER_TRADE, MIN_DTE, MAX_DTE, VIX_PAUSE_THRESHOLD
  - MIN_IVR_CREDIT_SPREAD, MAX_IVR_DEBIT_SPREAD
- Secrets from .env: AXIOM_SECRET, NEXUS_SECRET, ORACLE_SECRET, etc.

### Logs
- Axiom service logs (stdio capture from FastAPI lifespan)
- Audit log table in axiom.db (all /assess, /pool, /regime calls)

---

## Outputs

### Report Format

All reports routed via `shared.sovereign_comms.report(agent, level, payload)`:

```python
report("axiom-inspector", level, {
    "domain": int,                           # 1–10
    "event": str,                            # machine-readable event name
    "severity": "CRITICAL|WARNING|INFO",     # redundant with level but explicit
    "timestamp": "ISO-8601 ET",              # when detected
    "finding": str,                          # human-readable description
    "impact": str,                           # what breaks if not fixed
    "root_cause": str or None,               # if known
    "required_action": str or None,          # what SOVEREIGN/GENESIS should do
    "data": dict,                            # domain-specific payload
})
```

### Escalation Routing

**CRITICAL** → SOVEREIGN immediately (< 30s from detection)
- Auth header failures (any 403)
- VIX unavailable > 5 min
- Pool empty
- Regime stale > 90 min
- Circuit breaker failure (VIX > 35 but submissions open)
- Config mismatch > $100 or > 1 position
- Audit log gap

**WARNING** → SOVEREIGN within 5 min (can batch)
- Webhook timeout / unreachable
- External API 429 rate-limit
- P95 latency > 3s for > 5 consecutive calls
- VIX estimated (not real-time) for > 3 consecutive refreshes
- Minor config discrepancy (< $100, 0 positions)

**INFO** → Log to CHRONICLE only (no Telegram escalation)
- Routine health checks passing
- Normal latency
- Pool refresh successful

### Daily Reports (to SOVEREIGN via Telegram)

**Pre-Market Sweep (8:30 AM ET)**
```json
{
    "event": "pre_market_sweep",
    "timestamp": "2026-05-16T08:30:00-04:00",
    "findings": [
        {"domain": 1, "status": "OK", "detail": "Auth headers verified (8/8 services)"},
        {"domain": 4, "status": "OK", "detail": "Agent webhooks reachable (3/3)"},
        {"domain": 6, "status": "OK", "detail": "External APIs healthy"}
    ],
    "escalations": 0,
    "action_required": false
}
```

**Post-Market Audit (4:30 PM ET)**
```json
{
    "event": "post_market_audit",
    "timestamp": "2026-05-16T16:30:00-04:00",
    "findings": [
        {"domain": 2, "status": "OK", "detail": "Config consistency: 0 mismatches"},
        {"domain": 10, "status": "OK", "detail": "Audit log: 47 trades logged, 18 regimes, 2 pools, 0 gaps"}
    ],
    "performance": {
        "p50_assess_latency_ms": 180,
        "p95_assess_latency_ms": 420,
        "p99_assess_latency_ms": 680
    },
    "alerts": [],
    "action_required": false
}
```

**Weekly Summary (Friday 5 PM ET)**
```json
{
    "event": "weekly_summary",
    "week_ending": "2026-05-17",
    "kpis": {
        "auth_integrity": "100%",
        "config_consistency": "100%",
        "webhook_health": "100%",
        "api_uptime": "99.8%",
        "audit_log_completeness": "100%",
        "mean_escalation_time": "45s",
        "mean_resolution_time": "8m45s"
    },
    "escalations": [
        {"date": "2026-05-13", "domain": 8, "severity": "WARNING", "cause": "VIX estimated 3x (Polygon down)"}
    ],
    "trends": {
        "latency_trend": "stable",
        "api_reliability_trend": "improving",
        "config_drift_trend": "none"
    },
    "recommendations": [
        "Polygon latency increasing — may want caching layer"
    ]
}
```

---

## Contracts (Guarantees)

### Availability
- Inspector General runs continuously during market hours (9 AM–4 PM ET)
- Scheduled jobs: pre-market (8:30 AM), market-hours sweeps (every 15 min), post-market (4:30 PM), weekly (Friday 5 PM)
- Does not block Axiom's pool curation or risk assessment
- Graceful degradation: if one audit fails, others continue

### Latency
- Auth verification: < 500ms per service (9 services → < 5s total)
- Webhook test: < 3s per webhook (3 webhooks → < 10s total)
- Config audit: < 1s (all in-memory comparison)
- Full sweep: < 30s start-to-finish (market-hours check)

### Data Accuracy
- Every escalation includes root cause analysis or explicit "root_cause: null, investigation needed"
- No false positives: only escalate when threshold actually exceeded
- Tolerance for transient errors: single API timeout ≠ escalation; 3 consecutive timeouts = escalation

### Idempotency
- Repeated audits of same state produce same finding (deterministic)
- Escalations de-duplicated: if same issue escalated 2 hours ago and not resolved, escalate again; otherwise suppress duplicate

### Error Handling
- All external API calls wrapped in try-except with timeout (3–10s depending on endpoint)
- Partial failures do not crash the sweep: if Polygon unavailable, ORATS still checked
- Network errors logged but do not escalate unless they persist > 5 min
- Database errors (axiom.db read) do not escalate; logged to Axiom stderr

---

## Failure Modes

### Failure: Polygon API down
- **Detection:** GET /pool calls Polygon; returns 502/503/timeout
- **Response:** Try Yahoo Finance fallback immediately; if both fail for > 5 min, escalate WARNING
- **Contract:** Data freshness still available via FRED or estimated VIX; no service halt

### Failure: ORATS API 429 (rate-limited)
- **Detection:** ORATS returns 429 Too Many Requests
- **Response:** Log, back off 60s, retry; do not escalate unless > 5 min
- **Contract:** ORATS calls may slow down but IVR scores still available from cache if fresh

### Failure: Agent webhook unreachable
- **Detection:** POST to Cipher/Sage/Atlas webhook times out or returns 5xx
- **Response:** Log, do not pause submissions (pool curation continues)
- **Impact:** Agents cannot receive pool updates; submission will retry on next cycle
- **Escalation:** WARNING to SOVEREIGN (informational, not blocking)

### Failure: Config mismatch detected
- **Detection:** Axiom's MAX_POSITIONS ≠ Alpha Buffer's MAX_POSITIONS
- **Response:** Log mismatch; if > $100 notional or > 1 position in conflict, escalate CRITICAL
- **Required Action:** GENESIS reviews code; values should be pulled from shared config source

### Failure: Audit log gap detected
- **Detection:** Trade timestamps jump > 60 min with no regime/pool refresh logs in between
- **Response:** Flag as potential data loss; escalate to GENESIS with log excerpt
- **Investigation:** GENESIS checks if Axiom crashed, if submissions were paused, if database write failed

### Failure: VIX data becomes all estimated (no real-time source)
- **Detection:** VIX estimated for 3 consecutive refreshes (> 45 min)
- **Response:** Escalate WARNING to SOVEREIGN; pool curation continues but flagged as "high uncertainty"
- **Decision:** SOVEREIGN may choose to pause submissions or continue with risk flag

### Failure: Circuit breaker misconfiguration
- **Detection:** VIX > 35 but /pool returns submissions_open=true
- **Response:** Escalate CRITICAL immediately (trading may be happening in high-risk regime)
- **Required Action:** GENESIS checks Axiom's VIX_PAUSE_THRESHOLD logic; Alpha Buffer's submission gate

---

## Test Cases

### Unit Tests (test_inspector.py)

#### Domain 1: Auth Headers
```python
def test_auth_header_all_services_ok():
    """Verify all 9 services return 200 with correct header."""
    # Mock responses: all return 200 OK
    findings = inspector.audit_auth_headers()
    assert findings["status"] == "OK"
    assert findings["passed"] == 8
    assert findings["failed"] == 0

def test_auth_header_single_service_403():
    """Verify escalation when one service returns 403."""
    # Mock: Alpha Buffer returns 403
    findings = inspector.audit_auth_headers()
    assert findings["status"] == "CRITICAL"
    assert findings["failed_service"] == "Alpha Buffer"
    # Should escalate immediately
```

#### Domain 2: Config Consistency
```python
def test_config_consistency_all_match():
    """Verify no drift when all services have same thresholds."""
    # Mock Axiom config: MAX_POSITIONS=3
    # Mock Alpha Buffer: MAX_POSITIONS=3
    # Mock Prime Buffer: MAX_POSITIONS=3
    findings = inspector.audit_config_consistency()
    assert findings["status"] == "OK"
    assert findings["mismatches"] == 0

def test_config_consistency_mismatch_critical():
    """Verify escalation when config mismatch is > $100."""
    # Axiom: MAX_POSITIONS=3, Alpha Buffer: MAX_POSITIONS=5 (extra 2 × $1000 = $2000)
    findings = inspector.audit_config_consistency()
    assert findings["status"] == "CRITICAL"
    assert findings["mismatch_usd"] == 2000.0
```

#### Domain 3: Data Integrity
```python
def test_pool_integrity_non_empty():
    """Verify pool is non-empty and universe-consistent."""
    app_state["pool"] = ["NVDA", "MSFT", "AAPL"]
    findings = inspector.audit_data_integrity()
    assert findings["pool_status"] == "OK"
    assert findings["pool_size"] == 3

def test_pool_integrity_empty_critical():
    """Verify escalation when pool is empty."""
    app_state["pool"] = []
    findings = inspector.audit_data_integrity()
    assert findings["pool_status"] == "CRITICAL"
    # Escalate immediately
```

#### Domain 4: Webhook Health
```python
def test_webhooks_all_reachable():
    """Verify all agent webhooks respond in < 3s."""
    # Mock: Cipher, Sage, Atlas all return 200 in < 1s
    findings = inspector.audit_webhooks()
    assert findings["status"] == "OK"
    assert len(findings["webhooks"]) == 3
    assert all(w["latency_ms"] < 3000 for w in findings["webhooks"])

def test_webhooks_timeout_warning():
    """Verify WARNING when webhook times out."""
    # Mock: Cipher webhook times out after 5s
    findings = inspector.audit_webhooks()
    assert findings["status"] == "WARNING"
    assert findings["timeouts"][0]["webhook"] == "Cipher"
```

#### Domain 6: External APIs
```python
def test_external_apis_all_ok():
    """Verify all external APIs reachable."""
    findings = inspector.audit_external_apis()
    assert findings["status"] == "OK"
    assert findings["polygon"] == "200 OK"
    assert findings["orats"] == "200 OK"
    assert findings["fred"] == "200 OK"

def test_external_apis_critical_down():
    """Verify escalation when critical API down > 5 min."""
    # Mock: Polygon returns 502 for 6 min straight
    findings = inspector.audit_external_apis(consecutive_failures=6)
    assert findings["status"] == "CRITICAL"
    assert findings["required_action"] == "investigate_polygon_api"
```

#### Domain 7: Performance
```python
def test_latency_p95_ok():
    """Verify P95 latency under 3s."""
    # Mock: 100 /assess calls, P95 = 420ms
    findings = inspector.audit_performance(latencies=[180, 220, 420, 250, ...])
    assert findings["status"] == "OK"
    assert findings["p95_ms"] == 420

def test_latency_p95_warning():
    """Verify WARNING when P95 > 3s for > 5 calls."""
    # Mock: 5 consecutive /assess calls at 3500ms each
    findings = inspector.audit_performance(latencies=[3500]*5)
    assert findings["status"] == "WARNING"
    assert findings["consecutive_slow"] == 5
```

#### Domain 8: VIX Freshness
```python
def test_vix_real_time_ok():
    """Verify VIX is real-time (not estimated)."""
    # app_state["last_vix"] = 28.5, is_estimated = False
    findings = inspector.audit_vix_freshness()
    assert findings["status"] == "OK"
    assert findings["is_estimated"] == False

def test_vix_estimated_warning():
    """Verify WARNING when VIX estimated for > 3 refreshes."""
    # app_state: last 4 VIX refreshes all estimated=True
    findings = inspector.audit_vix_freshness(consecutive_estimated=4)
    assert findings["status"] == "WARNING"
    assert findings["message"] == "VIX estimated for 4 consecutive refreshes"
```

#### Domain 9: Circuit Breaker
```python
def test_circuit_breaker_vix_35_pauses():
    """Verify submissions pause when VIX > 35."""
    # Mock: VIX = 36.0
    app_state["regime"].vix = 36.0
    findings = inspector.audit_circuit_breaker()
    assert findings["status"] == "OK"
    assert findings["submissions_paused"] == True

def test_circuit_breaker_failure_critical():
    """Verify CRITICAL when VIX > 35 but submissions still open."""
    # Mock: VIX = 38.0, but /pool returns submissions_open=true
    app_state["regime"].vix = 38.0
    app_state["submissions_open"] = True
    findings = inspector.audit_circuit_breaker()
    assert findings["status"] == "CRITICAL"
    assert findings["required_action"] == "investigate_submissions_gate"
```

#### Domain 10: Audit Log Completeness
```python
def test_audit_log_no_gaps():
    """Verify audit log has no gaps over past 24h."""
    # Mock: trades logged at 9:30, 10:15, 11:00, ..., 15:45 (every 45 min avg)
    findings = inspector.audit_log_completeness()
    assert findings["status"] == "OK"
    assert findings["gaps"] == 0
    assert findings["trades_logged"] == 47

def test_audit_log_gap_critical():
    """Verify CRITICAL when audit log has > 60 min gap."""
    # Mock: trades at 10:00, then gap until 11:15 (75 min)
    findings = inspector.audit_log_completeness()
    assert findings["status"] == "CRITICAL"
    assert findings["max_gap_min"] == 75
    assert findings["required_action"] == "investigate_data_loss"
```

### Integration Tests (test_inspector_integration.py)

```python
def test_full_pre_market_sweep():
    """Run all 10 audits; verify none crash and all report."""
    findings = inspector.run_pre_market_sweep()
    assert len(findings) == 10
    assert all(f["domain"] in range(1, 11) for f in findings)
    # Should complete in < 30s
    assert findings["total_time_ms"] < 30000

def test_escalation_routing():
    """Verify CRITICAL escalations route to SOVEREIGN immediately."""
    # Mock: Config mismatch detected
    findings = inspector.audit_config_consistency()
    if findings["status"] == "CRITICAL":
        inspector.escalate_to_sovereign(findings)
        # Verify call to shared.sovereign_comms.report() was made
        assert sovereign_comms.report.called
        assert sovereign_comms.report.call_args[0][1] == "CRITICAL"

def test_deduplication():
    """Verify same escalation not sent twice in < 2h window."""
    # Escalate VIX API down at 10:15 AM
    inspector.escalate_to_sovereign(vix_api_down_finding)
    # Same finding detected at 10:16 AM
    inspector.escalate_to_sovereign(vix_api_down_finding)
    # Should de-duplicate; only 1 call to Telegram
    assert telegram_calls == 1
```

---

## Build Approach

### Module: `axiom/inspector.py`

**Structure:**
```python
# Inspector class with methods per domain
class AxiomInspector:
    def __init__(self, app_state, settings, logger):
        self.app_state = app_state
        self.settings = settings
        self.logger = logger
        self.escalation_history = {}  # Deduplication

    def audit_auth_headers(self) -> dict:
        """Domain 1: Auth header verification."""
    
    def audit_config_consistency(self) -> dict:
        """Domain 2: Configuration consistency."""
    
    # ... audit_* methods for domains 3–10
    
    def run_pre_market_sweep(self) -> list[dict]:
        """Run all 10 audits, return findings."""
    
    def escalate_to_sovereign(self, finding: dict) -> None:
        """Route finding to SOVEREIGN with deduplication."""
```

**Integration into main.py:**
- Add to lifespan: instantiate AxiomInspector
- Add to scheduler: job at 8:30 AM pre_market_sweep; every 15 min market-hours sweep; 4:30 PM post-market; Friday 5 PM weekly
- All escalations via `shared.sovereign_comms.report()`

### Tests: `tests/axiom/test_inspector.py` + `test_inspector_integration.py`
- 40+ unit test cases (4–5 per domain)
- 3 integration tests
- All must pass before deployment

---

## Dependencies

### Imports
```python
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, dict, list
import requests
import pytz

# Nexus shared
from shared.sovereign_comms import report
from database import get_audit_log_entries
```

### External
- All services already available at localhost:8001–8009
- Polygon, ORATS, Alpha Vantage, FRED already used by Axiom

### No New Dependencies
- Does not require new packages; uses existing Axiom dependencies

---

## Success Criteria

1. **Code Quality:**
   - All 40+ tests pass
   - No hardcoded thresholds (all from config.py)
   - Type hints on all functions
   - Docstrings on all public methods

2. **Operational:**
   - Pre-market sweep completes in < 30s
   - No false positives (escalations only when threshold actually exceeded)
   - De-duplication works (same issue not escalated > 1x per 2 hours)
   - All escalations reach SOVEREIGN via Telegram

3. **Integration:**
   - Inspector runs in parallel with pool curation (no blocking)
   - Scheduler fires on time (pre-market, market-hours, post-market)
   - Audit logs captured in axiom.db

4. **Ahmed Approval:**
   - Spec approved before build
   - Code reviewed by GENESIS after build
   - Live testing in staging before production

---

## Deployment

### Staging
1. Build inspector.py in nexus/axiom/
2. Wire into main.py lifespan
3. Run full test suite
4. Deploy to localhost:8001 (development)
5. Run 1 full market day in staging
6. Verify all reports reach SOVEREIGN via Telegram

### Production
1. Deploy to Railway (Axiom production service)
2. Monitor first 3 full market days
3. Adjust escalation thresholds if needed (false positives/negatives)
4. Lock configuration

---

## Amendment Log

| Date | Change | Approved By |
|------|--------|------------|
| 2026-05-16 | Created spec; awaiting approval | — |
| TBD | Approved; proceeding to build | Ahmed Sadek |
| TBD | Implementation complete + tested | GENESIS |

---

**Spec Ready for Review.**
