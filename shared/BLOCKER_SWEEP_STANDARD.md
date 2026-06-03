# NEXUS BLOCKER SWEEP — STANDARD TEMPLATE v1.1
**Issued by:** Ahmed Sadek | **Effective:** 2026-04-28 | **CHRONICLE:** blocker_sweep_template_v1

---

## SCHEDULE (Mon–Fri, ET)
| Agent    | Time      | Domain                          |
|----------|-----------|---------------------------------|
| SOVEREIGN| :00/hour  | Full system view                |
| GENESIS  | :15/hour  | Code + deployment integrity     |
| OMNI     | :30/hour  | Synthesis + execution pipeline  |
| CIPHER   | :45/hour  | Scanning pipeline + config      |

Active: 8:00 AM – 3:45 PM ET

---

## BLOCKER SEVERITY (universal triage language)
| Level | Symbol | Meaning                                    | SLA                          |
|-------|--------|--------------------------------------------|------------------------------|
| P0    | 🔴     | Trade execution blocked OR money at risk   | Fix within current sweep     |
| P1    | 🟠     | Service degraded, trades impaired          | Fix within 2 sweeps (30 min) |
| P2    | 🟡     | Non-critical anomaly                       | Fix within trading day       |
| P3    | ⚪     | Observation, no immediate impact           | CHRONICLE entry only         |

**Escalation:**
- P0 unresolved → SOVEREIGN immediately
- P0 SOVEREIGN cannot resolve → Ahmed (only case Ahmed is paged)
- P1 unresolved in 2 sweeps → SOVEREIGN
- P2/P3 → never escalate, just fix and log

---

## 6-PHASE TEMPLATE

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[AGENT] BLOCKER SWEEP — [HH:MM] ET
Date: [YYYY-MM-DD] | Domain: [agent domain]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PHASE 1 — SERVICE HEALTH
SERVICE        PORT   STATUS    KEY METRIC
Alpha Exec     8005   ✅        paused=F | open=2 | trades=4
Prime Exec     8006   ✅        paused=F | open=1 | trades=8
OMNI           8004   ✅        syntheses=22 | GOs=15
Alpha Buffer   8002   ✅        cb=NORMAL | pending=0
Guardian       8009   ✅        heartbeat=12s
Oracle         8007   ✅        cache_hit=0.41
THESIS         8060   ✅        scheduler=running
Railway Alpha   —    ✅        loop=T | L11=NORMAL | 1.0x
Railway Prime   —    ✅        paused=F
Alpaca          —    ✅        ACTIVE | equity=$91k | dt=13

PHASE 2 — DEEP SCAN ([agent domain focus])
[Agent-specific checks — see domain focus table below]

PHASE 3 — BLOCKERS FOUND
✅ NO BLOCKERS — system clear
[OR:]
🔴 P0 BLOCKER #1: [title]
  Detected: [where found]
  Root cause: [specific diagnosis]
  Fix applied: [exact action]
  Verification: [how confirmed]
  Permanent? Yes/No
  CHRONICLE: registered ✅

PHASE 4 — PREEMPTIVE FLAGS
No preemptive flags.
[OR:]
⚠️ [service/metric] at [value] → approaching [threshold]
⚠️ [pattern] observed → [why it matters]

PHASE 5 — ESCALATION
No escalation needed.
[OR:]
→ SOVEREIGN: [P0/P1 unresolved — what and why]
→ Ahmed: [only if P0 SOVEREIGN cannot resolve]

PHASE 6 — SWEEP SUMMARY
Blockers found:    [N]  (P0: N | P1: N | P2: N | P3: N)
Blockers fixed:    [N]
Blockers pending:  [N]  (escalated to [agent])
Preemptive flags:  [N]
Sweep duration:    [Xs]
Next sweep:        [agent] at [time]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## AGENT DOMAIN FOCUS (Phase 2)

**SOVEREIGN:** All service health, CANARY, CHRONICLE P0s, bus escalations, open positions, Alpaca account
**GENESIS:** Config integrity, Railway versions, import errors, phantom patterns, log error counts
**OMNI:** Execution failures last 60min, concordance pipeline, CANARY gate, THESIS connectivity, brain timeout rate
**CIPHER:** Agent submission volume, 422 rejection rate, config values (GO_THRESHOLD_P1=63, MIN_SCORE_P2=62, MAX_NEW_PER_DAY=50), guardian heartbeat, healing DB
**PRIMUS:** V2 services (ATM/ATG/Capital Router), reconciliation status

---

## KEY PRINCIPLES

1. **Phase 1 before Phase 2** — health check first, fast-fail on unreachable
2. **Preemptive flags** — check trend lines, not just current state. This stops repeat failures.
3. **Permanent resolution** — root cause fixed, not symptom patched. If only symptom: PENDING + escalation
4. **CHRONICLE everything** — P0/P1 always. Recurring blockers get pattern entries.
5. **Silent if clean** — NO_REPLY when nothing to report
6. **Report only after fixing** — never report a problem without a fix

---
*Template v1.1 — severity definitions added by GENESIS (2026-04-28)*
*Registered in CHRONICLE: doc_key=blocker_sweep_template_v1*
