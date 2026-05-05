# NEXUS BLOCKER SWEEP — STANDARD TEMPLATE v1.0

**Effective:** 2026-04-29
**Issued by:** Ahmed Sadek
**Propagated by:** Cipher

---

## Schedule

| Agent    | Cron (ET)          | Times (weekdays)     |
|----------|--------------------|----------------------|
| Sovereign | `0 8-15 * * 1-5`  | 8:00–3:00 PM         |
| Genesis   | `15 8-15 * * 1-5` | 8:15–3:15 PM         |
| Omni      | `30 8-15 * * 1-5` | 8:30–3:30 PM         |
| Cipher    | `45 8-15 * * 1-5` | 8:45–3:45 PM         |
| Primus    | Per SQS schedule  | SQS domain only      |

---

## Canonical Output Template

Every agent's sweep output must match this format exactly — no deviations.

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[AGENT NAME] BLOCKER SWEEP — [TIME] ET
Date: [YYYY-MM-DD] | Run: [sweep #N today]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PHASE 1 — SERVICE HEALTH (all assigned services)
──────────────────────────────────────────────
[SERVICE]    [PORT]  [STATUS]  [KEY METRIC]
Alpha Exec    8005   ✅ healthy  trades=2 | open=2 | paused=False
Prime Exec    8006   ✅ healthy  trades=1 | open=3 | paused=False
OMNI          8004   ✅ healthy  syntheses=12 | go_verdicts=5
Axiom         8001   ✅ healthy  regime=NORMAL | vix=18.5
[...]         [...]  [❌/⚠️/✅]  [relevant metric]

PHASE 2 — DEEP SCAN (agent's domain focus)
──────────────────────────────────────────────
[Pull last 50-100 lines of relevant logs]
[Check known failure patterns for this agent's scope]
[Check CHRONICLE for unresolved P0/P1 findings]
[Check message bus inbox for pending escalations]

PHASE 3 — BLOCKERS FOUND
──────────────────────────────────────────────
[If none]: ✅ NO BLOCKERS — system clear

[If blockers]:
🔴 BLOCKER #1: [title]
  Detected:    [where/how found]
  Root cause:  [specific diagnosis]
  Fix applied: [exact action taken]
  Verification:[how confirmed resolved]
  Permanent?   [Yes / No — if No, what follow-up needed]
  CHRONICLE:   [registered Y/N | finding ID if P0/P1]

🟡 BLOCKER #2: [title]
  [same structure]

PHASE 4 — PREEMPTIVE FLAGS
──────────────────────────────────────────────
[Conditions trending toward a blocker but not yet one]
⚠️ [service/metric] approaching threshold — [current value vs limit]
⚠️ [pattern] observed — [why it matters, what to watch]

[If none]: ✅ No preemptive flags.

PHASE 5 — ESCALATION
──────────────────────────────────────────────
[If none]: No escalation needed.

[If needed]:
→ SOVEREIGN: [what and why]
→ GENESIS:   [what and why]
→ Ahmed:     [P0 only or decision required — what specifically]

PHASE 6 — SWEEP SUMMARY
──────────────────────────────────────────────
Blockers found:    [N]
Blockers fixed:    [N]
Blockers pending:  [N] (escalated to [agent])
Preemptive flags:  [N]
Sweep duration:    [Xs]
Next sweep:        [agent] at [time]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Agent Domain Focus (Phase 2 scope)

### SOVEREIGN
Full system view:
- All service health (all ports)
- CANARY status
- CHRONICLE P0s unaddressed
- Message bus escalations queue
- Open positions across Alpha + Prime

### GENESIS
Code and deployment:
- Railway service health
- Execution error logs
- Phantom position patterns
- Any code fix needed
- CHRONICLE findings assigned to Genesis

### OMNI
Synthesis layer:
- Concordance pipeline integrity
- Execution routing failures (429/422/503)
- Echo chamber false positives
- THESIS connectivity
- Brain timeouts
- CANARY gate status

### CIPHER
Scanning pipeline:
- Cipher/Atlas/Sage pick quality
- DTE violations
- Duplicate concordances
- Buffer concordance pool depth
- Alpaca API error patterns
- Railway scanning services

### PRIMUS
SQS domain:
- ATM Multi-Week, ATG Swing, ATM 0DTE, ATG Intraday, Capital Router health
- Trade + reconciliation status
- Per existing PRIMUS sweep template

---

## Key Design Principles

1. **Phase 1 before Phase 2** — always check health first, fast-fail on unreachable services
2. **Preemptive flags** — the differentiator from reactive sweeps. Flag trending problems before they become blockers
3. **Permanent resolution required** — fix must address root cause, not just symptom. If only symptom fixed, mark PENDING with escalation
4. **CHRONICLE registration** — every P0/P1 finding gets a CHRONICLE entry. Recurring blockers get a pattern entry
5. **Fix first, report after** — no report is sent until fix is applied and verified. Reporting an unfixed blocker is not acceptable

---

## Governing Law

Nexus Agent Law (2026-04-28):
- The agent who finds the problem owns it
- Immediate intervention — no standby, no permission needed
- Root cause only — permanent fix
- Inform Sovereign after acting
- Log everything to Chronicle

