# SPEC: THESIS Jones Gate Recalibration
**ID:** GENESIS-SPEC-2026-05-04-001  
**Author:** GENESIS  
**Status:** AWAITING AHMED APPROVAL  
**Date:** 2026-05-04  
**Priority:** HIGH — directly caused 0 GO verdicts on 2026-05-04

---

## Purpose

Recalibrate the Jones hard gate in THESIS so that moderate VIX environments (14–22) 
do not unconditionally zero all position sizing when other conditions are acceptable.

Today (2026-05-04), the Jones framework returned FAIL with `sizing_multiplier=0.0` 
because Claude interpreted VIX=16.89 as "complacent markets" = poor R:R. This zeroed 
every position for the full session. The claim is accurate in one sense — low VIX 
suppresses option premiums. But for a **credit spread system** selling premium, 
moderate-low VIX is actually a viable operating regime; it is not a NO-GO condition.

The Jones gate as currently written asks a general market R:R question. It needs to 
be credit-spread-aware.

---

## Root Cause (Precise)

Jones is a **HARD gate** — a FAIL returns `size_multiplier=0.0` unconditionally 
(see `thesis_engine.py` lines 150-159). Claude's Jones prompt instructs it to FAIL 
when "R:R < 3:1" and assesses VIX subjectively via the phrase "when VIX is above 35 
the volatility regime is destructive." 

VIX=16.89 is below 35. But Claude also weighs "entry quality" and "market structure" 
— and on today's tape (choppy, no clear momentum), Claude scored entry quality as 
"chasing" and market structure as "choppy" → FAIL.

This is not wrong in principle. But the consequence of a HARD FAIL being unconditional 
is too blunt for a systematic credit spread operation. A credit spread at moderate VIX 
with defined risk is structurally different from a directional equity trade in choppy conditions.

---

## Proposed Solution

**Two-part change:**

### Part A — Jones Prompt: Credit Spread Awareness
Update the Jones system prompt to explicitly address credit spread strategy context:
- VIX 14–22 = **viable** for credit spreads (defined risk, theta decay works in seller's favor)
- VIX < 14 = **reduced** premium — recommend sizing reduction, not FAIL
- VIX 22–35 = wider spreads available, can be favorable — assess R:R normally
- VIX > 35 = destructive vol — FAIL (same as today)
- "Choppy market" with defined-risk credit spreads should not auto-FAIL — the 
  risk is bounded by the spread width, not unlimited

This preserves Jones's philosophy (R:R must be ≥ 3:1) while giving Claude the right 
frame for evaluating a credit spread system vs. a directional momentum system.

### Part B — Jones Gate: HARD → GRADUATED (key change)
Change Jones from an unconditional HARD gate (`size_multiplier=0.0` on FAIL) to a 
**graduated gate**:

| Jones Verdict | Current Behavior | Proposed Behavior |
|---|---|---|
| PASS | 1.0x sizing | 1.0x sizing (unchanged) |
| FAIL (VIX > 35) | 0.0x — NO GO | 0.0x — NO GO (unchanged) |
| FAIL (other reasons) | 0.0x — NO GO | 0.25x — reduced sizing |

**Rationale:** Druckenmiller is the unconditional macro veto (FAIL = 0.0x, always). 
Jones should be a strong signal with consequence, but not a duplicate unconditional veto. 
If macro is clean (Druckenmiller PASS) and the agents agree, reducing to 25% size when 
Jones has concerns is more appropriate than zeroing everything.

**Exception preserved:** VIX > 35 (destructive regime) remains a hard 0.0x regardless.

---

## Inputs

- `jones.py` — framework analysis and prompt
- `thesis_engine.py` — `resolve_conflict()` method, lines 150-159
- `models.py` — `FrameworkAnalysis` and `GateResult` models

---

## Outputs

**Success:** On a moderate VIX day (14–22) where Druckenmiller PASSes and agents 
score qualifying concordances, THESIS returns `sizing_multiplier ≥ 0.25` instead of 0.0.

**Preserved NO-GO cases:**
- VIX > 35 → still 0.0x
- Druckenmiller FAIL → still 0.0x unconditional
- Jones FAIL + Druckenmiller FAIL → still 0.0x (double gate)
- Soft vetoes from Soros/Cohen/Buffett can still reduce sizing further

---

## Contracts

**Guarantees:**
- VIX > 35 always produces 0.0x (hard regime guard preserved)
- Druckenmiller remains the primary unconditional macro veto
- Jones FAIL still produces a warning in every report and alerts SOVEREIGN

**Does NOT guarantee:**
- That a Jones FAIL + 0.25x will produce profitable trades
- Any change to agent scoring or concordance thresholds (separate spec)

---

## Failure Modes

| Scenario | Handling |
|---|---|
| Jones FAIL still zeroed (bug) | Test: verify size_multiplier ≥ 0.25 when Druckenmiller PASS + Jones FAIL + VIX < 35 |
| Jones prompt change causes always-PASS | Test: inject VIX=40 — must return FAIL |
| Both gates FAIL | size_multiplier must remain 0.0 (Druckenmiller still hard) |

---

## Test Cases (minimum 5)

1. **VIX=16, Druckenmiller PASS, Jones FAIL (choppy market)** → `size_multiplier=0.25`, `is_go=True`
2. **VIX=38, Druckenmiller PASS, Jones FAIL (high vol)** → `size_multiplier=0.0`, `is_go=False`
3. **VIX=18, Druckenmiller FAIL, Jones PASS** → `size_multiplier=0.0`, `is_go=False` (Druckenmiller veto preserved)
4. **VIX=18, both PASS** → `size_multiplier ≥ 0.75` (soft vetoes may reduce further)
5. **VIX=16, Jones FAIL + 3 soft vetoes** → `size_multiplier=0.25` (Jones floor respected, soft vetoes do not push below Jones floor)
6. **THESIS weekly runs Sunday, Jones FAIL on VIX=20** → Daily brief inherits `sizing_multiplier=0.25`, not 0.0

---

## Files Changed

- `/Users/ahmedsadek/nexus/thesis/frameworks/jones.py` — update `_SYSTEM_PROMPT` with credit spread context
- `/Users/ahmedsadek/nexus/thesis/thesis_engine.py` — update `resolve_conflict()` to use graduated sizing on Jones FAIL

---

## Risk Assessment

**Risk: LOW-MEDIUM**

This increases THESIS permissiveness on moderate-VIX days. The guard rails remain:
- Druckenmiller hard veto preserved
- VIX > 35 hard zero preserved  
- Agent concordance thresholds unchanged (still need 2-3 agents ≥65)
- OMNI 4-brain synthesis still makes the final call before execution

This change affects sizing, not execution. OMNI can still return NO_GO on a 0.25x 
thesis day — the sizing just wouldn't be completely zeroed before OMNI even runs.

**Reversibility:** One-line revert in `thesis_engine.py`. Can be rolled back in minutes.

---

## AWAITING AHMED APPROVAL

**Ahmed — two decisions needed:**

1. **Approve Part A only** (Jones prompt update only — no code structure change)?
   - Safest option. May not fully solve the problem if Claude still interprets choppy + low VIX as FAIL.

2. **Approve Part A + Part B** (prompt update + graduated sizing)?
   - Recommended. Addresses the root issue: Jones FAIL should reduce, not eliminate.

3. **Reject — keep current behavior.**
   - Accept that moderate VIX + choppy tape = 0 trades. System stays maximally conservative.

Reply with your decision and I'll build immediately.
