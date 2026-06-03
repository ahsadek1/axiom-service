"""
Alert Broker — Layer 1 of Unified Communication Architecture
Port: 8099

Single deduplication + batching + rate-limiting gateway for ALL Nexus/SQS alerts.
Every watchdog, notifier, and service routes through here instead of calling Telegram directly.

POST /alert   → receive alert from any source
GET  /health  → broker health + stats
GET  /stats   → detailed stats
"""

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from dedup import DedupEngine, BatchEngine, AlertIn
from router import route_alert
import sys as _sys_wd
_sys_wd.path.insert(0, "/Users/ahmedsadek/nexus")
from shared.watchdog import Watchdog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("alert_broker")

ALERT_BROKER_SECRET: str = os.getenv(
    "ALERT_BROKER_SECRET",
    "ab_secret_f4e2d1c8b7a3e9f5d2c4b6a8e0f3d5c7b9a1e4f6d8c0b2a4e6f8d0c2b4a6e8"
)

START_T = time.time()

# ---------------------------------------------------------------------------
# Core engines (module-level so lifespan can init them)
# ---------------------------------------------------------------------------

dedup_engine: Optional[DedupEngine] = None
batch_engine: Optional[BatchEngine] = None


def _do_send(alerts: List[AlertIn]) -> None:
    """Called by batch engine with a list of alerts ready to send."""
    for alert in alerts:
        try:
            results = route_alert(
                source=alert.source,
                level=alert.level,
                title=alert.title,
                body=alert.body,
                targets=alert.targets,
            )
            any_ok = any(results.values())
            if any_ok and dedup_engine:
                dedup_engine.record_sent(alert.dedup_key)
                dedup_engine.increment_sent()
            logger.info(
                "alert_broker: sent [%s] '%s' → %s",
                alert.level, alert.title, results,
            )
        except Exception as exc:
            logger.error("alert_broker: _do_send crashed: %s", exc)


# ---------------------------------------------------------------------------
_nns_watchdog = Watchdog("alert-broker")

# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global dedup_engine, batch_engine
    dedup_engine = DedupEngine()
    batch_engine = BatchEngine(flush_callback=_do_send)
    logger.info("Alert Broker started on port %s", os.getenv("ALERT_BROKER_PORT", "8099"))
    _nns_watchdog.start()
    yield
    logger.info("Alert Broker shutting down")


app = FastAPI(
    title="Alert Broker",
    description="Unified dedup + batching alert gateway for Nexus/SQS",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class AlertRequest(BaseModel):
    source: str = Field(..., description="Sending process name (e.g. 'nexus-integrity')")
    level: str = Field(..., description="CRITICAL | WARNING | INFO")
    title: str = Field(..., description="One-line summary")
    body: str = Field("", description="Detail text (optional)")
    dedup_key: str = Field(..., description="Deduplication key — same key within 60s = suppressed")
    targets: List[str] = Field(
        default=["ahmed"],
        description="Targets: ahmed | nexus_health_group | sqs_group | sovereign",
    )


class AlertResponse(BaseModel):
    ok: bool
    action: str         # "queued" | "suppressed" | "sent_immediate"
    dedup_key: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

VALID_LEVELS = {"CRITICAL", "WARNING", "INFO"}
VALID_TARGETS = {"ahmed", "nexus_health_group", "sqs_group", "sovereign", "bus"}


@app.post("/alert", response_model=AlertResponse)
def receive_alert(
    req: AlertRequest,
    x_alert_secret: Optional[str] = Header(None),
):
    """
    Receive an alert from any source.

    Auth: X-Alert-Secret header required.
    CRITICAL alerts bypass batch — sent immediately.
    WARNING/INFO held 10s, batched by source.
    Duplicate dedup_key within 60s → suppressed.
    """
    if x_alert_secret != ALERT_BROKER_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    # Normalise level
    level = req.level.upper()
    if level not in VALID_LEVELS:
        level = "INFO"

    # Normalise targets
    targets = [t for t in req.targets if t in VALID_TARGETS] or ["ahmed"]

    alert = AlertIn(
        source=req.source,
        level=level,
        title=req.title,
        body=req.body,
        dedup_key=req.dedup_key,
        targets=targets,
    )

    # Dedup check
    if dedup_engine and dedup_engine.is_duplicate(alert.dedup_key):
        dedup_engine.increment_suppressed()
        logger.debug(
            "alert_broker: suppressed [%s] '%s' (dedup_key=%s)",
            level, req.title, req.dedup_key,
        )
        return AlertResponse(ok=True, action="suppressed", dedup_key=req.dedup_key)

    # Route to batch engine (CRITICAL bypasses, rest batched)
    if batch_engine:
        batch_engine.add(alert)

    action = "sent_immediate" if level == "CRITICAL" else "queued"
    return AlertResponse(ok=True, action=action, dedup_key=req.dedup_key)


@app.get("/health")
def health():
    """Broker health + today's stats."""
    stats = dedup_engine.get_stats() if dedup_engine else {"sent": 0, "suppressed": 0}
    queue_depth = batch_engine.queue_depth() if batch_engine else 0
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_T),
        "alerts_today": stats["sent"],
        "suppressed_today": stats["suppressed"],
        "queue_depth": queue_depth,
    }


@app.get("/stats")
def stats():
    """Detailed dedup stats."""
    s = dedup_engine.get_stats() if dedup_engine else {"sent": 0, "suppressed": 0}
    total = s["sent"] + s["suppressed"]
    suppression_rate = round(s["suppressed"] / total * 100, 1) if total > 0 else 0.0
    return {
        **s,
        "total_received": total,
        "suppression_rate_pct": suppression_rate,
        "queue_depth": batch_engine.queue_depth() if batch_engine else 0,
        "uptime_seconds": int(time.time() - START_T),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("ALERT_BROKER_PORT", "8099"))
    logger.info("Starting Alert Broker on port %d", port)
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="warning", access_log=False)
