# CIPHER ADDENDUM — nexus-integrity v2.1 + Sovereign Addendum
**Author:** Cipher 🔐
**Date:** 2026-04-30
**Basis:** commercial-monitoring-system-v2.md (v2.1) + SOVEREIGN_ADDENDUM_V2_1.md
**Status:** APPROVED — Ahmed Sadek (2026-04-30)
**Build order:** C1 → C6 → C3 → C2 → C7 → C5 → C4

## C1 — Dead Man's Switch (Monitoring the Monitor)
External 30-line process. 60s interval. GET /health on 8011. 2 failures → P0.
TRS stale >20min → SOVEREIGN treats as UNKNOWN not GREEN.

## C2 — Alert Deduplication
last_fired_at + cooldown per alert type in CHRONICLE monitoring_thresholds.
Cooldowns: P0=0, P1=20min, P2=60min, P3=240min.

## C3 — Probe Secret Rotation Detection
Startup + 8:45 AM daily: self-auth check. 401/403 → P0 + block probes.

## C4 — CHRONICLE Mid-Session Connectivity Loss
Track consecutive write failures. 3 failures → P1 to local SQLite → alert.
On reconnect → replay queue (cap 500). Continue on local cache during outage.

## C5 — Composite Score Trend Endpoint
GET /composite-health/trend — last 8 snapshots (2h) with deltas.
Distinguishes stable-AMBER from deteriorating-toward-RED.

## C6 — Alpaca Session Expiry Gate
Stage 5 dry-run: assert response confirms options trading authorization.
Not just 200 OK — verify the permission field in the response body.

## C7 — Synthesis Silence vs No-Signal Disambiguation
3-way Stage 4: LIVE / QUIET / SILENT.
QUIET = concordances forming but all below GO threshold (market, not failure).
SILENT = no concordances = investigate.
