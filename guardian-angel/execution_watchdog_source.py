"""
Execution Watchdog — Temporary bridge until reconciler_v2 is deployed on Railway.
Checks execution_paused every 5 min. If paused, resumes automatically.
Runs as a background process on the Mac mini.
Will be removed once reconciler_v2 is live on Railway.
"""
import time, requests, datetime

ALPHA_URL    = "https://worker-production-2060.up.railway.app"
NEXUS_SECRET = "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2"
HEADERS      = {"X-Nexus-Secret": NEXUS_SECRET, "Content-Type": "application/json"}
CHECK_EVERY  = 300  # 5 min

def is_paused():
    try:
        r = requests.get(f"{ALPHA_URL}/admin/reconciler-status", headers=HEADERS, timeout=8)
        return r.status_code == 200 and r.json().get("execution_paused", False)
    except:
        return False

def resume():
    try:
        r = requests.post(f"{ALPHA_URL}/admin/resume-execution", headers=HEADERS,
                          json={"reason": "watchdog_auto_resume"}, timeout=8)
        return r.status_code == 200
    except:
        return False

print(f"[Watchdog] Started — checking every {CHECK_EVERY}s")
while True:
    if is_paused():
        ok = resume()
        ts = datetime.datetime.now().strftime("%H:%M")
        print(f"[Watchdog] {ts} — paused detected, resumed: {ok}")
    time.sleep(CHECK_EVERY)
