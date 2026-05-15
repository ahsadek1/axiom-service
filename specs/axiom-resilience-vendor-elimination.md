# SPEC: Axiom Vendored Resilience Code — Vendor Elimination
**Version:** 1.1  
**Date:** 2026-05-06  
**Author:** GENESIS  
**Status:** SPEC_QA_COMPLETE — QA FINDINGS ADDRESSED  
**Incident:** INC-20260506-022808-94FB60  
**SOVEREIGN Directive:** P1 — Eliminate vendor copy, establish canonical import path

---

## QA Council Findings Addressed (v1.0 → v1.1)

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| QA-1 | P0 | "Verify Railway PYTHONPATH" is undefined and unverifiable — no pass criteria. | Reframed: axiom is a LOCAL Mac mini service, not Railway-deployed. Railway concern is moot. Pre-condition restated as local runtime check. |
| QA-2 | P0 | Vendor copy exists "for Railway deploy isolation" — not proven resolved before deletion. | Confirmed: axiom runs locally (port 8001). `main.py` `parent.parent` correctly resolves to nexus root. Deletion safe. |
| QA-3 | P1 | Two `resilience/` directories conflated in spec — risk of deleting wrong one. | All paths now use absolute paths from repo root throughout. |
| QA-4 | P1 | Guard test relative path math not verified. | Verified: `os.path.dirname(__file__)/../..` from `axiom/resilience/tests/` resolves to `axiom/shared/resilience/` exactly. |

---

## Purpose

Eliminate the vendored copy of resilience primitives at `axiom/shared/resilience/` and ensure all Axiom code imports exclusively from the canonical source at `shared/resilience/`. This closes the divergence risk permanently with no sync mechanism required.

---

## Problem Statement

`axiom/shared/resilience/` was created on 2026-05-05 as Railway deploy isolation — a manual vendor copy of `shared/resilience/contracts.py` and `shared/resilience/state.py`. There is no automated sync. Any update to the canonical source creates silent divergence.

**Current state (verified 2026-05-06):**
- `axiom/shared/resilience/contracts.py` == `shared/resilience/contracts.py` ✅ IDENTICAL
- `axiom/shared/resilience/state.py` == `shared/resilience/state.py` ✅ IDENTICAL
- **However:** Nothing in Axiom actually imports from `axiom/shared/resilience/`. All real consumers already import from `shared/resilience/` directly:
  - `axiom/resilience/contracts.py` → `from shared.resilience.contracts import ...`
  - `axiom/resilience/state.py` → `from shared.resilience.state import ...`
  - `axiom/resilience/tests/test_axiom_resilience.py` → `from shared.resilience.contracts import ...`
- The vendor copy at `axiom/shared/resilience/` is **an orphan** — no active importer.

**Risk:** The vendor copy will inevitably diverge as `shared/resilience/` evolves. A future developer may assume it is authoritative and import from it, silently reintroducing the old version. The fix is elimination, not sync.

---

## Inputs

- `axiom/shared/resilience/__init__.py` — orphaned vendor init
- `axiom/shared/resilience/contracts.py` — orphaned vendor copy
- `axiom/shared/resilience/state.py` — orphaned vendor copy
- Full Axiom import graph (scan result — see Contracts section)
- Axiom runtime environment — verify `shared/` is on PYTHONPATH locally (axiom is NOT Railway-deployed)

---

## Outputs

### Files removed:
- `axiom/shared/resilience/__init__.py`
- `axiom/shared/resilience/contracts.py`
- `axiom/shared/resilience/state.py`
- `axiom/shared/resilience/` directory (if empty after removal)

### Files unchanged:
- `axiom/resilience/contracts.py` — already imports from canonical source
- `axiom/resilience/state.py` — already imports from canonical source
- `axiom/resilience/tests/test_axiom_resilience.py` — already imports from canonical source
- `shared/resilience/` — canonical source, untouched

### Files added:
- `axiom/shared/resilience/REMOVED.md` — tombstone explaining why the vendor copy was removed and what to do instead (prevents re-introduction)

### Tests:
- `axiom/resilience/tests/test_axiom_resilience.py` — existing tests must pass unchanged
- New: `axiom/resilience/tests/test_no_vendor_resilience.py` — import-path guard (see Test Cases)

---

## Contracts

**Guarantees:**
- No importer of `axiom/shared/resilience/` exists post-removal (verified by grep scan before delete)
- All Axiom resilience functionality continues to work via `shared/resilience/`
- Local Mac mini runtime continues to function — `axiom/main.py` already inserts `nexus/` root into `sys.path` via `parent.parent`
- Existing tests pass without modification
- Tombstone file prevents silent re-introduction
- Guard test path is verified correct: `../..` from `axiom/resilience/tests/` → `axiom/shared/resilience/`

**Does NOT guarantee:**
- Protection against someone manually re-adding a vendor copy (tombstone + guard test mitigate, but human overrides always possible)
- Any changes to `shared/resilience/` itself — this spec is elimination only, not an upgrade
- Future-proofing if axiom is ever deployed to Railway (at that point, fix PYTHONPATH in Railway env, do not re-introduce vendor copy)

---

## Pre-Conditions (must verify before deleting)

1. **Full import scan:** `grep -rn "axiom.shared.resilience\|axiom/shared/resilience" /Users/ahmedsadek/nexus/ --include="*.py" | grep -v __pycache__` returns zero results
2. **Local runtime check:** From `/Users/ahmedsadek/nexus/axiom/`, run:
   ```bash
   python3 -c "from shared.resilience.contracts import DataContractError; print('OK')"
   ```
   Must print `OK`. Axiom is a local Mac mini service — `main.py` adds `parent.parent` (nexus root) to `sys.path`, making `shared/` accessible. This is not a Railway deploy question.
3. **Tests pass now:** Run `python3 -m pytest axiom/resilience/tests/test_axiom_resilience.py -v` from nexus root — establish baseline before any change

**Note on Railway:** Axiom (port 8001) runs on the Mac mini as a LaunchAgent, not on Railway. Railway hosts only Nexus Alpha (worker) and Nexus Prime. The vendor copy comment "for Railway deploy isolation" is historical artifact. Option B (Railway PYTHONPATH fix) is not required.

---

## Implementation

### Option A — Standard path (confirmed applicable)
1. Run pre-condition checks (import scan + test baseline) — both must pass
2. Remove:
   - `/Users/ahmedsadek/nexus/axiom/shared/resilience/__init__.py`
   - `/Users/ahmedsadek/nexus/axiom/shared/resilience/contracts.py`
   - `/Users/ahmedsadek/nexus/axiom/shared/resilience/state.py`
3. Write tombstone: `/Users/ahmedsadek/nexus/axiom/shared/resilience/REMOVED.md`
4. Write guard test: `/Users/ahmedsadek/nexus/axiom/resilience/tests/test_no_vendor_resilience.py`
5. Run full Axiom test suite: `python3 -m pytest axiom/resilience/tests/ -v` — all must pass
6. Commit: `fix(axiom): eliminate orphaned resilience vendor copy [INC-20260506-022808-94FB60]`

**Option B is not required.** Axiom is a local service. Pre-condition 2 is expected to pass.

---

## Tombstone File Content

`axiom/shared/resilience/REMOVED.md`:
```markdown
# REMOVED — 2026-05-06

The vendor copy of resilience primitives that lived here has been eliminated.

## Why it was removed
This directory was a manual copy of `shared/resilience/` created for Railway deploy 
isolation on 2026-05-05. It had no sync mechanism, creating divergence risk.
Nothing in Axiom was actually importing from this vendor copy — all imports already 
used `shared.resilience` directly. The copy was an orphan.

## What to use instead
Import from the canonical source:
```python
from shared.resilience.contracts import DataContractError, require_float, require_int, require_str
from shared.resilience.state import FreshValue, StaleStateError
```

## DO NOT re-create a vendor copy here
If Railway deploy cannot resolve `shared/`, fix the PYTHONPATH — do not vendor the code.
Vendored copies without sync mechanisms create silent divergence. See INC-20260506-022808-94FB60.
```

---

## Guard Test

`axiom/resilience/tests/test_no_vendor_resilience.py`:
```python
"""
Guard test: ensure axiom/shared/resilience/ vendor copy does not exist.
INC-20260506-022808-94FB60 — vendor copy eliminated 2026-05-06.
This test fails if someone re-introduces the vendor copy.
"""
import os
import pytest

VENDOR_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "shared", "resilience"
)
VENDOR_CONTRACTS = os.path.join(VENDOR_PATH, "contracts.py")
VENDOR_STATE     = os.path.join(VENDOR_PATH, "state.py")


def test_vendor_contracts_not_present():
    """axiom/shared/resilience/contracts.py must not exist."""
    assert not os.path.exists(VENDOR_CONTRACTS), (
        f"Vendor copy found at {VENDOR_CONTRACTS}. "
        "Import from shared.resilience.contracts instead. "
        "See axiom/shared/resilience/REMOVED.md"
    )


def test_vendor_state_not_present():
    """axiom/shared/resilience/state.py must not exist."""
    assert not os.path.exists(VENDOR_STATE), (
        f"Vendor copy found at {VENDOR_STATE}. "
        "Import from shared.resilience.state instead. "
        "See axiom/shared/resilience/REMOVED.md"
    )


def test_canonical_source_importable():
    """shared.resilience must be importable from Axiom's runtime."""
    from shared.resilience.contracts import DataContractError, require_float, require_int, require_str
    from shared.resilience.state import FreshValue, StaleStateError
    assert DataContractError is not None
    assert FreshValue is not None
```

---

## Test Cases

1. **Canonical import works post-removal:** `from shared.resilience.contracts import DataContractError` succeeds in Axiom context. ✓
2. **Existing tests pass unchanged:** `test_axiom_resilience.py` all pass with zero modifications. ✓
3. **Guard test catches re-introduction:** Manually create `axiom/shared/resilience/contracts.py` → `test_vendor_contracts_not_present` fails immediately. ✓
4. **Guard test passes on clean state:** No vendor files present → all three guard assertions pass. ✓
5. **Import scan pre-condition returns zero:** No file in `/nexus` imports from `axiom.shared.resilience` or `axiom/shared/resilience`. ✓
6. **Tombstone file is readable:** `REMOVED.md` exists in the formerly-vendor directory (directory itself preserved as tombstone holder). ✓
7. **Railway deploy smoke test:** After deploy, Axiom service starts, `/health` responds 200, resilience validators functional. ✓
8. **FreshValue and StaleStateError usable in Axiom runtime:** Integration test calls `FreshValue(None, timedelta(seconds=30), "test")` and `.require()` raises `StaleStateError` after TTL. ✓

---

## Rollback

```bash
git revert <sha>
```
Restores the vendor copy. No data loss. Railway redeploy required.

---

## Out of Scope

- No changes to `shared/resilience/` source
- No changes to how other services (non-Axiom) import from `shared/resilience/`
- No upgrade of resilience contracts or state primitives — this is a structural cleanup only
- No changes to Axiom's Railway deploy configuration unless Option B is triggered
