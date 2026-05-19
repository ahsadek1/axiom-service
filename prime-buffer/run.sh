#!/bin/bash
# Prime Buffer launcher
cd /Users/ahmedsadek/nexus/prime-buffer || exit 1
if [ -f ".env" ]; then
  set -a
  source .env
  set +a
else
  echo "ERROR: .env file not found"
  exit 1
fi
# Use wrapper script that properly sets sys.path before uvicorn loads the module
exec .venv/bin/python run_app.py
