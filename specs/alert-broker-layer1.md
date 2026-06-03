# SPEC: Alert Broker — Layer 1 of Unified Communication Architecture
**Author:** GENESIS
**Date:** 2026-05-05
**Status:** PENDING AHMED APPROVAL
**Priority:** P0 — stops 100-alert floods immediately

---

## Purpose
Single deduplication + batching process on .141. Every Telegram alert from every Nexus and SQS process routes through it. Eliminates duplicate storms. Ahmed sees one alert per event, not 29.

---

## The Problem It Solves
29 independent processes currently send Telegram alerts directly:
- nexus-watchdog.sh, alert_monitor.py, primus_watchdog.py, governance_loop.py,
  service_watchdog.py, nexus-integrity/notifier.py, pipeline-sentinel/notifier.py,
  dead_mans_switch.py, graceful_degradation.py, + 20 more

Each has its own cooldown. None knows what others sent. One system event → all 29 fire → alert storm.

---

## Architecture

```
[All watchdogs / services]
        │
        ▼ POST /alert  (localhost:9998)
  ┌─────────────────────┐
  │   Alert Broker      │  ← Single process on .141
  │   port 9998         │
  │                     │
  │  Dedup window: 60s  │
  │  Batch window: 10s  │
  │  Queue: SQLite      │
  └─────────┬───────────┘
            │
     ┌──────┴──────┐
     ▼             ▼
  Telegram      Message Bus
  (Ahmed DM,   (SOVEREIGN inbox)
   groups)
```

---

## Inputs

### `POST /alert`
```json
{
  "source": "nexus-integrity",
  "level": "CRITICAL|WARNING|INFO",
  "title": "OMNI silence 25min",
  "body": "No synthesis since 10:42 AM",
  "dedup_key": "omni_silence",
  "targets": ["ahmed", "nexus_health_group"]
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| source | str | yes | Sending process name |
| level | str | yes | CRITICAL / WARNING / INFO |
| title | str | yes | One-line summary |
| body | str | no | Detail text |
| dedup_key | str | yes | Deduplication key — same key within 60s = suppressed |
| targets | list[str] | no | ["ahmed", "nexus_health_group", "sqs_group"]. Default: ["ahmed"] |

### Auth
Header: `X-Alert-Secret` = env `ALERT_BROKER_SECRET` (new shared secret, all callers use same value)

---

## Outputs

### `GET /health`
```json
{"status": "ok", "alerts_today": 42, "suppressed_today": 318, "queue_depth": 0}
```

### `POST /alert` response
```json
{"ok": true, "action": "queued|suppressed", "dedup_key": "omni_silence"}
```

---

## Core Logic

### Deduplication (60s window)
- On receive: check SQLite `dedup_log` for same `dedup_key` sent within last 60s
- If found: increment suppressed counter, return `action: suppressed` — NO telegram send
- If not found: add to send queue

### Batching (10s window, CRITICAL bypasses)
- CRITICAL alerts: flush immediately (no batch delay)
- WARNING / INFO: hold for 10s, combine same-source alerts into one message
- Batch format: `[nexus-integrity] 3 warnings: OMNI silence | Alpha paused | TRS AMBER`

### Routing
| Target | Destination |
|---|---|
| ahmed | Telegram DM to 8573754783 |
| nexus_health_group | Telegram group -5241272802 |
| sqs_group | Telegram group -5130564161 |
| sovereign | Message bus POST to SOVEREIGN inbox |

### Rate limiting (hard cap)
- Max 10 Telegram sends per 5 minutes to any single target
- If cap hit: buffer remaining, flush when cap clears
- Always log suppression count at end of burst

---

## What Changes in Existing Code

Each existing alert sender gets a one-line wrapper replacement:

**Before** (scattered across 20+ files):
```python
requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={...})
```

**After**:
```python
# sqs/shared/alert_client.py OR nexus/shared/alert_client.py
alert_client.send(source="nexus-integrity", level="CRITICAL", title="...", body="...", dedup_key="...")
```

`alert_client.py` is a 30-line drop-in that POSTs to `http://localhost:9998/alert`. Falls back to direct Telegram if broker is unreachable (fail-safe — never lose a CRITICAL).

---

## Files

```
/Users/ahmedsadek/nexus/alert-broker/main.py          ← New FastAPI service (port 9998)
/Users/ahmedsadek/nexus/alert-broker/dedup.py         ← Dedup + batch logic
/Users/ahmedsadek/nexus/alert-broker/router.py        ← Target routing
/Users/ahmedsadek/nexus/alert-broker/requirements.txt
/Users/ahmedsadek/nexus/shared/alert_client.py        ← Drop-in for Nexus callers
/Users/ahmedsadek/sqs/shared/alert_client.py          ← Drop-in for SQS callers
~/Library/LaunchAgents/ai.nexus.alert-broker.plist    ← LaunchAgent
/Users/ahmedsadek/nexus/alert-broker/tests/test_alert_broker.py
```

---

## Failure Modes

| Failure | Behavior |
|---|---|
| Broker unreachable | alert_client.py falls back to direct Telegram — never silent |
| SQLite locked | In-memory dedup for current batch, log warning |
| Telegram API down | Queue to SQLite, retry every 30s |
| Rate limit hit (429) | Respect Retry-After, queue remainder |
| Broker crash | LaunchAgent restarts within 5s |

---

## Test Cases (minimum 8)

1. `test_dedup_suppresses_within_60s` — same dedup_key twice in 30s → second suppressed
2. `test_dedup_allows_after_window` — same key at 0s and 65s → both sent
3. `test_critical_bypasses_batch` — CRITICAL alert sent immediately, not held 10s
4. `test_warning_batched` — 3 warnings in 5s → one batched message
5. `test_rate_limit_hard_cap` — 11 alerts in 2 min → 10 sent, 1 queued
6. `test_auth_required` — missing X-Alert-Secret → 403
7. `test_fallback_on_broker_down` — alert_client with broker 503 → direct Telegram called
8. `test_health_endpoint` — GET /health → alerts_today + suppressed_today accurate

---

## Contracts

- Broker NEVER drops a CRITICAL. If all delivery paths fail, writes to local file `/tmp/alert_broker_emergency.log`
- Broker NEVER blocks callers — POST /alert returns in <50ms regardless of send state
- alert_client.py NEVER raises — catches all exceptions, logs at WARNING

---

## Env Vars
```
ALERT_BROKER_SECRET=<new shared secret>
ALERT_BROKER_PORT=9998
GENESIS_BOT_TOKEN=7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c
AHMED_CHAT_ID=8573754783
NEXUS_HEALTH_GROUP_ID=-5241272802
SQS_GROUP_ID=-5130564161
```

---

## Layer 2, 3, 4 Dependencies
This spec is self-contained. Layers 2-4 are separate specs.
Layer 3 (Agent DMs) will use the broker's routing table to add per-agent bot targets.
Layer 4 (Command Relay) will add an inbound command endpoint to the broker process.

---

## Out of Scope
- Modifying existing watchdog logic beyond the alert_client.py swap
- SOVEREIGN bus polling (Layer 2)
- Agent-to-agent DM bots (Layer 3)
- Command relay / ACK (Layer 4)
