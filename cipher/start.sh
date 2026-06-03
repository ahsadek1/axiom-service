#!/bin/bash
# Cipher service launcher — loads secrets from secure file, not plist
set -a
source /Users/ahmedsadek/nexus/.cipher-secrets
set +a
exec /Users/ahmedsadek/nexus/cipher/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 9001
