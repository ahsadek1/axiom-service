#!/bin/bash
# EDGE service launcher — loads secrets from secure file, not plist
set -a
source /Users/ahmedsadek/nexus/.edge-secrets
set +a
exec /Users/ahmedsadek/nexus/edge/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 9011
