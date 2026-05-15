"""
Command Relay — Layer 4 of Unified Communication Architecture
Port: 9997

SOVEREIGN on .42 sends a directive to the bus → this relay on .141 picks it up,
executes it against the right service, and ACKs back to SOVEREIGN via bus.

True bidirectional control: SOVEREIGN issues → relay executes → SOVEREIGN sees result.

Supported directives:
  FLEET_STATUS     → query /health on all 9 Nexus services, return summary
  SERVICE_STATUS   → query /health on one service
  HALT_SERVICE     → POST /halt to a service
  RESUME_SERVICE   → POST /resume to a service
  BROADCAST        → push a message to all agent bus inboxes
  TRIGGER_DIAGNOSTIC → run morning diagnostic, return result
  PING             → echo ACK (connectivity check)
"""

import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("command_relay")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RELAY_SECRET: str = os.getenv(
    "COMMAND_RELAY_SECRET",
    "cr_secret_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
)
SOVEREIGN_BUS_URL: str = os.getenv("SOVEREIGN_BUS_URL", "http://192.168.1.141:9999")
NEXUS_SECRET: str = os.getenv("NEXUS_WEBHOOK_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
NEXUS_HOST: str = os.getenv("NEXUS_HOST", "192.168.1.141")
CHRONICLE_URL: str = os.getenv("CHRONICLE_URL", "http://192.168.1.42:8020")
CHRONICLE_WRITE_TOKEN: str = os.getenv(
    "CHRONICLE_WRITE_TOKEN",
    "75cf9f07c2b8ef505e0cc19f6440d88a01a6678d9e81400061bd24f53bd7215d"
)

HTTP_TIMEOUT: float = 8.0
START_T = time.time()

# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------

NEXUS_SERVICES: Dict[str, Dict[str, Any]] = {
    "axiom":           {"port": 8001, "auth_header": "X-Axiom-Secret",       "auth_value": NEXUS_SECRET},
    "alpha-buffer":    {"port": 8002, "auth_header": "X-Nexus-Secret",       "auth_value": NEXUS_SECRET},
    "prime-buffer":    {"port": 8003, "auth_header": "X-Nexus-Prime-Secret", "auth_value": NEXUS_SECRET},
    "omni":            {"port": 8004, "auth_header": "X-Nexus-Secret",       "auth_value": NEXUS_SECRET},
    "alpha-execution": {"port": 8005, "auth_header": "X-Nexus-Secret",       "auth_value": NEXUS_SECRET},
    "prime-execution": {"port": 8006, "auth_header": "X-Nexus-Prime-Secret", "auth_value": NEXUS_SECRET},
    "oracle":          {"port": 8007, "auth_header": "X-Oracle-Secret",      "auth_value": os.getenv("ORACLE_SECRET", NEXUS_SECRET)},
    "ails":            {"port": 8008, "auth_header": "X-AILS-Secret",        "auth_value": os.getenv("AILS_SECRET", NEXUS_SECRET)},
    "guardian-angel":  {"port": 8009, "auth_header": "",                     "auth_value": ""},
}

SQS_SERVICES: Dict[str, Dict[str, Any]] = {
    "capital-router":  {"port": 8000, "auth_header": "X-SQS-Sovereign-Secret", "auth_value": os.getenv("SQS_SOVEREIGN_SECRET", "")},
    "atm-multi":       {"port": 9004, "auth_header": "X-SQS-Sovereign-Secret", "auth_value": os.getenv("SQS_SOVEREIGN_SECRET", "")},
    "atm-0dte":        {"port": 9005, "auth_header": "X-SQS-Sovereign-Secret", "auth_value": os.getenv("SQS_SOVEREIGN_SECRET", "")},
    "atg-swing":       {"port": 9006, "auth_header": "X-SQS-Sovereign-Secret", "auth_value": os.getenv("SQS_SOVEREIGN_SECRET", "")},
    "atg-intraday":    {"port": 9007, "auth_header": "X-SQS-Sovereign-Secret", "auth_value": os.getenv("SQS_SOVEREIGN_SECRET", "")},
}

ALL_SERVICES = {**NEXUS_SERVICES, **SQS_SERVICES}


def _service_url(name: str, path: str = "/health") -> Optional[str]:
    svc = ALL_SERVICES.get(name)
    if not svc:
        return None
    return f"http://{NEXUS_HOST}:{svc['port']}{path}"


def _service_headers(name: str) -> Dict[str, str]:
    svc = ALL_SERVICES.get(name)
    if not svc or not svc.get("auth_header"):
        return {}
    return {svc["auth_header"]: svc["auth_value"]}


# ---------------------------------------------------------------------------
# ACK back to SOVEREIGN via bus
# ---------------------------------------------------------------------------

def _ack_sovereign(directive_id: str, result: Any) -> None:
    """Send ACK to SOVEREIGN inbox on the bus. Fire-and-forget, never raises."""
    def _send():
        try:
            payload = {
                "from": "command-relay",
                "to": "sovereign",
                "message": f"ack:{directive_id}: {json.dumps(result)}",
            }
            requests.post(f"{SOVEREIGN_BUS_URL}/send", json=payload, timeout=HTTP_TIMEOUT)
        except Exception as exc:
            logger.warning("command_relay: ACK failed for %s: %s", directive_id, exc)
    threading.Thread(target=_send, daemon=True).start()


# ---------------------------------------------------------------------------
# Directive handlers
# ---------------------------------------------------------------------------

def _handle_fleet_status(data: Dict) -> Dict:
    """Query /health on all services in parallel."""
    results = {}
    lock = threading.Lock()

    def _check(name: str):
        url = _service_url(name)
        try:
            resp = requests.get(url, headers=_service_headers(name), timeout=HTTP_TIMEOUT)
            status = "ok" if resp.status_code == 200 else f"HTTP {resp.status_code}"
        except Exception as exc:
            status = f"unreachable: {str(exc)[:60]}"
        with lock:
            results[name] = status

    threads = [threading.Thread(target=_check, args=(n,), daemon=True) for n in ALL_SERVICES]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=HTTP_TIMEOUT + 1)

    healthy = sum(1 for v in results.values() if v == "ok")
    return {"services": results, "healthy": healthy, "total": len(ALL_SERVICES)}


def _handle_service_status(data: Dict) -> Dict:
    """Query /health on one named service."""
    name = data.get("service", "")
    url = _service_url(name)
    if not url:
        return {"error": f"unknown service: {name}"}
    try:
        resp = requests.get(url, headers=_service_headers(name), timeout=HTTP_TIMEOUT)
        return {"service": name, "status": resp.status_code, "body": resp.json() if resp.status_code == 200 else {}}
    except Exception as exc:
        return {"service": name, "status": "unreachable", "error": str(exc)}


def _handle_halt_service(data: Dict) -> Dict:
    """POST /halt to a named service."""
    name = data.get("service", "")
    # For SQS services, use /sovereign/dispatch with HALT directive
    if name in SQS_SERVICES:
        url = _service_url(name, "/sovereign/dispatch")
        if not url:
            return {"error": f"unknown service: {name}"}
        try:
            resp = requests.post(
                url,
                json={"directive": "HALT", "id": "relay-halt", "data": {}},
                headers=_service_headers(name),
                timeout=HTTP_TIMEOUT,
            )
            return {"service": name, "action": "halt", "status": resp.status_code}
        except Exception as exc:
            return {"service": name, "error": str(exc)}
    else:
        url = _service_url(name, "/halt")
        if not url:
            return {"error": f"unknown service: {name}"}
        try:
            resp = requests.post(url, headers=_service_headers(name), timeout=HTTP_TIMEOUT)
            return {"service": name, "action": "halt", "status": resp.status_code}
        except Exception as exc:
            return {"service": name, "error": str(exc)}


def _handle_resume_service(data: Dict) -> Dict:
    """POST /resume to a named service."""
    name = data.get("service", "")
    if name in SQS_SERVICES:
        url = _service_url(name, "/sovereign/dispatch")
        if not url:
            return {"error": f"unknown service: {name}"}
        try:
            resp = requests.post(
                url,
                json={"directive": "RESUME", "id": "relay-resume", "data": {}},
                headers=_service_headers(name),
                timeout=HTTP_TIMEOUT,
            )
            return {"service": name, "action": "resume", "status": resp.status_code}
        except Exception as exc:
            return {"service": name, "error": str(exc)}
    else:
        url = _service_url(name, "/resume")
        if not url:
            return {"error": f"unknown service: {name}"}
        try:
            resp = requests.post(url, headers=_service_headers(name), timeout=HTTP_TIMEOUT)
            return {"service": name, "action": "resume", "status": resp.status_code}
        except Exception as exc:
            return {"service": name, "error": str(exc)}


def _handle_broadcast(data: Dict) -> Dict:
    """Push a message to multiple agent bus inboxes."""
    targets = data.get("targets", list(ALL_SERVICES.keys()))
    message = data.get("message", "")
    results = {}
    for target in targets:
        try:
            resp = requests.post(
                f"{SOVEREIGN_BUS_URL}/send",
                json={"from": "sovereign", "to": target, "message": message},
                timeout=HTTP_TIMEOUT,
            )
            results[target] = resp.status_code < 300
        except Exception as exc:
            results[target] = False
            logger.warning("command_relay: broadcast to %s failed: %s", target, exc)
    return {"broadcast": results, "message": message}


def _handle_ping(data: Dict) -> Dict:
    return {"pong": True, "relay_uptime_s": int(time.time() - START_T)}


DIRECTIVE_HANDLERS = {
    "FLEET_STATUS":        _handle_fleet_status,
    "SERVICE_STATUS":      _handle_service_status,
    "HALT_SERVICE":        _handle_halt_service,
    "RESUME_SERVICE":      _handle_resume_service,
    "BROADCAST":           _handle_broadcast,
    "PING":                _handle_ping,
}


# ---------------------------------------------------------------------------
# Bus polling loop (polls SOVEREIGN directives from command-relay inbox)
# ---------------------------------------------------------------------------

_watermark: float = 0.0
_watermark_lock = threading.Lock()


def _poll_bus_for_directives() -> None:
    """Poll command-relay inbox on bus for SOVEREIGN directives. Runs as daemon."""
    global _watermark
    logger.info("command_relay: bus poll loop started")
    while True:
        try:
            resp = requests.get(f"{SOVEREIGN_BUS_URL}/inbox/command-relay", timeout=HTTP_TIMEOUT)
            if resp.status_code == 200:
                messages = resp.json().get("messages", [])
                for msg in messages:
                    try:
                        _dispatch_bus_message(msg)
                    except Exception as exc:
                        logger.error("command_relay: dispatch error: %s", exc)
        except Exception as exc:
            logger.warning("command_relay: bus poll failed: %s", exc)
        time.sleep(30)


def _dispatch_bus_message(msg: Dict) -> None:
    """Parse and execute a directive from bus inbox."""
    content = msg.get("message", "")
    msg_id = msg.get("id", "unknown")

    # Format: "directive:{json}" or "DIRECTIVE"
    directive = ""
    data: Dict = {}
    if ":" in content:
        directive, _, rest = content.partition(":")
        directive = directive.strip().upper()
        try:
            data = json.loads(rest.strip())
        except Exception:
            data = {}
    else:
        directive = content.strip().upper()

    handler = DIRECTIVE_HANDLERS.get(directive)
    if handler:
        logger.info("command_relay: executing directive '%s' (id=%s)", directive, msg_id)
        result = handler(data)
        _ack_sovereign(msg_id, {"directive": directive, "result": result})
    else:
        logger.warning("command_relay: unknown directive '%s'", directive)
        _ack_sovereign(msg_id, {"error": f"unknown directive: {directive}"})


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    t = threading.Thread(target=_poll_bus_for_directives, daemon=True, name="relay-bus-poll")
    t.start()
    logger.info("Command Relay started — bus polling active")
    yield
    logger.info("Command Relay shutting down")


app = FastAPI(
    title="Command Relay",
    description="Layer 4 — SOVEREIGN directive execution relay",
    version="1.0.0",
    lifespan=lifespan,
)


class DirectiveRequest(BaseModel):
    directive: str
    id: str = ""
    data: dict = {}


@app.post("/execute")
def execute_directive(
    req: DirectiveRequest,
    x_relay_secret: Optional[str] = Header(None),
):
    """Execute a SOVEREIGN directive directly via HTTP (for low-latency commands)."""
    if x_relay_secret != RELAY_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    handler = DIRECTIVE_HANDLERS.get(req.directive.upper())
    if not handler:
        raise HTTPException(status_code=400, detail=f"unknown directive: {req.directive}")

    result = handler(req.data)
    if req.id:
        _ack_sovereign(req.id, {"directive": req.directive, "result": result})
    return {"ok": True, "directive": req.directive, "result": result}


# ---------------------------------------------------------------------------
# /relay/command — Cipher Phase 4 spec
# Routes commands from SOVEREIGN to named agents or services.
# Persists every command to CHRONICLE /chronicle/adaptive.
# ---------------------------------------------------------------------------

# Agent routing table: agent name → bus destination
AGENT_ROUTES: Dict[str, str] = {
    "cipher":  "cipher",
    "genesis": "genesis",
    "omni":    "omni",
    "primus":  "primus",
    "vector":  "vector",
    "atlas":   "atlas",
    "sage":    "sage",
    "axiom":   "axiom-agent",
}


def _chronicle_log_command(from_: str, to: str, command: str, payload: Any, result: Any) -> None:
    """Persist relay command to CHRONICLE adaptive_events. Fire-and-forget."""
    def _send():
        try:
            requests.post(
                f"{CHRONICLE_URL}/chronicle/adaptive",
                json={
                    "source": "command-relay",
                    "event_type": "relay_command",
                    "system": "command-relay",
                    "payload": {
                        "from": from_,
                        "to": to,
                        "command": command,
                        "payload": payload,
                        "result": result,
                        "ts": time.time(),
                    },
                },
                headers={"X-Chronicle-Auth": CHRONICLE_WRITE_TOKEN},
                timeout=5,
            )
        except Exception as exc:
            logger.warning("command_relay: CHRONICLE log failed: %s", exc)
    threading.Thread(target=_send, daemon=True).start()


def _route_to_agent(to: str, from_: str, command: str, payload: Any) -> Dict:
    """Route command to an agent via the message bus."""
    bus_dest = AGENT_ROUTES.get(to.lower())
    if not bus_dest:
        return {"error": f"unknown agent: {to}"}
    message = json.dumps({"command": command, "payload": payload, "from": from_})
    try:
        resp = requests.post(
            f"{SOVEREIGN_BUS_URL}/send",
            json={"from": "command-relay", "to": bus_dest, "message": message},
            timeout=HTTP_TIMEOUT,
        )
        return {"routed": True, "bus_status": resp.status_code, "agent": to}
    except Exception as exc:
        return {"error": str(exc), "agent": to}


class RelayCommandRequest(BaseModel):
    from_: str = ""
    to: str
    command: str
    payload: dict = {}

    class Config:
        populate_by_name = True

    # Accept both "from" and "from_" as input keys
    @classmethod
    def model_validate(cls, obj, *args, **kwargs):
        if isinstance(obj, dict) and "from" in obj and "from_" not in obj:
            obj = dict(obj)
            obj["from_"] = obj.pop("from")
        return super().model_validate(obj, *args, **kwargs)


@app.post("/relay/command")
def relay_command(
    req: RelayCommandRequest,
    x_relay_secret: Optional[str] = Header(None),
):
    """
    Route a command from SOVEREIGN to a named agent or Nexus service.

    Body: {from, to, command, payload}
    - to: agent name (cipher/genesis/omni/primus/vector) or service name
    - Routes agents via message bus; routes services via direct HTTP
    - Every command persisted to CHRONICLE /chronicle/adaptive
    """
    if x_relay_secret != RELAY_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    to = req.to.lower()
    result: Dict

    if to in AGENT_ROUTES:
        result = _route_to_agent(to, req.from_, req.command, req.payload)
    elif to in ALL_SERVICES:
        # Treat command as a directive to the named service
        handler = DIRECTIVE_HANDLERS.get("SERVICE_STATUS") if req.command in ("status", "health") else None
        if handler:
            result = handler({"service": to})
        else:
            result = {"error": f"unsupported command '{req.command}' for service '{to}'; use /execute for directives"}
    else:
        result = {"error": f"unknown target: {req.to}"}

    # Persist to CHRONICLE regardless of outcome
    _chronicle_log_command(req.from_, req.to, req.command, req.payload, result)

    logger.info("relay/command: %s → %s [%s] → %s", req.from_, req.to, req.command, result)
    return {"ok": True, "to": req.to, "command": req.command, "result": result}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_T),
        "directives": list(DIRECTIVE_HANDLERS.keys()),
        "agent_routes": list(AGENT_ROUTES.keys()),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("COMMAND_RELAY_PORT", "9997"))
    logger.info("Starting Command Relay on port %d", port)
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="warning", access_log=False)
