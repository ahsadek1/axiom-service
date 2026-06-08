#!/usr/bin/env python
"""Startup wrapper that loads .env before launching uvicorn."""
import os
import sys
from pathlib import Path

# Load .env into os.environ BEFORE importing main app
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                # Strip quotes if present
                value = value.strip('"').strip("'")
                os.environ[key] = value
                # Debug: log critical keys to stderr
                if key in ["DEEPSEEK_KEY", "DEEPSEEK_API_KEY", "POLYGON_API_KEY"]:
                    print(f"[STARTUP] {key}={'***' if value else 'NOT SET'}", file=sys.stderr, flush=True)

# Now import and run uvicorn
import uvicorn

if __name__ == "__main__":
    # Port: $2 arg > PORT env > default 8001
    port_arg = sys.argv[1] if len(sys.argv) > 1 else None
    port = int(port_arg or os.getenv("PORT", "8001"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
