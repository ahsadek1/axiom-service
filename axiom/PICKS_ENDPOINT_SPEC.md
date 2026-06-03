# /picks Endpoint Specification

## Overview
The `/picks` endpoint provides a real-time feed of pending picks from screening agents, ready for OMNI synthesis.

**OMNI polls this endpoint every ~30 seconds during market hours.**

## Endpoint Details

### Request
```
GET /picks
Header: X-Nexus-Secret: <AXIOM_SECRET>
```

### Response (200 OK)
```json
{
  "pending_picks": [
    {
      "pick_id": "MOCK_SPY_20260620_CALL_500",
      "ticker": "SPY",
      "strike": 500.0,
      "dte": 14,
      "direction": "call",
      "thesis": "breakdown below 200-SMA, oversold at 2 SDs",
      "confidence": 8.5,
      "screening_agent": "ARGUS",
      "risk_flags": ["concentration_risk", "vix_elevated"],
      "risk_score": 6.2,
      "submitted_at": "2026-05-19T14:35:22Z",
      "assessed_at": "2026-05-19T14:35:45Z"
    }
  ],
  "count": 1,
  "timestamp": "2026-05-19T14:36:00Z"
}
```

### Field Definitions

| Field | Type | Description |
|-------|------|-------------|
| `pick_id` | string | Unique identifier (candidate_id format: MOCK_{SYMBOL}_{EXPDATE}_{TYPE}_{STRIKE}) |
| `ticker` | string | Stock ticker (e.g., "SPY", "AAPL") |
| `strike` | float | Option strike price |
| `dte` | int \| null | Days to expiration (null if not yet computed) |
| `direction` | string | "call" or "put" |
| `thesis` | string | Trade thesis from screening agent |
| `confidence` | float | Screening agent confidence (0–10 scale) |
| `screening_agent` | string | Source agent: "ARGUS" (Cipher), "APEX" (Sage), "TRIDENT" (Atlas) |
| `risk_flags` | string[] | Axiom risk flags (e.g., ["earnings_in_7_days", "concentration_risk"]) |
| `risk_score` | float \| null | Axiom risk score (0–10, null if not yet assessed) |
| `submitted_at` | string | ISO timestamp when pick was submitted to Axiom |
| `assessed_at` | string \| null | ISO timestamp when Axiom completed risk assessment |

### Response Codes
- **200 OK** — Success; returns array of pending picks (may be empty)
- **403 Forbidden** — Missing/invalid X-Nexus-Secret header
- **500 Internal Server Error** — Service error; check logs

## Contract

1. **Queue Management**
   - Returns all pending picks awaiting OMNI synthesis
   - Does NOT remove picks from queue (executor handles removal after execution)
   - Picks remain visible to OMNI until marked "executed" or "skipped"

2. **Assessment Status**
   - `risk_score` and `assessed_at` may be null if Axiom has not yet completed assessment
   - OMNI should NOT wait for assessment to complete — use available data

3. **Polling Interval**
   - OMNI polls every ~30 seconds during market hours (9:30 AM – 4:00 PM ET)
   - Empty array is normal (no pending picks)

4. **Authorization**
   - Requires valid `X-Nexus-Secret` header
   - Must match `AXIOM_SECRET` environment variable on Axiom service

## Integration Example (Python)

```python
import requests
from datetime import datetime

AXIOM_URL = "https://axiom-production-334c.up.railway.app"
AXIOM_SECRET = "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2"

def fetch_pending_picks():
    """Poll Axiom for pending picks."""
    headers = {
        "X-Nexus-Secret": AXIOM_SECRET,
    }
    
    resp = requests.get(
        f"{AXIOM_URL}/picks",
        headers=headers,
        timeout=5
    )
    
    if resp.status_code == 200:
        data = resp.json()
        picks = data.get("pending_picks", [])
        print(f"📊 {data['count']} pending picks")
        for pick in picks:
            print(f"  - {pick['ticker']} {pick['strike']} {pick['direction'].upper()} "
                  f"(DTE={pick['dte']}, risk={pick['risk_score']}/10)")
        return picks
    else:
        print(f"❌ Error: {resp.status_code}")
        return []

# Usage
if __name__ == "__main__":
    picks = fetch_pending_picks()
```

## Deployment Status

- **Service URL:** https://axiom-production-334c.up.railway.app
- **Endpoint:** GET /picks
- **Auth Header:** X-Nexus-Secret
- **Deployed:** May 19, 2026
- **Version:** 4.0.0

## Next Steps (OMNI Implementation)

1. **Polling Loop**
   ```python
   while market_hours:
       picks = fetch_pending_picks()  # Every 30s
       if picks:
           verdicts = await omni_synthesize(picks)  # Spawn sub-agents
           await executor_submit(verdicts)
       await asyncio.sleep(30)
   ```

2. **Deduplication**
   - Track `pick_id` to detect duplicate submissions from multiple agents

3. **Verdict Mapping**
   - Use `pick_id` in GO verdict to trace back to original pick
   - Enables executor to correlate verdict → execution

4. **Error Handling**
   - Retry on 500/timeout (exponential backoff)
   - Alert Ahmed if Axiom endpoint unreachable for >5 min

## Performance Notes

- Response time: < 50ms (in-memory)
- Queue size: typically 0–5 picks during normal trading
- Auth validation: < 1ms
- Max picks returned: theoretically unlimited (depends on MAX_POSITIONS config, typically 3–10)

---

**Author:** Axiom  
**Date:** May 19, 2026  
**Status:** Live (v4.0.0)
