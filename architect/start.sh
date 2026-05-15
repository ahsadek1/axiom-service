#!/bin/bash
# ARCHITECT service launcher — loads secrets from secure file, not plist
set -a
source /Users/ahmedsadek/nexus/.architect-secrets
set +a
exec /Users/ahmedsadek/nexus/architect/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 9014
