# SPEC: Mandate Overnight P0/P1 Auto-Escalation
**Version:** 1.1  
**Date:** 2026-05-06  
**Author:** GENESIS  
**Status:** SPEC_QA_COMPLETE — QA FINDINGS ADDRESSED  
**SOVEREIGN Directive:** Overnight P0/P1 Auto-Escalation (HIGH PRIORITY)

---

## QA Council Findings Addressed (v1.0 → v1.1)

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| QA-1 | P0 | `incidents.severity` actual values are `HIGH`/`CRITICAL`/`P1`/`P2` — not `P0`/`P1`. Spec filter matched zero rows. | Added severity normalization map. |
| QA-2 | P0 | Duplicate alerts: same incident can appear in both `incidents` AND `mandate_compliance`, causing double-page. | Added deduplication by `incident_id` cross-reference. |
| QA-3 | P1 | Multi-day unresolved P0s excluded because `detected_at < today` scopes to current calendar day. | Changed lookback to 7 days. |
| QA-4 | P1 | Ahmed DM failure behavior → SOVEREIGN path undefined. | Explicit: DM fail → continue to SOVEREIGN regardless. |
| QA-5 | P1 | Consolidated vs per-incident messages unspecified. | Explicit: one consolidated message per recipient. |
| QA-6 | P2 | DB timestamps are mixed format (`Z`, `+00:00`, `-04:00`). | Require UTC normalization before all comparisons. |
| QA-7 | P2 | No idempotency guard for duplicate cron fires. | Add `cron_log` last-run check (2h window). |
| QA-8 | P3 | `failure_type` matching semantics unspecified. | Explicit: exact string match only. |

---

## Purpose

Add an 11 PM ET nightly escalation sweep to `mandate_compliance_report.py` that pages Ahmed DM and SOVEREIGN directly if any P0 or P1 incidents remain unactioned at that time. Operates independently of OMNI and the morning premarket sweep — must function even when no other agents are live overnight.

---

## Problem Statement

The existing 3× daily schedule (8 AM / 12 PM / 4:30 PM ET) creates a ~18.5-hour gap in escalation coverage. A P0 incident logged at 5 PM could sit unactioned until the 8 AM premarket brief. The overnight window is precisely when manual oversight is zero.

**Current gap:** EOD report fires at 4:30 PM → next sweep at 8 AM = 15.5 hours of no escalation path.

---

## Inputs

- CHRONICLE DB: `/Users/ahmedsadek/nexus/data/chronicle.db`
  - `incidents` table — `severity` field. **Actual values in production:** `CRITICAL`, `HIGH`, `P1`, `P2` (not `P0`/`P1` strings)
  - `mandate_compliance` table — `outcome` and `intervention_at` fields
- Time: 11:00 PM ET (America/New_York), Monday–Sunday — Mac mini runs in ET, cron fires at local time
- Severity normalization map (canonical → internal):
  - **P0** = `incidents.severity IN ('CRITICAL', 'P0')` OR `mandate_compliance.failure_type IN ('service_down', 'execution_error')` with no `intervention_at`
  - **P1** = `incidents.severity IN ('HIGH', 'P1')` OR `mandate_compliance.failure_type IN ('pipeline_blocked', 'ghost_position', 'credential')` with no `intervention_at`
- Timestamp normalization: all `detected_at` values normalized to UTC via `datetime.fromisoformat()` with `.astimezone(utc)` before comparison — handles mixed `Z`, `+00:00`, and offset-aware formats

---

## Outputs

### When unactioned P0/P1 findings exist at 11 PM ET:

**Ahmed DM (Telegram `8573754783`):**
```
🚨 OVERNIGHT ESCALATION — P0/P1 UNACTIONED
11:00 PM ET | 2026-05-06

UNACTIONED FINDINGS: 2

─────────────────────────────
INC-20260506-001 | P0 | service_down
Agent: CIPHER → alpha-buffer
Detected: 16:45 ET | Intervention: NONE
Age: 6h 15m
Damage: No picks routed since 16:45

INC-20260506-002 | P1 | pipeline_blocked
Agent: OMNI → synthesis-loop
Detected: 18:30 ET | Intervention: NONE
Age: 4h 30m
Damage: Not recorded
─────────────────────────────

These incidents require immediate attention.
No agent self-resolved them before end of day.
```

**SOVEREIGN (sessions_send to `agent:sovereign:main`):**  
Same content + a directive line: `DIRECTIVE: Review and action or assign these findings before market open.`

### When no unactioned P0/P1 findings exist at 11 PM ET:

- Post a clean single-line confirmation to NEXUS HEALTH GROUP only: `✅ OVERNIGHT SWEEP 11PM ET — No unactioned P0/P1 findings. System clear.`
- Do NOT page Ahmed or SOVEREIGN.

---

## Logic

### What counts as "unactioned"

An incident is unactioned at 11 PM if ALL of these are true:
1. `detected_at` is within the last **7 days** (not scoped to today only — persistent multi-day P0s must surface)
2. `detected_at` is more than 30 minutes before sweep time (grace window — see below)
3. `intervention_at` IS NULL — no agent has touched it
4. `outcome` IN ('OPEN', NULL) — not yet resolved
5. `severity` normalized to P0/P1 via the severity normalization map (see Inputs above)

Grace period: incidents detected within 30 minutes of 11 PM ET (i.e., after 10:30 PM ET) are excluded — agents may still be responding to late detections.

### Deduplication

An incident may appear in BOTH `incidents` and `mandate_compliance` tables (same `incident_id` / `id` key). Deduplication rule:
1. Collect all unactioned candidates from both tables
2. Key by `incident_id` (from `mandate_compliance`) or `id` (from `incidents`) — same format: `INC-YYYYMMDD-HHMMSS-XXXXXX`
3. If the same key appears in both tables, use the **`incidents` row** (explicit severity takes precedence over inferred)
4. Each unique incident ID appears in the report exactly once
5. This prevents double-paging Ahmed for the same event

### Severity inference (for mandate_compliance rows without an incidents cross-reference)

| failure_type | Inferred Severity |
|---|---|
| service_down | P0 |
| execution_error | P0 |
| pipeline_blocked | P1 |
| ghost_position | P1 |
| credential | P1 |
| data_source | P2 |
| db_inconsistency | P2 |
| health_parse | P3 |
| default | P2 |

Cross-reference: If the incident_id also appears in the `incidents` table, use the explicit `severity` field from that table instead.

---

## Contracts

**Guarantees:**
- Ahmed DM is sent before SOVEREIGN message — if Telegram fails, attempt SOVEREIGN anyway
- Each delivery attempt is retried once (simple retry, 5s wait) before logging failure
- Both `mandate_compliance` and `incidents` tables are scanned — either source triggers escalation
- Run completes in under 30 seconds
- Runs every night including weekends — P0s don't have days off

**Does NOT guarantee:**
- That SOVEREIGN acts on the directive (that is SOVEREIGN's mandate)
- Delivery if Telegram is unreachable (logged to CHRONICLE, does not crash)
- Deduplication across the 4:30 PM EOD report and the 11 PM sweep (overlap is intentional — better to page twice than miss once)

---

## Failure Modes

| Failure | Behavior |
|---|---|
| CHRONICLE DB unreachable | Log to stderr; send "🚨 OVERNIGHT SWEEP FAILED — DB UNREACHABLE" to Ahmed DM (hardcoded TG_BOT_TOKEN + AHMED_DM in script — no CHRONICLE dependency) |
| Ahmed DM (Telegram) failure | Retry once (5s); log failure; **continue to SOVEREIGN attempt regardless** — never abort both notifications due to one channel failure |
| SOVEREIGN message bus failure | Log failure to stderr; Ahmed DM already sent — sweep completes |
| Empty `incidents` table | Proceed with `mandate_compliance` scan only — not an error |
| Script crash (unhandled exception) | LaunchAgent restarts automatically; uncaught errors logged to stderr |
| Duplicate cron fire within 2h | Check `cron_log` table — if `overnight` type run within last 2 hours, exit 0 silently (idempotency guard) |

---

## Implementation Details

### 1. New function in `mandate_compliance_report.py`
```python
def overnight_escalation_sweep():
    """Run at 11 PM ET. Page Ahmed + SOVEREIGN if unactioned P0/P1 exists."""
```

### 2. Report type: `"overnight"` 
Add to the `if len(sys.argv) > 1: report_type = sys.argv[1]` branch.  
Auto-detect: `hour >= 22` → `report_type = "overnight"`.

### 3. SOVEREIGN delivery
Use `sessions_send` via the OpenClaw inter-session API or POST to message bus:
```
POST http://192.168.1.141:9999/send
{"from": "genesis", "to": "sovereign", "message": "<report>"}
```
Fallback: if message bus fails, log failure — do not suppress Ahmed DM.

### 4. Cron schedule addition
Add to the existing LaunchAgent or crontab:
```
# 11 PM ET nightly (Mac mini runs America/New_York — local time = ET)
0 23 * * * /Users/ahmedsadek/nexus/.venv/bin/python /Users/ahmedsadek/nexus/scripts/mandate_compliance_report.py overnight
```

### 5. Idempotency guard
At sweep start, check `cron_log` for an `overnight` entry within the last 2 hours. If found, exit 0 silently. On completion, write a `cron_log` entry with type `overnight` and current UTC timestamp.

### 6. failure_type matching
All `failure_type` comparisons use **exact string match** only. `credential_expired` does NOT match `credential`. Unrecognized types fall to inferred P2 (default) — never silently promoted to P0/P1.

---

## Test Cases

1. **Happy path — no unactioned P0/P1:** CHRONICLE has 3 incidents, all resolved or P2/P3. Script sends group confirmation only. Ahmed and SOVEREIGN not paged. ✓
2. **P0 unactioned — service_down, no intervention_at:** Script sends escalation to Ahmed DM + SOVEREIGN with incident details. ✓
3. **P1 unactioned — pipeline_blocked, intervention_at NULL:** Included in escalation. ✓
4. **Incident within 30-min grace window (10:45 PM detection):** Excluded from escalation. ✓
5. **P2 unresolved incident:** NOT included in overnight escalation page. ✓
6. **Cross-reference: mandate_compliance says P1 type, incidents table says CRITICAL:** Uses `incidents.severity = CRITICAL` → normalized to P0. ✓
7. **Same incident in both tables (duplicate):** Appears once in report — deduplication by incident_id. ✓
8. **3-day-old unresolved CRITICAL incident (not just today):** Included — 7-day lookback. ✓
9. **CHRONICLE unreachable:** Ahmed DM sent with "SWEEP FAILED — DB UNREACHABLE" (TG hardcoded). No crash. ✓
10. **Telegram DM fails:** Sweep continues to SOVEREIGN attempt regardless. ✓
11. **Duplicate cron fire 30s later:** Second run detects `cron_log` entry < 2h, exits 0 silently. ✓
12. **Weekend run (Saturday 11 PM):** Executes normally. ✓
13. **Severity `HIGH` in incidents table:** Normalized to P1 — included in escalation. ✓
14. **`failure_type = credential_expired` (not exact match for `credential`):** Falls to P2 default — NOT included in overnight escalation. ✓

---

## Out of Scope

- No changes to the 3× daily schedule (premarket / midday / EOD)
- No new CHRONICLE tables required
- No modification to incident severity assignment (that is each agent's responsibility at log time)
- No alerting for P2/P3 overnight — they can wait for premarket brief

---

## Rollback

`git revert <sha>` removes the overnight function and cron entry. Existing 3× daily schedule unaffected.
