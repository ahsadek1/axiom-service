# Nexus Dress Rehearsal

Mock trading framework that runs 10 independent scenarios against all Nexus V2 services and sends a formatted report to Telegram.

## Setup

```bash
cd /Users/ahmedsadek/nexus/rehearsal
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
source .venv/bin/activate
python rehearsal.py
```

## Services Required (localhost)

| Port | Service       | Auth Header           |
|------|---------------|-----------------------|
| 8001 | Axiom         | X-Axiom-Secret        |
| 8002 | Alpha Buffer  | X-Nexus-Secret        |
| 8003 | Prime Buffer  | X-Nexus-Prime-Secret  |
| 8004 | OMNI          | X-Nexus-Secret        |
| 8005 | Alpha Exec    | X-Nexus-Secret        |
| 8006 | Prime Exec    | X-Nexus-Prime-Secret  |
| 8007 | ORACLE        | X-Oracle-Secret       |
| 8008 | AILS          | X-AILS-Secret         |
| 9001 | Cipher        | X-Nexus-Secret        |
| 9002 | Atlas         | X-Nexus-Secret        |
| 9003 | Sage          | X-Nexus-Secret        |

## Scenarios

| # | Name | Type |
|---|------|------|
| S1 | Clean Concordance Baseline | Positive |
| S2 | Agent Timeout (P2 Pathway) | Positive |
| S3 | Conditional Verdict / Score Gate | Block (PASS = blocked) |
| S4 | VIX Brake Infrastructure | Block (PASS = brake confirmed) |
| S5 | Primary Data Source Unavailable | Positive |
| S6 | Earnings Gate Block | Block (PASS = gate confirmed) |
| S7 | DTE Boundary Condition | Positive |
| S8 | Duplicate Order Prevention | Block (PASS = dedup enforced) |
| S9 | Position Limit Configuration | Block (PASS = limit configured) |
| S10 | Complete Exit Lifecycle | Positive |

Each scenario is isolated — an exception in one will not abort the others.
