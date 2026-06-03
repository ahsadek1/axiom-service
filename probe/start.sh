#!/bin/bash
# PROBE service launcher — loads secrets from secure file, not plist
set -a
source /Users/ahmedsadek/nexus/.probe-secrets
set +a
exec /Users/ahmedsadek/nexus/probe/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 9010
