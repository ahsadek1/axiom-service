# nexus/shared/resilience — Shared resilience primitives
# Spec: NEXUS_RESILIENCE_BASE v1.1 (Cipher, 2026-05-02)
# DB Decision: Option A (Ahmed Sadek, 2026-05-02)
#
# Module load order (respect for migration):
#   1. contracts  — zero-dep, deploy first
#   2. alerts     — replaces scattered Telegram bot calls
#   3. state      — FreshValue thread-safe cache
#   4. db         — begin_immediate + idempotency (execution tables ONLY)
#   5. health     — HealthReport standard schema
#   6. scheduler  — stateless APScheduler scan wrapper

from .contracts import (
    DataContractError,
    require_float,
    require_int,
    require_str,
)
from .alerts import (
    alert_ahmed,
    cooldown_remaining,
    reset_cooldown,
    reset_cooldowns,
)
from .state import (
    FreshValue,
    StaleStateError,
)
from .db import (
    ScanResult,
    begin_immediate,
    idempotent_insert,
)
from .health import (
    HealthReport,
    HealthStatus,
    SourceHealth,
)
from .scheduler import build_scanner

__all__ = [
    # contracts
    "DataContractError",
    "require_float",
    "require_int",
    "require_str",
    # alerts
    "alert_ahmed",
    "cooldown_remaining",
    "reset_cooldown",
    "reset_cooldowns",
    # state
    "FreshValue",
    "StaleStateError",
    # db
    "ScanResult",
    "begin_immediate",
    "idempotent_insert",
    # health
    "HealthReport",
    "HealthStatus",
    "SourceHealth",
    # scheduler
    "build_scanner",
]
