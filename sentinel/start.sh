#!/bin/bash
set -a
source /Users/ahmedsadek/nexus/sentinel/.env
set +a
exec /Users/ahmedsadek/nexus/sentinel/.venv/bin/python sentinel.py
