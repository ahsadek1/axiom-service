# NEXUS TRADING SYSTEM

**Architecture:** NEXUS_MASTER_ARCHITECTURE.md  
**Build date:** April 10, 2026  
**Status:** Local development

## Services

| Service | Port | Description |
|---------|------|-------------|
| axiom | 8001 | Risk gate + live candidate pool |
| alpha-buffer | 8002 | Options concordance buffer |
| prime-buffer | 8003 | Swing concordance buffer |
| omni | 8004 | Quad Intelligence synthesis engine |
| alpha-execution | 8005 | Options execution (Alpaca) |
| prime-execution | 8006 | Swing equity execution (Alpaca) |

## Setup

```bash
cp .env.example .env
# Fill in all values in .env
```

## Run all services

```bash
# Each service in its own terminal:
cd axiom && python -m uvicorn main:app --port 8001 --reload
cd alpha-buffer && python -m uvicorn main:app --port 8002 --reload
cd prime-buffer && python -m uvicorn main:app --port 8003 --reload
cd omni && python -m uvicorn main:app --port 8004 --reload
cd alpha-execution && python -m uvicorn main:app --port 8005 --reload
cd prime-execution && python -m uvicorn main:app --port 8006 --reload
```

## Architecture
See `/Users/ahmedsadek/.openclaw/workspace-genesis/docs/NEXUS_MASTER_ARCHITECTURE.md`
