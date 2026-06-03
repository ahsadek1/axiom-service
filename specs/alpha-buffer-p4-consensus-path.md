# SPEC: Alpha Buffer — P4 Consensus Concordance Path
**ID:** GENESIS-SPEC-2026-05-04-002  
**Author:** GENESIS  
**Status:** AWAITING AHMED APPROVAL  
**Date:** 2026-05-04  
**Priority:** HIGH — directly caused 0 pipeline trades on 2026-05-04

---

## Purpose

Activate a P4 concordance path in the Alpha Buffer for cases where all 3 agents 
agree on a ticker/direction but each scores in the 60–64 range — below the current 
P2 threshold of 65 but clearly above the noise floor (58).

Today (2026-05-04), 95 of 511 tickers (18.6%) landed in the 60–69 band. None 
could form a concordance. The P2 threshold of 65 means any tick where all three 
agents agree at moderate (not strong) conviction produces nothing.

---

## Problem Statement (Precise)

Current concordance paths:
```
P1: 3/3 agents, weighted ≥ 65 → 100% size
P2: 2/3 agents, each ≥ 65     →  75% size  
P3: 1 agent, score ≥ 90       →  50% size (solo, currently disabled)
P4: OMNI-initiated only        →  25% size (not triggered by concordance engine)
```

The 60–64 band has no route. A ticker where all three agents independently arrive 
at a score of 61-63 — unanimous agreement at moderate conviction — contributes zero 
to the pipeline. This is the most common productive scoring band.

The comment in `config.py` acknowledges this directly:
> "Agents consistently score 58-68 on quality setups."

The 65 threshold was recalibrated on 2026-04-24 (was 78). But the floor of 65 still 
cuts off the most common quality band.

---

## Proposed Solution

**Activate P4 as a 3-agent consensus path within `concordance.py`.**

### P4 Trigger Conditions (all must be true):
1. All 3 valid agents have submitted for the ticker/direction/window
2. Each individual agent score ≥ **60** (above submission floor of 58, meaningful signal)
3. Weighted score (Cipher 45% / Atlas 30% / Sage 25%) ≥ **62** (consensus floor)
4. No echo chamber detected (same check as P1/P2)
5. P1 and P2 were not triggered first (P4 is fallback only)

### P4 Sizing:
- `sizing_mult = 0.25` (25% of base position = $500 on $25K paper)
- This is the smallest live size in the system — limited capital at risk
- Verdict label: `"GO"` only (STRONG_GO requires weighted ≥ 80 — not applicable here)

### Position in Logic Flow:
```
P1 check → P2 check → P3 check (if solo enabled) → P4 check (new) → None
```

P4 fires ONLY when all prior paths fail.

---

## Inputs

- `concordance.py` — `evaluate_concordance()` function
- `config.py` — add `MIN_SCORE_P4 = 60.0` and `MIN_WEIGHTED_P4 = 62.0` constants
- `database.py` — already accepts P4 in pathway column (VALID_PATHWAYS includes P4)
- `PATHWAY_SIZING` in `config.py` — `"P4": 0.25` already exists

---

## Outputs

**Success case:** On a session where agents score 60–64 unanimously, P4 concordances 
reach OMNI. OMNI still makes the final synthesis decision (can return NO_GO). 
No P4 trade executes without OMNI validation.

**Unchanged behavior:**
- P1, P2, P3 logic untouched
- OMNI is still the last gate before execution
- Circuit breaker rules apply identically to P4 trades
- Max positions and max per day limits apply

---

## Contracts

**Guarantees:**
- P4 only fires when ALL 3 agents agree (not 2 of 3)
- Each agent score must be ≥ 60 individually (not just weighted average)
- Echo chamber detection runs identically to P1
- P4 sizing is strictly 0.25x — never higher
- P4 cannot upgrade to STRONG_GO (score range is too low)

**Does NOT guarantee:**
- That P4 produces profitable trades (OMNI still filters)
- More than 25% base position size ($500 paper max) per P4 trade

---

## Failure Modes

| Scenario | Handling |
|---|---|
| P4 fires when only 2 agents present | Guard: `participating_agents == VALID_AGENTS` check |
| P4 fires with score below 60 for any agent | Guard: individual score check before weighted |
| P4 + echo chamber | Guard: echo chamber returns None (same as P1 downgrade) |
| P4 fires when P2 was already triggered | Guard: P4 is last in the chain, only reached if P1/P2 failed |
| P4 triggers but OMNI rejects | Expected: OMNI is the synthesis gate, rejection is correct behavior |

---

## Test Cases (minimum 5)

1. **3/3 agents, all score 62** → P4 triggered, `sizing_mult=0.25`, `pathway="P4"`
2. **3/3 agents, scores [63, 60, 58]** → P4 NOT triggered (58 < 60 individual floor)
3. **3/3 agents, scores [64, 62, 61], echo chamber** → P4 NOT triggered
4. **3/3 agents, weighted score 59 (e.g. scores [62, 60, 55])** → P4 NOT triggered (weighted < 62)
5. **2/3 agents, both score 62** → P4 NOT triggered (requires all 3 agents)
6. **3/3 agents, scores [66, 61, 60]** → P2 triggered first (Cipher+Atlas qualify at ≥65), P4 never reached
7. **3/3 agents, all score 65** → P1 or P2 triggered (not P4 — P4 is fallback only)

---

## Files Changed

- `/Users/ahmedsadek/nexus/alpha-buffer/config.py` — add `MIN_SCORE_P4 = 60.0`, `MIN_WEIGHTED_P4 = 62.0`
- `/Users/ahmedsadek/nexus/alpha-buffer/concordance.py` — add `_try_p4()` function and call in `evaluate_concordance()`

---

## Risk Assessment

**Risk: LOW**

P4 is 25% base position size ($500 max paper). On a $25K account, even 5 simultaneous 
P4 trades = $2,500 deployed = 10% of capital. The position size limit and max-new-per-day 
caps provide the outer bound.

More importantly: OMNI still makes the final call. A P4 concordance reaching OMNI 
with a weighted score of 62 will face the same 4-brain scrutiny as a P1 at 75. 
OMNI can and will return NO_GO on weak setups.

P4 trades will produce data: we'll see whether 60-64 unanimous agreement actually 
converts to profitable setups. If not, we disable P4 in 2 weeks with a config change.

**Circuit breaker sensitivity:** P4 losses count toward the circuit breaker the same 
as any other trade. If P4 produces consecutive losses, CB triggers normally.

**Reversibility:** Single constant change in config.py disables P4. Zero downtime.

---

## Expected Impact on Tomorrow (2026-05-05)

On today's tape (95 tickers in 60-69 band):
- The majority of the 60-64 band was single-agent or 2-agent submissions
- P4 requires all 3 agents — so not every 60-64 ticker becomes a P4
- Realistic estimate: 3-8 P4 concordances per session on a normal tape
- Each goes to OMNI; OMNI filters further
- Expected P4 executions: 0-3 per session initially

---

## AWAITING AHMED APPROVAL

**Ahmed — one decision needed:**

**Approve P4 activation?**
- Yes → I build, test, deploy tonight. Active for tomorrow open.
- Yes, but paper-only first (P4 marked for monitoring, not execution) → I can add a `P4_ENABLED=false` env flag
- No → Accept that unanimous moderate-conviction agreement produces 0 pipeline flow

**Recommended:** Approve with the `P4_ENABLED` env flag as a circuit breaker. 
Set to `false` initially. After one week of data showing OMNI behavior on P4 
concordances, enable live. This gives us data before capital exposure.

Reply with your decision and I'll build immediately.
