# SPEC: Nexus Sentinel Prime (NSP) v3
**Authorized by:** Ahmed Sadek  
**Date:** 2026-04-30  
**From:** VECTOR  
**Status:** AWAITING_SPEC_APPROVAL  

---

## PURPOSE

One unified process — Nexus Sentinel Prime (NSP) — that senses every system signal, predicts failures before they happen, heals to the root cause, and escalates through a redundant agent network when autonomous resolution fails.

Nexus Integrity becomes an **imported Python library** inside NSP. Not a peer service. Not queried over HTTP. Imported. One process, one state machine, one lock. No authority conflicts.

---

## INPUTS

- HTTP /health endpoints from all 9 Nexus services (ports 8001–8009)
- Scanner endpoints (Cipher 9001, Atlas 9002, Sage 9003)
- SQLite telemetry DB (local WAL mode)
- CHRONICLE (192.168.1.42:8020) — read baselines on startup, async write audit trail
- Message bus (192.168.1.141:9999) — broadcast/receive escalation claims
- psutil — process-level memory, CPU, disk
- Log files — scan for polygon_timeout_count, db_write_error_count, polygon_timeout_rate, write_error_count

## OUTPUTS

- Autonomous service interventions (restart, cache warm, prefetch, rollback)
- Escalation packages to VECTOR, Cipher, Axiom, GENESIS, SOVEREIGN
- Intervention lock claims on message bus
- CHRONICLE audit trail (async, buffered when .42 unreachable)
- Local SQLite intervention log (primary, always-write)
- P0/P1 Telegram alerts to Ahmed (8573754783) + SOVEREIGN

## CONTRACTS

**Guarantees:**
- Trading state gate is hardcoded and cannot be bypassed by config
- Circuit breaker limits are hardcoded (3 restarts/service/hr, 10 total/hr)
- Every intervention has a defined undo (rollback library)
- State persists to SQLite BEFORE every action (mid-intervention recovery on restart)
- CHRONICLE writes never block an intervention
- Dead telemetry records (past expires_at) never vote in any score calculation

**Explicitly does NOT guarantee:**
- Real-time CHRONICLE sync (async, up to 60s lag)
- Intervention during LIVE hours for non-DEAD/non-severe-DEGRADED classes
- Autonomous fix for CODE or CONFIG_DRIFT classes (escalate only)

---

## LAYER -1: DEAD MAN'S SWITCH

**Standalone process. Separate launchd plist. Cannot share NSP's fate.**

- Watches NSP `/health` every 30 seconds
- 2 consecutive failures → P0 alert to Ahmed (8573754783) + SOVEREIGN via message bus
- Writes own heartbeat to `/tmp/nsp_dms_heartbeat`
- ~10 lines of logic. Zero dependencies on NSP internals.

---

## LAYER 1: TELEMETRY FABRIC

**Architecture:** Single writer thread. Async queue for all collectors. WAL mode SQLite.

**Schema requirement:** Every record has `expires_at = created_at + 4 hours`. Purge job runs every 30 minutes. Dead records never vote in any score calculation.

**Adaptive polling per service:**
- `HEALTHY`: poll every 30s
- `AMBER` / anomaly detected: poll every 5s
- `POST-RESTART`: poll every 2s for first 5 minutes, then revert to HEALTHY rate

**Signals to collect — every service, every poll:**

| Service | Signals |
|---|---|
| AXIOM (8001) | pool_size, regime, regime_last_updated_age_s, vix, tier2_consecutive_failures, submissions_open, polygon_timeout_count (log scan) |
| ALPHA-BUFFER (8002) | cb_status, omni_retry_queue_depth, db_write_error_count (log scan) |
| PRIME-BUFFER (8003) | cb_status, omni_retry_queue_depth, db_write_error_count (log scan) |
| OMNI (8004) | brain_error_rate, last_synthesis_age_s, pool_active_threads, canary_gate_status, halt_active, echo_chamber_count |
| ALPHA-EXEC (8005) | alpaca_reachable, execution_paused, vix_brake, ticker_fail_counts, reconcile_mismatches, stale_pending_positions |
| PRIME-EXEC (8006) | alpaca_reachable, execution_paused, vix_brake, ticker_fail_counts, reconcile_mismatches, stale_pending_positions |
| ORACLE (8007) | cache_hit_rate, cache_warm_count, polygon_timeout_rate (log scan), engine_error_count |
| AILS (8008) | ails_db_status, backtest_db_status, write_error_count (log scan) |
| SCANNERS (9001–9003) | brain_failures, today_picks, total_windows, picks_per_window_ratio |
| System (psutil) | memory %, CPU %, disk % per service process |

**Self-preservation gate:** Before every intervention, NSP checks its own telemetry DB lag. If own DB write lag > 2s → stand down all interventions, fire P1 alert only.

**State persistence:** ALL intervention state (attempt counters, fix history, current baselines, intervention locks) written to SQLite BEFORE every action. NSP restart must recover mid-intervention state cleanly.

---

## LAYER 2: PREDICTIVE ENGINE

Runs every 60 seconds on telemetry store.

**Calibration mode (MANDATORY):**
- First 5 clean trading days: observe only, zero interventions
- Baselines initialized from CHRONICLE historical data if available
- Days where TRS < 60 for more than 2 hours: excluded from baseline calculation
- Calibration mode flag persisted in SQLite, survives restarts
- Day 6+: predictive engine activates

**Signal processing:**
- Minimum 3 consecutive readings outside baseline before triggering
- Market-hours-aware baselines (VIX at 11AM ≠ VIX at 3:30PM)
- Linear regression for trend, not single-point comparison

**Pre-emptive trigger rules:**

| Signal | Warning | Critical | Action |
|---|---|---|---|
| Oracle cache_hit_rate falling | <0.65 + declining | <0.50 | Trigger cache warm — NOT restart |
| OMNI brain_error_rate rising | >25% over 10min | >40% | Probe individual brain APIs |
| Axiom pool_size declining | <10 + falling | <5 | Force tier1 refresh |
| Axiom regime_last_updated_age_s | >45min | >60min | Force regime reload |
| Synthesis silence | >15min market hours | >25min | Restart OMNI |
| Execution reconcile_mismatches | >0 for 5min | >2 | Pause execution + escalate |
| AILS write_error_count | Any | >3/hr | Escalate GENESIS (CODE class) |
| Memory pressure | >80% | >90% | Controlled recycle |

---

## LAYER 3: INTERVENTION ENGINE

### Operating Modes — enforced at every intervention decision

| Mode | Hours | Policy |
|---|---|---|
| 🌙 DARK | 6PM–8AM | Fully aggressive — restart anything, reset caches |
| 🌅 TRANSITION | 8–9:30AM, 4–6PM | Fix what's needed for next session |
| ⚠️ LIVE | 9:30AM–4PM | DEAD and severe DEGRADED only — queue everything else for DARK |

### Trading State Gate — HARDCODED, NOT CONFIGURABLE

Before restarting ANY of: Alpha-Execution, Prime-Execution, OMNI, Prime-Buffer, Axiom:

```python
if open_positions > 0 OR pending_orders > 0:
    defer intervention
    notify SOVEREIGN immediately
    # DO NOT PROCEED
```

- This gate **cannot be bypassed by config**. It **cannot be disabled**.
- If the gate check itself fails: abort intervention, escalate.

### Circuit Breaker — HARDCODED

- Max 3 restarts per service per hour
- Max 10 total interventions per hour across all services
- Either limit hit → **FREEZE mode** (zero autonomous actions)
- FREEZE lifted only by SOVEREIGN or Ahmed explicitly via API call

### Root Cause Classifier — explicit decision trees

```
# DEAD
if consecutive_failed_polls >= 3 AND process_not_in_ps:
    class = DEAD → restart

# UNRESPONSIVE (not DEAD)
if consecutive_failed_polls >= 3 AND process_exists:
    class = UNRESPONSIVE → wait 30s, recheck before restarting

# DEGRADED
if status == "degraded" AND process_alive:
    if cache_warm == 0:          class = DEGRADED → trigger prefetch, not restart
    if brain_error_rate > 0.4:   class = DEGRADED → probe brain APIs
    if cb_status != "NORMAL":    class = DEGRADED → alert, do not restart

# STARVED
if service_A degraded AND dependency_of_A is DOWN:
    class = STARVED → fix dependency source first, suppress A alerts

# CASCADING
if same_service restarted 3+ times in 10 minutes:
    class = CASCADING → FREEZE that service, escalate immediately

if 3+ services fail within 60 seconds:
    class = CASCADING → FREEZE all restarts, escalate immediately

# EXTERNAL
if polygon_api returning 503 OR alpaca_api returning 503:
    class = EXTERNAL → MAINTENANCE_HOLD on all downstream services
    suppress anomaly detection on: Oracle, OMNI, AILS, Axiom
    resume when external source recovers

# CONFIG_DRIFT (7th class)
if running_config != known_good_snapshot:
    class = CONFIG_DRIFT → diff + alert Ahmed, no auto-fix without approval

# CODE
if SQLite OperationalError OR TypeError OR logic exception in logs:
    class = CODE → escalate GENESIS immediately, no infra fix attempted
```

### 3-Attempt Rule with per-class observation windows

| Class | Window before Attempt 2 | Window before Attempt 3 |
|---|---|---|
| DEAD | 20s | 45s |
| UNRESPONSIVE | 60s | 120s |
| DEGRADED | 120s | 300s |
| STARVED | Fix dependency first | — |
| CASCADING | Skip to escalation | — |
| EXTERNAL | No attempts | — |
| CODE | No attempts | — |

### Mandatory Rollback

Every fix in the intervention library ships with a defined undo. Before executing any fix:
1. Snapshot current state
2. Execute fix
3. If fix makes things worse: execute rollback automatically

---

## ESCALATION

Fires after 3 failed attempts OR circuit breaker trips.

### Intervention Lock Protocol

1. NSP posts claim to message bus: `{service, incident_id, lock_holder: "nsp", expires_at: T+300}`
2. Escalation broadcast goes to all 5 agents
3. Each agent checks bus for active claim before acting
4. If locked by another agent: observe and advise only
5. Lock expires after 5 minutes if no resolution confirmed

### Differentiated Agent Packages

| Agent | Package | Role |
|---|---|---|
| **Vector** | Infra action package — launchctl commands, log paths, current state | Acts first (2-min window) |
| **Cipher** | Logic audit package — verify no bad trades during failure window, trading state check | Trading safety |
| **Axiom** | Risk gate package — open positions, market conditions, safe to intervene? | Risk authority |
| **GENESIS** | Code audit package — dispatched only for CODE or CONFIG_DRIFT class. No live deploys during market hours | Code authority |
| **SOVEREIGN** | Command package — full incident summary, decision authority | Command authority |

**Escalation timeout:** Max 6 hours continuous. After 6hr unresolved → pause loop + single consolidated summary to Ahmed.

### CHRONICLE Audit Trail

- NSP writes to local SQLite intervention log **immediately**
- Separate async process syncs to CHRONICLE (192.168.1.42:8020) every 60s
- If .42 unreachable: buffer locally, replay when connectivity restores
- **Never block intervention on CHRONICLE write**

**CHRONICLE schema for every intervention record:**

```
failure_class, service, signal_chain, fix_attempt_log,
duration_to_resolve_s, trading_impact (bool),
execution_suspended (bool), outcome, actual_root_cause,
predicted_root_cause, baseline_feedback_signal
```

---

## POST-MORTEM LEARNING LOOP

After every intervention (success or failure):
- Record: what was attempted, outcome, actual vs predicted root cause
- Generate feedback signal to adjust baseline thresholds
- Flag if prediction was wrong: recalibrate that signal's model

---

## PHASING (21 days — DO NOT COMPRESS)

| Phase | Days | Deliverables |
|---|---|---|
| **Phase 1** | 1–3 | DMS + telemetry fabric + WAL SQLite + TTL schema + calibration mode + state persistence + NI as imported library |
| **Phase 2** | 4–8 | Root cause classifier + trading state gate + circuit breaker + intervention lock + EXTERNAL_SHADOW + CHRONICLE schema + local buffer |
| **Phase 3** | 8–12 | Predictive engine + adaptive polling + 5-day bootstrap + market-hours baselines + operating modes |
| **Phase 4** | 12–16 | Intervention engine + rollback library + per-class observation windows + CASCADING hard rules |
| **Phase 5** | 16–19 | Escalation bus + lock primitive + differentiated agent packages + 6hr timeout |
| **Phase 6** | 19–20 | Cut Sentinel fully, 24hr parallel validation, NSP takes full authority |
| **Phase 7** | 20–21 | Post-mortem loop + CONFIG_DRIFT class + baseline feedback + hardening |

> "Do not compress this timeline. The intervention engine needs 2+ weeks of testing minimum per Axiom's assessment."

---

## VECTOR SUPPORT COMMITMENTS

- Infrastructure testing at each phase
- Health verification after each deployment
- Log monitoring during NSP bring-up
- Escalation path testing (end-to-end agent broadcast)
- Field reports to group on each phase completion

---

## FAILURE MODES

| Failure | Response |
|---|---|
| NSP process dies | DMS fires P0 within 60s |
| NSP telemetry DB lag > 2s | Self-preservation gate: stand down all interventions, P1 alert only |
| CHRONICLE unreachable | Buffer locally, replay on restore — never block intervention |
| Trading state gate check fails | Abort intervention, escalate to SOVEREIGN |
| 3+ services fail within 60s | CASCADING class → FREEZE all restarts, escalate |
| CODE class detected | No infra fix — escalate GENESIS immediately |
| 6hr unresolved escalation | Pause loop, consolidated summary to Ahmed |

---

## TEST CASES (minimum)

1. **Happy path:** Service goes DEAD → classifier detects → trading gate clear → DARK mode → restart → recovery logged
2. **Trading gate block:** Service DEAD during LIVE hours with open positions → defer → notify SOVEREIGN → no restart
3. **Circuit breaker trip:** Service restarted 3x in 60min → FREEZE → SOVEREIGN escalation
4. **CASCADING:** 3 services fail within 60s → FREEZE all → escalation broadcast
5. **EXTERNAL class:** Polygon 503 → MAINTENANCE_HOLD → suppress downstream anomalies → resume on recovery
6. **CODE class:** SQLite OperationalError in logs → escalate GENESIS, no restart attempted
7. **Rollback:** Fix applied, service degrades further → automatic rollback to snapshotted state
8. **Calibration mode:** Day 1–5 → zero interventions regardless of signal anomalies
9. **State recovery:** NSP killed mid-intervention → restart → resumes from SQLite state correctly
10. **DMS:** NSP /health fails 2× → DMS fires P0 to Ahmed + SOVEREIGN
11. **Self-preservation gate:** NSP DB write lag > 2s → stand down, P1 alert only
12. **CHRONICLE buffer:** .42 unreachable → interventions proceed, buffered locally → replayed on restore
13. **Predictive warning:** Oracle cache_hit_rate declining + <0.65 → cache warm triggered, NOT restart

---

## APPROVAL REQUIRED BEFORE BUILD BEGINS

This spec requires Ahmed's explicit approval before GENESIS opens any code editor.

**APPROVED:** Ahmed Sadek — 2026-04-30 11:04 EDT
**Timeline:** 11-day compressed build (calibration 5 trading days non-negotiable)
