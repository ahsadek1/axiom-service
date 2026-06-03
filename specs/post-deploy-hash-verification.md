# Spec: Post-Deploy Hash Verification
**Drafted:** 2026-05-01 by GENESIS  
**For:** VECTOR (implementation alongside P0-A work)  
**Priority:** P2-C

---

## Purpose
Verify that what was deployed to production matches exactly what was tested and approved. Prevent silent divergence between tested code and running code — the class of failure where a service runs the wrong version without anyone knowing.

---

## Problem Statement
Today's incident included an `alpaca_client.py` signature mismatch — SOVEREIGN had overwritten the file, the running process loaded the old version, and the mismatch went undetected for hours. A post-deploy hash check would have caught this at deploy time, not at 9:37 AM when the first trade attempt failed.

---

## Inputs
- `service_name`: string — the service being verified (e.g. "alpha-execution")
- `deploy_commit_sha`: string — the git commit SHA that was deployed
- `service_root`: string — local path to the service directory
- `critical_files`: list[str] — files whose hashes must match exactly (e.g. `["alpaca_client.py", "main.py", "config.py"]`)

---

## Outputs
**Success:**
```json
{
  "verified": true,
  "service": "alpha-execution",
  "commit": "abc1234",
  "files_checked": 3,
  "timestamp": "2026-05-01T16:49:00-04:00"
}
```

**Failure:**
```json
{
  "verified": false,
  "service": "alpha-execution",
  "commit": "abc1234",
  "mismatches": [
    {
      "file": "alpaca_client.py",
      "expected_sha256": "a1b2c3...",
      "actual_sha256": "d4e5f6...",
      "detail": "File on disk does not match deployed commit"
    }
  ],
  "timestamp": "2026-05-01T16:49:00-04:00"
}
```

---

## Contracts
**Guarantees:**
- If `verified=true`, every `critical_file` on disk matches the deployed commit byte-for-byte
- If `verified=false`, the exact mismatching files are identified with both expected and actual hashes
- Runs in < 5 seconds for up to 20 files

**Does NOT guarantee:**
- That the running process has reloaded the files (process must be restarted separately)
- That non-critical files match (only `critical_files` list is checked)
- Correctness of the code — only identity verification

---

## Failure Modes
| Condition | Behavior |
|---|---|
| File missing from disk | `verified=false`, detail: "file not found" |
| Git not available | Fall back to stored hash from deploy manifest |
| Deploy manifest missing | Fail with `verified=false`, reason: "no manifest" |
| Network timeout on remote git | Use local git only, skip remote check |

---

## Implementation Notes for Vector

### Step 1: At deploy time, generate a manifest
```python
# shared/deploy_manifest.py
import hashlib, json, os, datetime

def generate_manifest(service_name: str, service_root: str, critical_files: list) -> dict:
    manifest = {
        "service": service_name,
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "files": {}
    }
    for fname in critical_files:
        fpath = os.path.join(service_root, fname)
        if os.path.exists(fpath):
            sha256 = hashlib.sha256(open(fpath, "rb").read()).hexdigest()
            manifest["files"][fname] = sha256
    return manifest

def save_manifest(service_name: str, manifest: dict) -> None:
    path = f"/Users/ahmedsadek/nexus/data/deploy_manifests/{service_name}.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(manifest, open(path, "w"), indent=2)
```

### Step 2: At verification time, compare against manifest
```python
def verify_deployment(service_name: str, service_root: str) -> dict:
    manifest_path = f"/Users/ahmedsadek/nexus/data/deploy_manifests/{service_name}.json"
    if not os.path.exists(manifest_path):
        return {"verified": False, "reason": "no_manifest"}
    
    manifest = json.load(open(manifest_path))
    mismatches = []
    
    for fname, expected_hash in manifest["files"].items():
        fpath = os.path.join(service_root, fname)
        if not os.path.exists(fpath):
            mismatches.append({"file": fname, "detail": "file_not_found"})
            continue
        actual_hash = hashlib.sha256(open(fpath, "rb").read()).hexdigest()
        if actual_hash != expected_hash:
            mismatches.append({
                "file": fname,
                "expected_sha256": expected_hash,
                "actual_sha256": actual_hash,
                "detail": "hash_mismatch"
            })
    
    return {
        "verified": len(mismatches) == 0,
        "service": service_name,
        "files_checked": len(manifest["files"]),
        "mismatches": mismatches,
        "timestamp": datetime.datetime.utcnow().isoformat()
    }
```

### Step 3: Wire into deploy script and post-restart check
- Call `generate_manifest()` at end of every deploy
- Call `verify_deployment()` after every service restart
- If `verified=false`: alert immediately via Telegram + block trading on that service

---

## Test Cases
1. **Happy path**: all files match manifest → `verified=true`
2. **File modified after deploy**: one file changed → `verified=false`, mismatch listed
3. **File deleted**: critical file missing → `verified=false`, `file_not_found`
4. **No manifest**: service never had a deploy manifest → `verified=false`, `no_manifest`
5. **Multiple mismatches**: 3 files changed → all 3 listed in `mismatches`
6. **Empty critical_files list**: → `verified=true`, `files_checked=0` (vacuous truth)

---

## Services to cover (priority order)
1. `alpha-execution` — alpaca_client.py, main.py, config.py
2. `oracle` — models.py, engines/gamma_engine.py, main.py
3. `alpha-buffer` — main.py, concordance.py, config.py
4. `omni` — main.py, synthesis.py, execution_router.py
5. `axiom` — tier2_filter.py, scheduler.py, main.py

---

## Integration with CHRONICLE
Log every verification result to `chronicle.db → deploy_verifications` table:
- `service`, `verified`, `commit`, `mismatch_count`, `timestamp`
- Query: "show me all services that have had hash mismatches in the last 7 days"
