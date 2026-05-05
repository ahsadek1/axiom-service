# MANDATE — Pipeline Sentinel

## Jurisdiction

Pipeline Sentinel is a **read-only observer**. Its job is to watch, measure, score, classify, and alert. Nothing else.

## What Pipeline Sentinel DOES

- Records pipeline hop traces submitted by other services
- Computes a 0–100 system health score from those traces
- Detects 7 failure classes and records them to its own database
- Sends Telegram alerts to Ahmed when the system is degraded
- Serves health scores and context blocks to any authorized service

## What Pipeline Sentinel DOES NOT DO

- **Never writes to any other service's database**
- **Never modifies picks, scores, or submission payloads**
- **Never calls Alpaca**
- **Never calls any AI brain (Claude, OpenAI, Gemini, DeepSeek)**
- **Never restarts other services** (that is Guardian Angel's job)
- **Never blocks the trading pipeline** — /trace always returns 200 even on DB failure

## Failure Behavior

If Pipeline Sentinel itself goes down:
- Guardian Angel v3 detects it on the next health poll
- launchd auto-restarts it
- The trading pipeline continues unaffected — services fire trace calls and silently discard failures

Pipeline Sentinel failing is **inconvenient** (we lose observability temporarily).  
Pipeline Sentinel blocking the pipeline would be **catastrophic**.  
The design prioritizes non-interference above all else.

## Context

Created: 2026-04-13  
Author: GENESIS  
Approved: Ahmed Sadek (2026-04-13)  
Port: 8008  
LaunchD: ai.nexus.pipeline-sentinel
