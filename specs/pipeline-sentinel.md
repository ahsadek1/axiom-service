# SPEC: pipeline-sentinel — Nexus Orchestration Intelligence Layer

**Version:** 1.0  
**Author:** GENESIS  
**Date:** 2026-04-13  
**Status:** APPROVED — Ahmed Sadek (2026-04-13)  
**Port:** 8008  
**LaunchD label:** ai.nexus.pipeline-sentinel

---

## Purpose

A standalone microservice that gives the Nexus trading system full visibility into its own operational integrity. Tracks every pick end-to-end through the pipeline, scores system health in real time, classifies failures with recovery protocols, and injects system context into every agent decision. Closes the exact gap that caused the V1 silent failure: HTTP 200 from an agent ≠ pick delivered and processed.

---

## Architecture Overview

Four components, each with a single responsibility:

1. **Pipeline Trace Ledger** — immutable record of every pick at every hop
2. **System Health Score** — continuous 0–100 score broadcast to all services
3. **Failure Mode Classifier** — classifies stalls/anomalies + prescribes recovery
4. **Agent Context Injector** — appends system health block to every Axiom pool push

All four run inside one service. One database (`pipeline_sentinel.db`). One launchd process.

---

## Inputs

### Inbound Webhooks (Pipeline Sentinel receives)

| Endpoint | Auth Header | Called By | Purpose |
|---|---|---|---|
| `POST /trace` | `X-Nexus-Secret` | All services | Record a pipeline hop for a pick |
| `GET /health` | None | Guardian Angel | Service liveness check |
| `GET /system-health` | `X-Nexus-Secret` | Any service | Get current system health score + context block |
| `GET /pipeline/{trace_id}` | `X-Nexus-Secret` | OMNI, Execution | Get full trace for a specific pick |
| `GET /stalls` | `X-Nexus-Secret` | Guardian Angel | Get all currently stalled picks |
| `POST /report` | `X-Nexus-Secret` | Guardian Angel | Receive anomaly report from GA v3 |

### Trace Payload (POST /trace)
```json
{
  "trace_id": "uuid4",
  "hop": "axiom_push | agent_received | buffer_accepted | omni_started | omni_completed | execution_received | alpaca_submitted | alpaca_confirmed",
  "service": "axiom | cipher | atlas | sage | alpha-buffer | prime-buffer | omni | alpha-execution | prime-execution",
  "ticker": "AAPL",
  "pathway": "alpha | prime",
  "status": "ok | error | timeout",
  "metadata": {}
}
```

### Outbound Calls (Pipeline Sentinel makes)
- Telegram alert to Ahmed (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_AHMED_CHAT_ID`)
- None — no calls to other Nexus services (sentinel is read-only to the pipeline)

---

## Outputs

### GET /system-health response
```json
{
  "health_score": 91,
  "score_components": {
    "pipeline_completion_rate": 0.97,
    "inter_service_latency_p95_ms": 340,
    "omni_brain_latency_p95_ms": 1800,
    "oracle_cache_freshness": 1.0,
    "active_anomaly_count": 0,
    "stalled_picks_count": 0
  },
  "status": "NOMINAL",
  "recommended_size_multiplier": 1.0,
  "active_failures": [],
  "context_block": {
    "health_score": 91,
    "executor_latency_p95_ms": 340,
    "pipeline_completion_rate": 0.97,
    "active_anomalies": [],
    "recommended_size_multiplier": 1.0
  },
  "submissions_open": true,
  "computed_at": "2026-04-13T18:00:00Z"
}
```

### Health Score Bands

| Score | Status | Size Multiplier | Submissions | Ahmed Alert |
|---|---|---|---|---|
| 85–100 | NOMINAL | 1.0 | Open | No |
| 70–84 | DEGRADED | 0.95 | Open | No |
| 55–69 | IMPAIRED | 0.85 | Open | Yes |
| 30–54 | CRITICAL | 0.0 | **HALTED** | Yes — immediate |
| 0–29 | EMERGENCY | 0.0 | **HALTED** | Yes — immediate |

---

## Pipeline Hop Sequence

### Alpha Pathway
```
axiom_push → cipher_received → alpha_buffer_accepted →
omni_started → omni_completed → alpha_execution_received →
alpaca_submitted → alpaca_confirmed
```
**Max allowed latency:** 8 minutes total  
**Stall detection:** Any hop missing for >5 minutes triggers PIPELINE_STALL

### Prime Pathway
```
axiom_push → sage_received / atlas_received → prime_buffer_accepted →
omni_started → omni_completed → prime_execution_received →
alpaca_submitted → alpaca_confirmed
```
**Max allowed latency:** 8 minutes total  
**Stall detection:** Same 5-minute rule

---

## Failure Mode Classifier

### Failure Classes + Recovery Protocols

| Class | Detection Condition | Auto-Recovery | Ahmed Alert |
|---|---|---|---|
| `EXECUTOR_SLOW` | Alpaca API hop latency P95 > 3000ms | Reduce `recommended_size_multiplier` to 0.5 | Yes |
| `DATA_STALE` | ORACLE hop missing or >15min gap in trace | Flag in context block; agents suppress ORACLE-dependent signals | Yes |
| `BRAIN_TIMEOUT` | `omni_started` present, `omni_completed` absent >6min | Log as OMNI timeout; next pick proceeds with 3/4 brains minimum | Yes |
| `NETWORK_DEGRADED` | P99 inter-hop latency >2000ms across 3+ consecutive picks | Add `network_degraded: true` to context block | No — info only |
| `AGENT_SILENT` | Agent service has 0 completed hops in current 15-min window | Trigger Axiom to re-push pool to that agent; alert after 2nd failure | Yes |
| `PIPELINE_STALL` | Any pick with no hop update for >5 minutes, not yet at `alpaca_confirmed` | Auto-retry signal sent to responsible service once; halt + alert after 2nd | Yes — immediate |
| `COMPLETION_RATE_LOW` | <80% of picks reach `alpaca_confirmed` in trailing 10 picks | Reduce size multiplier to 0.85, alert Ahmed | Yes |

---

## Health Score Computation

Score starts at 100. Deductions applied per component:

| Component | Max Deduction | Condition |
|---|---|---|
| Pipeline completion rate | −30 | Linear: 100%→0pts at 60% completion rate |
| Inter-service P95 latency | −20 | Linear: 0pts at 500ms, −20pts at 3000ms |
| OMNI brain P95 latency | −15 | Linear: 0pts at 2000ms, −15pts at 8000ms |
| Active stalled picks | −15 | −5 per stalled pick, max −15 |
| Active failure classes | −15 | −5 per active failure class, max −15 |
| Guardian Angel anomaly flags | −5 | −5 if GA reports any active anomaly |

Score recomputed every 30 seconds. Stored in DB with timestamp. 30-day history kept.

---

## Database Schema (pipeline_sentinel.db)

### traces table
```sql
CREATE TABLE traces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id        TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    pathway         TEXT NOT NULL CHECK(pathway IN ('alpha','prime')),
    hop             TEXT NOT NULL,
    service         TEXT NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('ok','error','timeout')),
    metadata        TEXT,  -- JSON
    ts              REAL NOT NULL,  -- unix timestamp
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_traces_trace_id ON traces(trace_id);
CREATE INDEX idx_traces_ts ON traces(ts);
```

### health_scores table
```sql
CREATE TABLE health_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    score           REAL NOT NULL,
    status          TEXT NOT NULL,
    size_multiplier REAL NOT NULL,
    submissions_open INTEGER NOT NULL,
    components      TEXT NOT NULL,  -- JSON
    active_failures TEXT NOT NULL,  -- JSON array
    ts              REAL NOT NULL,
    created_at      TEXT NOT NULL
);
```

### failure_events table
```sql
CREATE TABLE failure_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    failure_class   TEXT NOT NULL,
    trace_id        TEXT,
    detail          TEXT NOT NULL,
    auto_recovered  INTEGER NOT NULL DEFAULT 0,
    resolved        INTEGER NOT NULL DEFAULT 0,
    resolved_at     TEXT,
    ts              REAL NOT NULL,
    created_at      TEXT NOT NULL
);
```

---

## Contracts

**GUARANTEES:**
- Every `/trace` call acknowledged within 100ms (async write — never blocks the caller)
- Health score recomputed every 30 seconds minimum
- PIPELINE_STALL detected within 5 minutes + 30 seconds (one score cycle after stall window expires)
- Telegram alert delivered within 60 seconds of a CRITICAL/EMERGENCY score change
- Service startup completes within 10 seconds; `/health` returns 200 within 5 seconds of start

**DOES NOT GUARANTEE:**
- That all services actually call `/trace` (adoption is required from each service separately)
- Ordering of hops within the same second (timestamps are the source of truth)
- That auto-recovery will succeed — it signals intent, not outcome

**EXPLICITLY DOES NOT DO:**
- Does not modify picks, scores, or submission payloads directly
- Does not call Alpaca
- Does not call any AI brain
- Does not write to any other service's database

---

## Environment Variables

```
NEXUS_SECRET=<shared webhook secret>
TELEGRAM_BOT_TOKEN=<bot token>
TELEGRAM_AHMED_CHAT_ID=8573754783
DATA_DIR=/Users/ahmedsadek/nexus/data
PIPELINE_SENTINEL_DB_PATH=/Users/ahmedsadek/nexus/data/pipeline_sentinel.db
STALL_WINDOW_SECONDS=300        # 5 minutes
SCORE_RECOMPUTE_INTERVAL_S=30
HEALTH_HALT_THRESHOLD=55        # below this: submissions halted
HEALTH_IMPAIR_THRESHOLD=70      # below this: size penalty applied
LOG_LEVEL=INFO
```

---

## Service Integration Map

Which services need to call `/trace` (adoption phase after core build):

| Service | Hops to instrument | Priority |
|---|---|---|
| Axiom | `axiom_push` | 🔴 HIGH — this is the entry point |
| Cipher / Atlas / Sage | `agent_received` | 🔴 HIGH — confirms delivery to agent |
| Alpha Buffer / Prime Buffer | `buffer_accepted` | 🔴 HIGH — confirms concordance entry |
| OMNI | `omni_started`, `omni_completed` | 🔴 HIGH — largest failure surface |
| Alpha Execution / Prime Execution | `execution_received`, `alpaca_submitted`, `alpaca_confirmed` | 🔴 HIGH — confirms trade |

---

## Failure Modes (the service itself)

| Failure | Behavior |
|---|---|
| DB write fails on `/trace` | Log error, return 200 anyway — never block the pipeline |
| DB read fails on `/system-health` | Return last cached score with `stale: true` flag |
| Telegram alert fails | Retry 3x with exponential backoff; log failure; do not crash |
| Service crashes | Guardian Angel v3 detects on next 60s health poll; launchd auto-restarts |
| DB corruption (same as GA incident) | On startup: run `PRAGMA integrity_check`; if fails, delete and recreate fresh |

---

## Test Cases (minimum — Claude Code must implement all)

1. **Happy path** — Submit 8-hop trace for one pick, confirm `/pipeline/{trace_id}` returns all hops in order
2. **Stall detection** — Submit `axiom_push` hop, wait (mock) 5min+30s, confirm PIPELINE_STALL failure event created and Telegram called
3. **Score computation** — Submit 10 picks with 6 completing, confirm health score reflects ~60% completion rate with correct deduction
4. **Health halt** — Force score below 55, confirm `/system-health` returns `submissions_open: false`
5. **Score recovery** — Force score below 55, then submit successful picks until score rises above 55, confirm `submissions_open` flips back to true
6. **EXECUTOR_SLOW classification** — Submit traces with Alpaca hop latency >3000ms on 3 consecutive picks, confirm `EXECUTOR_SLOW` failure class created
7. **AGENT_SILENT detection** — Submit no agent hops for 15 minutes for one agent, confirm `AGENT_SILENT` event and alert
8. **DB startup integrity check** — Corrupt the DB file, restart service, confirm it detects and recreates
9. **Concurrent trace writes** — Fire 20 simultaneous `/trace` calls, confirm all 20 recorded correctly
10. **Context block format** — Confirm `/system-health` response includes valid `context_block` matching the exact schema Axiom pool push expects
11. **Telegram alert dedup** — Trigger same failure class twice within 5 minutes, confirm only 1 Telegram message sent (rate limiting)
12. **Score history** — Confirm 30-day retention: records older than 30 days pruned, recent records preserved

---

## Service Adoption — What Changes in Existing Services

After `pipeline-sentinel` is built and running, each service needs a one-line client call added. GENESIS will instrument each service in a follow-up build using a shared `pipeline_client.py` in `/nexus/shared/`:

```python
# Fire-and-forget — never blocks the caller
from shared.pipeline_client import trace_hop
trace_hop(trace_id=pick["trace_id"], hop="buffer_accepted", service="alpha-buffer", ticker=pick["ticker"], pathway="alpha")
```

The trace_id originates at Axiom and travels in every payload as a passthrough field.

---

## File Structure

```
/Users/ahmedsadek/nexus/pipeline-sentinel/
├── main.py                  # FastAPI app, all endpoints
├── scorer.py                # Health score computation engine
├── classifier.py            # Failure mode detection + classification
├── notifier.py              # Telegram alert with dedup/rate-limiting
├── database.py              # SQLite schema init + all DB operations
├── models.py                # Pydantic request/response models
├── requirements.txt
├── .env                     # env vars (gitignored)
├── tests/
│   └── test_pipeline_sentinel.py   # All 12 test cases
└── MANDATE.md
```

```
/Users/ahmedsadek/nexus/shared/
└── pipeline_client.py       # Fire-and-forget trace helper (used by all services)
```

---

## Definition of Done

- [ ] All 12 tests pass
- [ ] Service starts clean on port 8008
- [ ] `/health` returns 200
- [ ] `/system-health` returns valid JSON with all required fields
- [ ] launchd plist created and service registered
- [ ] o3-mini adversarial pass completed (findings documented)
- [ ] Gemini 2.5 Pro full-codebase scan completed (findings documented)
- [ ] MEMORY.md updated
- [ ] CHANGELOG.md entry added

---

*SPEC: pipeline-sentinel v1.0 — GENESIS — 2026-04-13*
