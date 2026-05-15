#!/bin/bash
cd /Users/ahmedsadek/nexus
set -a
source .env
set +a
cd omni
exec /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python -m uvicorn main:app --host 0.0.0.0 --port 8060 --log-level info
