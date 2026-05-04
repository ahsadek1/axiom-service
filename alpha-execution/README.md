# alpha-execution — Service README

## What this does
Receives GO verdicts from OMNI, places options trades on Alpaca paper account, monitors exits.

## Port
`8005` | Auth header: `X-Nexus-Secret`

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `NEXUS_WEBHOOK_SECRET` | Yes | — | Inbound auth for all endpoints |
| `ALPACA_API_KEY` | Yes | — | Alpaca paper account key |
| `ALPACA_SECRET_KEY` | Yes | — | Alpaca paper account secret |
| `ALPACA_PAPER` | Yes | — | Must be `true` |
| `NEXUS_AUTO_EXECUTE` | No | `false` | Set `true` to enable live order submission |
| `ANTHROPIC_API_KEY` | No | — | For OMNI quad-intelligence integration |

## MOCK_SESSION_MODE — Mock Trading Framework Override

**Purpose:** Allows the Nexus Mock Trading Framework to bypass the market hours gate
so it can exercise the real execution path on weekends/after-hours.

**How to use:**
```bash
MOCK_SESSION_MODE=true python3 -m uvicorn main:app --port 8005
```
or set in the process environment before starting the service.

**Safety constraints (non-negotiable):**
1. **Env-var only** — no hardcoded default, no config file fallback. Off unless explicitly set.
2. **Paper account only** — the service asserts `"paper" in ALPACA_BASE_URL` before bypassing. If this check fails (wrong URL), the gate is NOT bypassed and a CRITICAL log fires.
3. **Every bypass logs a WARNING** — visible in stderr.log, auditable.
4. **Never set this in production** — there is no live-capital scenario where this flag should be active.

**Where it applies:** `_is_market_hours()` in `main.py` — called at the `/execute` endpoint and the exit monitor loop.

## Logs
- stdout: `/Users/ahmedsadek/nexus/logs/alpha-execution/stdout.log`
- stderr: `/Users/ahmedsadek/nexus/logs/alpha-execution/stderr.log`

## LaunchAgent
`~/Library/LaunchAgents/ai.nexus.alpha-execution.plist`
