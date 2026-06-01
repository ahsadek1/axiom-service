#!/bin/bash
cd /Users/ahs/thesis
PYTHON=/Users/ahs/thesis/.venv/bin/python3
[ ! -f "$PYTHON" ] && PYTHON=/usr/bin/python3
nohup $PYTHON main.py >> thesis.log 2>> thesis.err &
echo $! > /tmp/thesis.pid
echo "THESIS started PID $!"
