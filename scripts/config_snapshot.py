#!/usr/bin/env python3
"""
config_snapshot.py — Nightly config drift detector.
Takes a snapshot of all service .env files and compares against known-good baseline.
Alerts Ahmed + SOVEREIGN if any drift detected.
"""
import os, json, hashlib, datetime, requests

SERVICES = {
    "alpha-execution": "/Users/ahmedsadek/nexus/alpha-execution/.env",
    "prime-execution": "/Users/ahmedsadek/nexus/prime-execution/.env",
    "omni":            "/Users/ahmedsadek/nexus/omni/.env",
    "axiom":           "/Users/ahmedsadek/nexus/axiom/.env",
    "alpha-buffer":    "/Users/ahmedsadek/nexus/alpha-buffer/.env",
    "prime-buffer":    "/Users/ahmedsadek/nexus/prime-buffer/.env",
    "ails":            "/Users/ahmedsadek/nexus/ails/.env",
    "oracle":          "/Users/ahmedsadek/nexus/oracle/.env",
}

SNAPSHOT_DIR = "/Users/ahmedsadek/nexus/data/config_snapshots"
BASELINE_FILE = f"{SNAPSHOT_DIR}/baseline.json"
def _require(var: str) -> str:
    """Return env var value or raise at startup — never silently use a stale fallback."""
    val = os.getenv(var)
    if not val:
        raise RuntimeError(f"{var} is required but not set. Source .deploy-secrets before running.")
    return val

TELEGRAM_TOKEN = _require("TELEGRAM_BOT_TOKEN")
AHMED_CHAT_ID  = os.getenv("AHMED_CHAT_ID", "8573754783")

os.makedirs(SNAPSHOT_DIR, exist_ok=True)

def hash_file(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None

def get_keys(path):
    """Return set of env var keys (not values) from .env file."""
    keys = set()
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    keys.add(line.split("=")[0].strip())
    except:
        pass
    return keys

now = datetime.datetime.now().isoformat()
current = {}
for svc, path in SERVICES.items():
    current[svc] = {
        "hash": hash_file(path),
        "keys": list(get_keys(path)),
        "exists": os.path.exists(path)
    }

# Save today's snapshot
today = datetime.date.today().isoformat()
snapshot_path = f"{SNAPSHOT_DIR}/snapshot_{today}.json"
with open(snapshot_path, "w") as f:
    json.dump({"timestamp": now, "services": current}, f, indent=2)

# Compare against baseline
drifts = []
if os.path.exists(BASELINE_FILE):
    with open(BASELINE_FILE) as f:
        baseline = json.load(f)
    for svc, data in current.items():
        b = baseline.get("services", {}).get(svc, {})
        if b.get("hash") and data.get("hash") and b["hash"] != data["hash"]:
            # Check what keys changed
            old_keys = set(b.get("keys", []))
            new_keys = set(data.get("keys", []))
            added = new_keys - old_keys
            removed = old_keys - new_keys
            drifts.append({
                "service": svc,
                "added_keys": list(added),
                "removed_keys": list(removed),
                "hash_changed": True
            })
        elif b.get("exists") and not data.get("exists"):
            drifts.append({"service": svc, "issue": "ENV FILE MISSING"})
else:
    # First run — save as baseline
    with open(BASELINE_FILE, "w") as f:
        json.dump({"timestamp": now, "services": current}, f, indent=2)
    print(f"Baseline created at {BASELINE_FILE}")

if drifts:
    msg = f"⚠️ <b>CONFIG DRIFT DETECTED</b>\n{now[:16]}\n\n"
    for d in drifts:
        msg += f"Service: <b>{d['service']}</b>\n"
        if d.get("added_keys"): msg += f"  Added: {d['added_keys']}\n"
        if d.get("removed_keys"): msg += f"  Removed: {d['removed_keys']}\n"
        if d.get("issue"): msg += f"  Issue: {d['issue']}\n"
        msg += "\n"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": AHMED_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except:
        pass
    print(f"DRIFT DETECTED: {len(drifts)} services changed")
    for d in drifts:
        print(f"  {d}")
else:
    print(f"NO DRIFT — all {len(current)} service configs match baseline")
