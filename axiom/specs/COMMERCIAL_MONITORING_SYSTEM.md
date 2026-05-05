# Nexus Commercial-Grade Monitoring System — Architecture Specification
**Design Authority:** Axiom  
**Date:** April 29, 2026  
**Target:** Nexus V1 (current) + V2 (real-time tick data)

---

## Table of Contents
1. [Architecture Overview](#1-architecture-overview)
2. [Synthetic Transaction Engine (Canary Factory)](#2-synthetic-transaction-engine)
3. [Multivariate Composite Health Score](#3-multivariate-composite-health-score)
4. [Telemetry Pipeline](#4-telemetry-pipeline)
5. [Statistical Process Control Engine](#5-statistical-process-control-engine)
6. [Self-Healing Orchestrator](#6-self-healing-orchestrator)
7. [Strategy-Level Circuit Breakers](#7-strategy-level-circuit-breakers)
8. [Latency Budget Tracking](#8-latency-budget-tracking)
9. [Health Dashboard](#9-health-dashboard)
10. [V2 Extensions (Real-Time Tick Data)](#10-v2-extensions)
11. [Implementation Phases](#11-implementation-phases)

---

## 1. Architecture Overview

```
                         ┌─────────────────────────────────────┐
                         │         NEXUS INTEGRITY LAYER        │
                         │       (Hosted in Axiom Service)       │
                         └─────────────────────────────────────┘
                                    │
         ┌──────────────────────────┼──────────────────────────┐
         │                          │                          │
  ┌──────▼──────┐          ┌───────▼───────┐          ┌───────▼───────┐
  │ Canary      │          │ Telemetry      │          │ SPC Engine    │
  │ Factory     │          │ Collector      │          │ (Statistical) │
  │ (Synth Tx)  │          │ (Event Bus)    │          │               │
  └──────┬──────┘          └───────┬───────┘          └───────┬───────┘
         │                         │                          │
         └──────────────┬──────────┴──────────┬───────────────┘
                        │                     │
                 ┌──────▼──────┐       ┌──────▼──────┐
                 │ Composite   │       │ Self-Healing │
                 │ Health      │       │ Orchestrator │
                 │ Aggregator  │       │              │
                 └──────┬──────┘       └──────┬──────┘
                        │                     │
                        └──────────┬──────────┘
                                   │
                          ┌────────▼────────┐
                          │  Health State   │
                          │  (RED/AMBER/    │
                          │   GREEN/STOP)   │
                          └─────────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    │              │              │
              ┌─────▼────┐  ┌─────▼────┐  ┌─────▼────┐
              │ Alert    │  │ Dashboard│  │ Size Cap │
              │ Bus      │  │ (UI)     │  │ (OMNI)   │
              └──────────┘  └──────────┘  └──────────┘
```

### Design Principles

1. **Synthetic-first:** The canary is the source of truth. Real trades validate the canary; the canary validates the system.
2. **No single source of truth for health:** Every metric is cross-validated against at least one independent measurement.
3. **Fail-open vs fail-closed per context:** Strategy-level circuit breakers fail-closed but individual service health checks fail-open (the system can trade while degraded, with reduced size).
4. **Self-healing, not just alerting:** Every failure must have an automated repair path before it reaches a human.
5. **Latency budgets, not just uptime:** Each pipeline stage has a max acceptable latency; violations auto-escalate.

---

## 2. Synthetic Transaction Engine (Canary Factory)

### 2.1 Concept

Fire test payloads through every executable pipeline path during market hours, verify each stage completes within tolerance. The canary is the single best predictor of whether real trades will succeed.

### 2.2 Canary Types

| Type | Pipeline Path | Frequency | What It Validates |
|---|---|---|---|
| **Alpha Single-Leg** | Agent webhook → Alpha /submit → Concordance → OMNI | Every 30 min | Full V1 equity options path |
| **Alpha Multi-Leg** | Same path, multi-leg payload | Every 60 min | Spread pricing + gate |
| **Prime Single-Leg** | Agent webhook → Prime /submit → Concordance → OMNI | Every 30 min | Full V1 futures path |
| **Axiom Assess** | POST /assess → risk scoring → verdict | Every 15 min | Risk engine correctness |
| **Execution Dry-Run** | POST /execute → validate parse → no actual fill | Every 30 min | Execution endpoint code paths |
| **Axiom Limits** | GET /limits → validate all config fields | Every 5 min | Config drift detection |
| **Data Feed** | Check Polygon/ORATS last successful call | Every 60 min | External data freshness |

### 2.3 Canary Payload Structure

```json
{
  "ticker": "SPY",
  "score": 70.0,
  "direction": "BULLISH",
  "agent": "Cipher",
  "system": "alpha",
  "strategy": "short_put",
  "canary_id": "cny_20260429_0930_001",
  "canary": true,
  "expected_result": "GO",
  "sla_seconds": 30,
  "stages": {
    "webhook_received": null,
    "concordance_scored": null,
    "omni_dispatched": null,
    "execution_accepted": null
  }
}
```

### 2.4 Verification Protocol

```
Canary Factory sends pick ──► Agent webhook (localhost:9001/receive-pool)
       │                              │
       │                       Alpha/Prime /submit
       │                              │
       │                       Concordance processing
       │                              │
       │                       OMNI dispatch check
       │                              │
       │                       Execution endpoint test
       │                              │
       ▼                              ▼
  Canary Factory polls all         Each stage records
  stages for < 30 second           latency + status
  SLA                              in staging table
       │
       ▼
  ALL stages = success? ──► GREEN canary
  ANY stage = fail?     ──► RED   canary → auto-diagnose → alert
  ANY stage > SLA?      ──► AMBER canary → report latency drift
```

### 2.5 Stage Verification

| Stage | Verification Method | Failure Mode Detected |
|---|---|---|
| Webhook received | Agent returns 200 + pick_id | Agent unreachable, routing failure |
| Concordance scored | Concordance counter increments | Concordance stall |
| OMNI dispatched | OMNI dispatch counter increments | OMNI death, pipeline drop |
| Execution accepted | /execute returns 200 with test=true | Code crash on input, schema mismatch |
| Axiom /assess | Returns GO/FLAG/SKIP within 5s | Risk engine crash, data source down |

### 2.6 Canary Lifecycle

```
CREATE ──► IN_FLIGHT ──► VERIFIED (GREEN)
                     └──► FAILED   (RED)
                          └──► ROOT_CAUSE_DIAGNOSED
                               └──► AUTO_FIXED or ESCALATED
```

---

## 3. Multivariate Composite Health Score

### 3.1 Scoring Model

```
Composite = Σ(weight_i × score_i) / Σ(weight_i)

Score ranges: 0-100
GREEN:   90-100  (full trading capacity)
AMBER:    60-89  (cap size to 50%, increase monitoring)
RED:      30-59  (halt new entries, manage existing positions only)
BLACK:     0-29  (halt everything, emergency)
```

### 3.2 Component Scores

| Component | Weight | Method | What It Measures |
|---|---|---|---|
| **Service Availability** | 20% | All 6 services respond /health within 10s | Process-level uptime |
| **Canary Success Rate** | 25% | Last 5 canary results (pass/fail/SLA) | Pipeline functional health |
| **Config Correctness** | 15% | /limits returns ALL expected fields with valid values | Config drift, missing endpoints |
| **Error Rate** | 15% | HTTP 4xx/5xx across all services in last 15 min | Active system degradation |
| **Pipeline Throughput** | 10% | Submissions vs verdicts vs executions ratio | Cascading blockage detection |
| **Data Freshness** | 10% | Age of last VIX update, last scan time, last agent response | Stale data = bad decisions |
| **Session Activity** | 5% | Any agent session active in last 5 min? | Unattended system detection |

### 3.3 Scoring Calculation Example

```python
def compute_composite_health(state):
    components = []
    
    # 1. Service Availability (20%)
    alive = sum(s.since for s in state if s.alive)
    total = len(state.monitored_services)
    availability = min(100, (alive / total) * 100)  # 90% alive = 90
    components.append(("availability", availability, 0.20))
    
    # 2. Canary Success Rate (25%)
    recent_canaries = state.canary_log[-5:]
    if not recent_canaries:
        canary_score = 0
    else:
        passed = sum(1 for c in recent_canaries if c.status == "PASS")
        all_sla = sum(1 for c in recent_canaries if c.latency <= c.sla)
        canary_score = ((passed / len(recent_canaries)) * 0.7 + 
                        (all_sla / len(recent_canaries)) * 0.3) * 100
    components.append(("canary", canary_score, 0.25))
    
    # 3. Config Correctness (15%)
    # Get expected config from code, compare to /limits response
    config_expected = {"max_positions": 3, "vix_pause_threshold": 35.0, ...}
    config_actual = fetch_limits()
    matching = sum(1 for k, v in config_expected.items() 
                   if k in config_actual and config_actual[k] == v)
    config_score = (matching / len(config_expected)) * 100
    components.append(("config", config_score, 0.15))
    
    # ... remaining components
    
    composite = sum(weight * score for _, score, weight in components)
    return composite, components
```

### 3.4 Scoring Output

```json
{
  "composite_health": 87.3,
  "grade": "AMBER",
  "timestamp": "2026-04-29T13:45:00Z",
  "components": [
    {"name": "availability", "score": 100, "weight": 0.20, "status": "GREEN"},
    {"name": "canary", "score": 80, "weight": 0.25, "status": "AMBER"},
    {"name": "config", "score": 100, "weight": 0.15, "status": "GREEN"},
    {"name": "error_rate", "score": 95, "weight": 0.15, "status": "GREEN"},
    {"name": "throughput", "score": 70, "weight": 0.10, "status": "AMBER"},
    {"name": "freshness", "score": 90, "weight": 0.10, "status": "GREEN"},
    {"name": "session", "score": 60, "weight": 0.05, "status": "AMBER"}
  ],
  "verdict": "Cap new entries to 50% of normal size.",
  "action": "REDUCE_SIZE",
  "escalation": false,
  "affected_strategies": ["alpha_multi_leg"],
  "canary_trace": {
    "last_5_results": ["PASS", "PASS", "LATENCY_BREACH", "PASS", "PASS"],
    "last_failure": {"id": "cny_20260429_1315_003", "reason": "concordance_stall_45s"}
  }
}
```

---

## 4. Telemetry Pipeline

### 4.1 Event Types

Every service emits structured events to a unified telemetry bus:

| Event Type | Source | Schema | Purpose |
|---|---|---|---|
| `submission_received` | Alpha/Prime | pick_id, agent, ticker, score, latency_ms | Track submission pipeline health |
| `concordance_event` | Alpha/Prime | pick_id, status, conflict_count, latency_ms | Detect concordance stalls |
| `dispatch_event` | Alpha/Prime | pick_id, target, latency_ms | Track OMNI dispatch timing |
| `execution_event` | OMNI/Executor | order_id, ticker, side, qty, price, status | Track execution health |
| `canary_event` | Axiom | canary_id, type, stage, status, latency_ms | Canary verification log |
| `monitor_check` | Axiom | check_name, status, latency_ms, details | Health check results |
| `error_event` | Any | service, endpoint, error_type, traceback, count | Error aggregation |
| `config_change` | Railway/Deploy | service, old_version, new_version, trigger | Config drift detection |
| `circuit_breaker` | Alpha/Prime | strategy, reason, state, params | CB state transitions |

### 4.2 Event Schema

```json
{
  "event_id": "evt_20260429_134500_0001",
  "event_type": "submission_received",
  "source": "alpha.main:321",
  "timestamp": "2026-04-29T13:45:00.123456Z",
  "session_id": "sess_cipher_20260429",
  "correlation_id": "pick_spy_20260429_1345_001",
  "canary": false,
  "data": {
    "ticker": "SPY",
    "agent": "Cipher",
    "score": 70.0,
    "pick_id": "ba288b03",
    "latency_ms": 234
  },
  "severity": "info"
}
```

### 4.3 Telemetry Storage

```
Chronicle DB (existing) ──►e2e_test_results table (for incidents)
                         ──►monitoring_metrics table (for time-series)
                         ──►canary_log table (for canary results)
                         ──►telemetry_events table (for raw events, TTL=7d)

New tables needed:
  - monitoring_metrics:  metric_name, window_id, score, detail_json, ran_at
  - canary_log:          canary_id, type, stage_statuses, total_latency_ms, verdict, ran_at
  - telemetry_events:    event_id, event_type, source, correlation_id, data_json, severity, ran_at
  - error_aggregation:   error_type, source, count_1h, count_24h, first_seen, last_seen
  - composite_health:    score, grade, component_scores_json, ran_at
```

### 4.4 Retention Policy

| Data Type | Retention | Purpose |
|---|---|---|
| Raw telemetry events | 7 days | Real-time debugging |
| Canary logs | 90 days | Trend analysis |
| Composite health snapshots | 1 year | Audit trail, compliance |
| Error aggregations | 1 year | Pattern detection |
| Incidents | Permanent | Root cause database |

---

## 5. Statistical Process Control Engine

### 5.1 Monitored Distributions

| Metric | Collection Method | Statistical Test | Threshold |
|---|---|---|---|
| GO verdict rate | Count GO vs total submissions per window | Z-score vs trailing 20-day mean | |z| > 3 = alert |
| Submission volume | Count per 30-min bucket | Z-score vs trailing 5-day mean | |z| > 5 = injection/runaway |
| Pick score distribution | All scores per window | K-S test vs trailing 10-day ECDF | p < 0.01 = shift |
| Pipeline latency (P50) | All latency measurements per hour | Exponentially weighted moving average | > 3x baseline = congestion |
| Pipeline latency (P99) | Same | Same | > 2x baseline = critical |
| Error rate by endpoint | Count per endpoint per hour | EWMA with control limits | Upper 3σ limit breach |
| Canary SLA miss rate | Count per canary type per day | CUSUM | Cumulative deviation |
| Agent response time | Time from webhook → /submit response | Moving average + std dev | > 3σ above mean |

### 5.2 How SPC Caught Past Failures (Post-Hoc)

| Incident | SPC Metric That Would Have Caught It | Lead Time Before Impact |
|---|---|---|
| INV-14 gate crash | Error rate on /execute endpoint | 0 (immediate — canary triggers /execute) |
| Prime 57% GO rate | GO verdict Z-score > 10σ from historical ~10% | Days before Ahmed was informed |
| AGENT_SILENCE | Agent response time → ∞, submission volume → 0 | Minutes |
| Dry cycles | Agent response volume → 0 on 3+ consecutive cycles | Within 2 cycles (~10 min) |
| Config drift | Composite health config component → 0 | Immediate |

### 5.3 Implementation Strategy

```python
class StatisticalProcessControl:
    def __init__(self, chronicle_db):
        self.db = chronicle_db
        self.trailing_windows = {
            "go_verdict_rate": 20,  # days
            "submission_volume": 5,  # days  
            "pipeline_latency": 500,  # observations
            "error_rate": 100,  # observations
        }
        self.cusum = CUSUMDetector(target_drift=1.0)
    
    def check_go_verdict_rate(self, rate: float) -> Alert:
        """Z-score against trailing 20-day mean."""
        mean, std = self._trailing_stats("go_verdict_rate", days=20)
        z = (rate - mean) / max(std, 0.001)
        if abs(z) > 3:
            direction = "HIGH" if rate > mean else "LOW"
            return Alert(f"GO verdict rate {rate:.1%} is {direction} ({z:.1f}σ)", severity="WARN")
        return None
    
    def check_latency_budget(self, service: str, latency_ms: float, budget_ms: float) -> Alert:
        """Check P99 against budget. Also check EWMA for trend."""
        if latency_ms > budget_ms:
            return Alert(f"{service} P99 {latency_ms}ms exceeds budget {budget_ms}ms", severity="CRITICAL")
        # Exponential weighted moving average for trend detection
        baseline = self._ewma(f"{service}_latency", latency_ms, alpha=0.05)
        if latency_ms > 2 * baseline:
            return Alert(f"{service} latency {latency_ms}ms is 2x baseline {baseline:.0f}ms", severity="WARN")
        return None
```

---

## 6. Self-Healing Orchestrator

### 6.1 Failure → Repair Mapping

| Failure Pattern | Diagnosis Method | Automated Repair | Escalation Time |
|---|---|---|---|
| Service unreachable | /health timeout | Railway redeploy via API | 30 seconds |
| Config drift (wrong values) | /limits mismatch | Re-deploy correct code | 1 minute |
| Code crash (500 on endpoint) | Error rate spike on endpoint | Railway rebuild + restart | 2 minutes |
| Agent not responding | Canary pick → no webhook ack | Agent session restart via OpenClaw | 3 minutes |
| Pipeline stall (submissions not flowing) | Concordance age > threshold | Trigger manual flush | 1 minute |
| Runaway verdicts (too many GO) | SPC GO rate alert | Reduce auto-approval to 0 | 2 minutes |
| Data source stale | Last Polygon timestamp > 15 min | Cycle data source connection | 2 minutes |
| Session unattended | No agent heartbeats > 5 min | Wake session via cron job | 2 minutes |

### 6.2 Repair Action Library

```python
class SelfHealingOrchestrator:
    def __init__(self):
        self.actions = {
            "railway_redeploy": self._railway_redeploy,
            "railway_rebuild": self._railway_rebuild,
            "session_wake": self._session_wake,
            "circuit_reset": self._circuit_reset,
            "flush_pipeline": self._flush_pipeline,
            "reduce_sizing": self._reduce_sizing,
            "agent_restart": self._agent_restart,
        }
    
    def diagnose_and_repair(self, failure: FailureEvent) -> RepairResult:
        """Map failure to repair action, execute, verify."""
        repair = self._map_failure_to_repair(failure)
        if repair is None:
            return RepairResult(failure.id, "UNKNOWN", False, 
                               "No automated repair path", escalated=True)
        
        # Execute repair
        result = self.actions[repair.action](**repair.params)
        
        # Verify repair
        verify_result = self._verify_repair(failure, timeout=30)
        
        # Log to CHRONICLE
        self._log_intervention(failure, repair, verify_result)
        
        return result
```

### 6.3 Escalation Ladder

```
Detection ──► Auto-diagnose (5s)
    │
    ├── Repair path exists ──► Execute repair ──► Verify (30s)
    │                                  │
    │                            Success ──► Log and close
    │                            Fail    ──► Escalate (Level 1)
    │
    └── No repair path ──► Escalate immediately (Level 1)

Level 1: Alert Nexus Health Group + Axiom auto-repair attempt
Level 2: Alert Ahmed DM (if Level 1 > 2 min no progress)
Level 3: Complete system halt (if Level 2 > 5 min no response)
```

---

## 7. Strategy-Level Circuit Breakers

### 7.1 Per-Strategy Monitoring

Current circuit breakers are global (Alpha buffer, Prime buffer). Commercial-grade requires per-strategy granularity.

| Breaker Level | Scope | Action | Reset |
|---|---|---|---|
| **Strategy** | Specific pick type (e.g., "NVDA short put") | Block that strategy only | Manual after root cause fixed |
| **Sector** | All picks in a sector (e.g., "Tech") | Block sector entries | Auto after 1 hour if metrics improve |
| **Agent** | One agent's picks (e.g., "Cipher") | Block that agent's submissions | Auto after next canary passes |
| **System** | All new entries | Full halt | Ahmed only |

### 7.2 Trigger Conditions

| Breaker | Trigger | Auto-Reset |
|---|---|---|
| Strategy | >3 consecutive losses on same strategy | No — needs root cause |
| Strategy | SPC detects score distribution shift | No |
| Sector | >30% exposure in single sector with VIX > 25 | Yes — 1 hour later |
| Agent | Canary failure on agent's path (2 consecutive) | Yes — next canary passes |
| Agent | SPC detects abnormal verdict pattern | No |
| System | Composite health < 30 (BLACK) | Ahmed only |
| System | Inverse VIX pause (VIX > 35) | VIX < 30 |
| System | Sector circuit triggered on 3+ sectors simultaneously | Ahmed only |

---

## 8. Latency Budget Tracking

### 8.1 Stage Budgets

| Pipeline Stage | Budget (P99) | Critical (P99.9) | Tracking Method |
|---|---|---|---|
| Agent → Webhook → /submit | 5 seconds | 30 seconds | Timestamp on pick payload |
| /submit → Concordance score | 2 seconds | 10 seconds | Concordance processing log |
| Concordance → OMNI dispatch | 5 seconds | 15 seconds | Dispatch queue monitor |
| OMNI → Execution | 3 seconds | 10 seconds | Execute endpoint response time |
| Axiom /assess | 3 seconds | 5 seconds | Endpoint latency |
| Axiom /limits | 1 second | 2 seconds | Endpoint latency |
| Canary trade (total) | 30 seconds | 60 seconds | Canary factory timer |
| Health monitor cycle | 5 minutes | 6 minutes | Scheduler drift |
| Data feed (Polygon refresh) | 15 minutes | 30 minutes | Latest timestamp check |

### 8.2 Budget Violation Handling

```
P99 budget breach ──► AMBER (log, continue, report to composite health)
P99.9 breach     ──► RED   (auto-diagnose, escalate, consider halting affected pipeline)
3× consecutive    ──► CRITICAL (halt affected pipeline, full diagnostic)
```

---

## 9. Health Dashboard

### 9.1 Information Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  NEXUS HEALTH — COMPOSITE: 87.3 (AMBER)  Wed Apr 29 13:45 ET  │
│  Last canary: PASS (2.3s)  Last failure: 1h ago (SPC verdict) │
├─────────────────────────────────────────────────────────────────┤
│  SERVICES │ CANARY │ CONFIG │ ERRORS │ THROUGHPUT │ FRESHNESS │
│  ┌──────┐ │ ┌────┐ │ ┌────┐ │ ┌────┐ │ ┌────────┐ │ ┌────────┐ │
│  │GREEN√│ │ │AMBR│ │ │GRN√│ │ │GRN√│ │ │─AMBR─ │ │ │  GRN√ │ │
│  │100/100│ │ │ 80 │ │ │100 │ │ │ 95 │ │ │   70   │ │ │   90   │ │
│  └──────┘ │ └────┘ │ └────┘ │ └────┘ │ └────────┘ │ └────────┘ │
├───────────┴────────┴────────┴────────┴────────────┴────────────┤
│ PIPELINE FLOW                                                    │
│                                                                  │
│  Agent ──┬──> Alpha ──> Concordance ──> OMNI ──> Execute        │
│           │             ✓ 2/10 pending      ✓ 3/3 dispatched    │
│           └──> Prime ──> Concordance ──> OMNI ──> Execute        │
│                          ✓ 0/5 pending      ✓ 1/1 dispatched    │
│                                                                  │
│  Canary: cny_20260429_1345_001 ──> Alpha pipeline               │
│          Stage: webhook(✓ 0.2s) submit(✓ 0.3s) conc(✓ 1.1s)    │
│                 dispatch(✓ 0.8s) exec(✓ 0.1s) TOTAL(✓ 2.5s)    │
├─────────────────────────────────────────────────────────────────┤
│ RECENT INCIDENTS                                                 │
│  09:34 — INV-14 gate crash ──> Auto-fixed via Railway redeploy  │
│  07:10 — E2E diagnostic: 3 failures ──> 2 auto-fixed, 1 pending│
│  ...                                                             │
├─────────────────────────────────────────────────────────────────┤
│ ACTIVE CIRCUIT BREAKERS                                          │
│  None                                                           │
├─────────────────────────────────────────────────────────────────┤
│ PENDING ESCALATIONS                                              │
│  None                                                           │
└─────────────────────────────────────────────────────────────────┘
```

### 9.2 Dashboard Data Flow

```
1. Composite health engine runs every 60 seconds
2. Pushes state to JSON endpoint: /composite-health
3. Dashboard polls /composite-health or subscribes via WebSocket
4. Dashboard auto-refreshes: 10s (real-time), 60s (stale fallback)
5. Same data feeds into OMNI's size calculator
```

### 9.3 Delivery Channels

| Channel | Content | Frequency | Audience |
|---|---|---|---|
| NEXUS HEALTH group chat | Composite score + any active RED/AMBER | Every status change + periodic (15 min) | All agents + Ahmed |
| Ahmed DM | Critical failures (RED/BLACK, circuit trips) | Immediate | Ahmed |
| Axiom internal | Full dashboard JSON | Every 60 seconds | Axiom self-healing |
| OMNI input | Score + sizing cap | Every 60 seconds | OMNI strategy sizing |
| /composite-health endpoint | Full state + all metrics | Polled | Any caller |

---

## 10. V2 Extensions (Real-Time Tick Data)

### 10.1 New Failure Modes in V2

| V2 Feature | New Failure Mode | Monitoring Requirement |
|---|---|---|
| Real-time tick data | Data feed stall (last tick > 100ms ago) | Tick freshness monitor |
| High-frequency signals | Signal quality degradation | Per-tick statistical validation |
| Multiple data sources | Data source divergence (same ticker, different prices) | Cross-source reconciliation |
| Automated execution | Execution feedback loop | Fill rate monitoring, slippage tracking |
| 1-second latency requirements | Micro-burst congestion | Sub-second latency budgets per stage |

### 10.2 Additional V2 Monitors

```
1. Tick Freshness Monitor
   - Expected: new tick every 100ms during market hours
   - Alert: no new tick in 1 second
   - Critical: no new tick in 5 seconds
   - Auto-fix: failover to backup data source

2. Cross-Source Reconciliation
   - Compare Polygon vs IQFeed vs direct exchange for SPY
   - Max acceptable divergence: $0.01
   - Alert: divergence > $0.05 for > 500ms
   - Critical: divergence > $0.10 instantaneous
   - Auto-fix: discard source with largest deviation

3. Alpha Degradation Monitor
   - Track time from tick arrival → signal output
   - P50 < 10ms, P99 < 50ms, P99.9 < 200ms
   - Budget breach = downgrade strategy to skip-time mode

4. Strategy Coherence Check
   - Post-trade: does actual fill match signal expectations?
   - Expected vs actual: delta in price, quantity, timing
   - Deviation > 3σ from historical = flag strategy

5. Micro-Burst Monitor
   - Burst = > 100 signals in 1-second window
   - Auto-throttle to configured max signal rate
   - Log burst pattern for capacity planning

6. Stale Signal Detector
   - Signal age > 500ms = discard
   - Track discard rate per strategy
   - > 1% discard rate = pipeline latency issue
```

### 10.3 V2 Composite Health Modifications

```json
{
  "V1 components": { ... },
  "V2 additions": {
    "tick_freshness": {"weight": 0.15, "V1_weight_reduced": "throughput"},
    "cross_source_divergence": {"weight": 0.10, "V1_weight_reduced": "availability"},
    "signal_latency": {"weight": 0.10, "V1_weight_reduced": "freshness"},
    "fill_quality": {"weight": 0.05, "V1_weight_reduced": "session"}
  }
}
```

V2 composite = V1 composite adjusted for V2-specific components.

---

## 11. Implementation Phases

### Phase 1 — Foundation (1-3 days)
**Build:** Canary Factory, Composite Health Endpoint, CHRONICLE tables

| Deliverable | Owner | Dependencies |
|---|---|---|
| `canary_factory.py` — synthetic transaction engine | Axiom | None |
| `/composite-health` endpoint on Axiom | Axiom | Canary Factory |
| `monitoring_metrics` + `canary_log` tables in CHRONICLE | Axiom | Chronicle DB access |
| Canary integrated into health_monitor scheduler (every 30 min) | Axiom | Canary Factory |

### Phase 2 — SPC Engine (3-7 days)
**Build:** Statistical monitors, error aggregation, anomaly detection

| Deliverable | Owner | Dependencies |
|---|---|---|
| `spc_engine.py` — Z-score, CUSUM, EWMA detectors | Axiom | CHRONICLE metrics table |
| Error aggregation (5-min and 1-hour windows) | Axiom | Telemetry events table |
| GO verdict distribution tracker | Axiom | Prime status endpoint |
| Pipeline latency budget tracker | Axiom | All pipeline services |

### Phase 3 — Self-Healing (7-14 days)
**Build:** Repair orchestration, escalation ladder, auto-fix paths

| Deliverable | Owner | Dependencies |
|---|---|---|
| `self_healing.py` — failure → repair mapping | Axiom + Cipher | Phase 1 + 2 |
| Railway redeploy automation | Axiom | Railway API token |
| Session wake automation | Axiom | OpenClaw cron API |
| Circuit breaker API for per-strategy halts | Axiom + Cipher | Existing CB endpoints |
| Escalation ladder (levels 1-3) | Axiom | All above |

### Phase 4 — Dashboard & Sizing (14-21 days)
**Build:** Health dashboard UI, OMNI sizing integration, CHRONICLE log reader

| Deliverable | Owner | Dependencies |
|---|---|---|
| Health dashboard (web page) | Axiom | All phases |
| OMNI sizing integration (composite → trade size multiplier) | Axiom + OMNI | Phase 1 |
| V2 extensions (tick monitors, cross-source) | Axiom | V2 deployment |

---

## Appendix A: All Existing Monitoring Systems — Gaps vs This Design

| Existing System | What It Misses | How This Design Covers It |
|---|---|---|
| Railway /health | Pipeline functionality, code paths | Canary trade runs every 30 min |
| health_monitor.py (Axiom) | Can't detect crashes in /execute endpoint | Canary trade calls /execute endpoint |
| Alert Monitor (launchd) | Silent failures with 200 OK + wrong data | Canary validates output correctness |
| E2E Diagnostic (startup) | Only runs at startup, not during hours | Monitors run continuously |
| Layer 11 (Alpha) | Only Alpha-side, no execution or Prime | Cross-service canary + composite score |
| Circuit Breakers (Alpha/Prime) | Only post-trade, not pre-trade validation | Canary tests pre-trade paths |
| HEARTBEAT.md (manual) | Human delay, only when session active | Automated every 5-30 minutes |

## Appendix B: Failure Detection Coverage Matrix

```
Legend: ● = First-line detection   ○ = Second-line   × = No coverage

Failure Mode                        Canary  SPC  CompHealth  AlertMon  L11  Railway
───────────────────────────────────────────────────────────────────────────
Code crash on endpoint               ●       ○     ○           ●         ×     ×
Pipeline stall (submission → exec)   ●       ●     ●           ○         ×     ×
Config drift (wrong values)          ○       ×     ●           ×         ×     ×
Agent not producing output           ○       ●     ●           ×         ●     ×
Runaway verdicts                     ×       ●     ○           ×         ○     ×
Data source stale                    ○       ×     ●           ×         ×     ×
Session unattended                   ●       ×     ○           ×         ○     ×
Error rate spike                     ×       ●     ●           ●         ×     ×
Latency degradation                  ●       ●     ●           ×         ○     ×
Strategy drift (changed behavior)    ×       ●     ×           ×         ×     ×
External dependency failure          ●       ×     ○           ×         ×     ×
Unattended system (no session)       ●       ×     ○           ×         ×     ×

● = First-line (detects within seconds-minutes)
○ = Second-line (detects within minutes-hour)
× = Cannot detect this failure mode
```

---

*This system, when fully implemented, provides:*
- **Immediate detection** (seconds): code crashes, pipeline stalls, error spikes
- **Early detection** (minutes): latency degradation, agent silence, config drift
- **Trend detection** (hours-days): strategy drift, runaway verdicts, data quality degradation
- **Self-healing** for 90%+ of known failure patterns
- **Ahmed only hears about** genuinely novel failures or multi-system cascades
