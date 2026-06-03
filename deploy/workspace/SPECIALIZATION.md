# SPECIALIZATION.md — DEPLOY Heuristics

1. **Env Var Inventory**: List every os.getenv() and os.environ[] call in the code. Cross-reference with secrets file and plist. Any env var in code but not in secrets = P0.

2. **Requirements Coverage**: Every `import X` where X is not stdlib must have a corresponding entry in requirements.txt. Missing = P0 (ImportError at startup).

3. **Path Existence**: Every hardcoded or env-var path (DB, logs, scripts) must either be guaranteed to exist or created by the service on startup. Non-existent path at first write = P1.

4. **Start Script Pattern**: start.sh must: (1) set -a, (2) source secrets file, (3) set +a, (4) exec the correct python/uvicorn command with correct port. Missing steps = P0/P1.

5. **Plist Completeness**: Every plist must have: Label, ProgramArguments, WorkingDirectory, StandardOutPath, StandardErrorPath, RunAtLoad=true, KeepAlive=true. Missing any = P1.

6. **Health Endpoint Truthfulness**: /health must actually check: DB connectivity (real connect), required env vars (actually read them), last operation timestamp. Returning hardcoded 200 = P1.

7. **Port Conflict Check**: Port numbers must be unique across all services in the system. Duplicate port = P0.

8. **Venv Isolation**: Each service must have its own .venv. Cross-contamination between service venvs = P2.

9. **Log Rotation**: If logs are written to files, is log rotation configured (rotating file handler or logrotate)? Unlimited log growth = P2.

10. **Rollback Path**: Is there a clear rollback procedure documented? For launchd: launchctl unload + previous start.sh. Missing rollback docs = P3.
