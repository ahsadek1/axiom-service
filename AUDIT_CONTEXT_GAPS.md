# AUDIT_CONTEXT_GAPS.md
### Outstanding [VERIFY] and [AHMED TO CONFIRM] Items — v1.1

Updated 2026-04-12 after GENESIS code review resolved 10 of the original 22 items.
Priority: 🔴 HIGH | 🟡 MEDIUM | 🟢 LOW

---

## RESOLVED — Closed by GENESIS Code Review

| # | Original Tag | Resolution |
|---|-------------|-----------|
| V1 | VIX thresholds | ✅ Full table documented. VIX < 12 / <20 / <25 / <30 / <35 / ≥35. v4 composite thresholds also documented. |
| V2 | Buffer window timeout | ✅ 15-minute rotating windows. No explicit timeout — ID changes naturally at clock boundary. |
| V4 | OMNI on ORACLE partial failure | ✅ Architecture clarified: OMNI does NOT call ORACLE. Agents call ORACLE. OMNI gets only concordance + Axiom data. |
| V5 | SpotGamma auth method | ✅ No public API exists. Explicitly documented in `spotgamma_client.py`. Polygon math fallback always active. |
| V8 | Telegram command sender validation | ✅ N/A — no service has inbound Telegram endpoints. All Telegram is outbound-only. |
| V9 | Axiom timeout fail-safe | ✅ **CONFIRMED BUG — FIXED.** Was fail-open (trades executed without regime check). Now fail-safe. Incident 9 documented. |
| V10 | .gitignore | ✅ `.env` in `.gitignore` — credentials protected from git commits. |
| V6 | OMNI app_state locking | ✅ **CONFIRMED BUG — FIXED.** `_state_lock` added. Incident 10 documented. |
| V7 | Brain timeout denominator | ✅ Documented: timed-out brain = abstention (not NO_GO vote). Threshold stays at 3 absolute votes. MIN_BRAINS_REQUIRED = 3 → CONDITIONAL if fewer respond. |
| V3 (partial) | Weight sum assertion | ✅ **Gap confirmed, documented.** No startup assertion exists. Math is safe (normalization by total_weight) but semantic drift is unchecked. Auditors must verify weights unchanged. |

---

## OPEN — Still Needs Resolution

### [AHMED TO CONFIRM] — Require Ahmed's input

| # | Priority | What Is Needed | Why It Matters |
|---|----------|---------------|----------------|
| A11 | 🔴 HIGH | **Exit monitor interval decision: 15-min → 1-min for Prime Execution?** o3-mini (Pass A) flagged as CRITICAL. A -18% backstop checking every 15 minutes could be -25% before it fires on volatile equity. Awaiting explicit approval to change. | Most urgent operational risk remaining in the system. |
| A12-BOT | 🔴 HIGH | **Has Telegram bot token been revoked via @BotFather?** Token was exposed in v1.0 of this document (Incident 11) and sent to an external LLM context. Once revoked: new token must be set in all 9 service `.env` files that use `TELEGRAM_BOT_TOKEN`, then services restarted. | Exposed bot token = anyone can send messages as @GENESIS15BOT and potentially receive Ahmed's trade notifications. |
| A1 | 🟡 MEDIUM | Agent invocation flow: does Axiom push signals to agents, or do agents poll Axiom? Affects concurrency model and concordance window timing documentation. | Determines whether missed-agent scenarios are possible due to push delivery failure. |
| A3 | 🟡 MEDIUM | News feed subscription decision: Benzinga Pro or Seeking Alpha. Sage has no live news signal. Engine 8 (news) is a stub returning empty data silently. | Sage is the fundamental/sentiment agent — without news, it has no market narrative context. This is the largest signal gap. |
| A4 | 🟢 LOW | yfinance as Polygon outage fallback — approved or not? | Adds price data redundancy at zero cost. |
| A6 | 🟡 MEDIUM | Exact auth methods for ORATS, Unusual Whales, Market Chameleon, Trading Economics | Needed to audit credential handling in ORACLE engines. |
| A7 | 🟢 LOW | Secret rotation policy — how often, what triggers rotation | Without a policy, compromise may go undetected. |
| A8 | 🟡 MEDIUM | Is openclaw.json write-protected since the April 10 corruption incident? | Gateway corruption disabled system for 3.5 hours. No enforcement confirmed preventing recurrence. |
| A9 | 🟢 LOW | Exact library versions from each service's requirements.txt | Needed for security CVE checks. Version-specific behavioral differences. |
| A10 | 🟡 MEDIUM | SQLite backup strategy for position/synthesis/AILS databases | Local disk only. Disk failure = permanent loss of all trade history and Bayesian learning. |
| A-brain-timeout | 🟡 MEDIUM | Exact per-brain HTTP timeout value in `omni/quad_intelligence.py` | Determines how long OMNI waits before declaring a brain unresponsive and proceeding without it. |

---

## Resolution Process

For remaining GENESIS-resolvable items: GENESIS reads code and updates document directly.

For [AHMED TO CONFIRM] items: Ahmed provides answer; GENESIS removes tag and updates document.

Next version: v1.2 — increment after A11 (exit monitor) and A12-BOT (token revocation) are resolved.

---

*AUDIT_CONTEXT_GAPS.md — v1.1 — Updated 2026-04-12 by GENESIS*
