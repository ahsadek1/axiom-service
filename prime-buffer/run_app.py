#!/usr/bin/env python3
"""
Wrapper script that sets sys.path and imports the app before running uvicorn
"""
import sys
sys.path.insert(0, "/Users/ahmedsadek/nexus")

# Import the app directly so uvicorn doesn't have to parse the string
from prime_buffer.main import app

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8003,
        log_level="info"
    )
