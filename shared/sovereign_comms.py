"""
sovereign_comms.py — Bidirectional SOVEREIGN Communication Module v2.0

Drop-in shared module for every agent on 192.168.1.141.

Public API:
  - report()                : fire-and-forget push to SOVEREIGN (with EscalationLevel routing)
  - get_instructions()      : poll SOVEREIGN's outbox with watermark deduplication
  - dispatch_instruction()  : parse and execute a raw SOVEREIGN instruction dict
  - start_polling_loop()    : launch background daemon thread that polls every N seconds

Escalation levels (EscalationLevel):
  CRITICAL     → SOVEREIGN immediate (bus, daemon thread)
  INFO         → SOVEREIGN standard (bus, daemon thread)
  EOD          → SQLite queue; flushed daily at 16:15 ET
  AUTONOMOUS   → suppressed; no bus call, no thread (logged at DEBUG only)
  AHMED_DIRECT → bypass SOVEREIGN; Telegram direct to Ahmed (daemon thread)

All methods are safe to call at any time — they never raise.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (env vars with hardcoded fallbacks)
# ---------------------------------------------------------------------------

SOVEREIGN_BUS_URL: str = os.getenv(
    "SOVEREIGN_BUS_URL", "http://192.168.1.141:9999"
)
NEXUS_HOST: str = os.getenv("NEXUS_HOST", "192.168.1.141")

# ---------------------------------------------------------------------------
# Agent Registry (used by push_directive / fleet_status / broadcast)
# Override any entry via env var NEXUS_<AGENT_UPPER>_URL
# ---------------------------------------------------------------------------

def _agent_url(name: str, default_port: int) -> str:
    env_key = f"NEXUS_{name.upper()}_URL"
    return os.getenv(env_key, f"http://{NEXUS_HOST}:{default_port}")


AGENT_REGISTRY: Dict[str, str] = {
    "axiom":          _agent_url("axiom",          8001),
    "alpha-buffer":   _agent_url("alpha_buffer",   8002),
    "prime-buffer":   _agent_url("prime_buffer",   8003),
    "omni":           _agent_url("omni",           8004),
    "alpha-execution":_agent_url("alpha_execution",8005),
    "prime-execution":_agent_url("prime_execution",8006),
    "oracle":         _agent_url("oracle",         8007),
    "ails":           _agent_url("ails",           8008),
    "guardian-angel": _agent_url("guardian_angel", 8009),
    "pipeline-sentinel": _agent_url("pipeline_sentinel", 8010),
    "sovereign":      _agent_url("sovereign",      9000),
    "cipher":         _agent_url("cipher",         9001),
    "atlas":          _agent_url("atlas",          9002),
    "sage":           _agent_url("sage",           9003),
    "genesis":        _agent_url("genesis",        9004),
    "primus":         _agent_url("primus",         9005),
}
GENESIS_BOT_TOKEN: str = os.getenv("GENESIS_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
SOVEREIGN_TELEGRAM_FALLBACK_CHAT_ID: str = os.getenv(
    "SOVEREIGN_TELEGRAM_FALLBACK_CHAT_ID", "8573754783"
)
EOD_DB_PATH: str = os.getenv(
    "NEXUS_EOD_DB_PATH", "/Users/ahmedsadek/nexus/data/eod_queue.db"
)
EOD_FLUSH_HOUR: int = 16
EOD_FLUSH_MINUTE: int = 15
POLL_INTERVAL_SECONDS: int = int(os.getenv("SOVEREIGN_POLL_INTERVAL", "60"))

_RETRY_DELAYS: List[float] = [0.5, 1.0, 2.0]
_HTTP_TIMEOUT: float = 3.0

# ---------------------------------------------------------------------------
# Escalation Level Constants
# ---------------------------------------------------------------------------


class EscalationLevel:
    """
    Five-tier escalation routing table.

    CRITICAL     — SOVEREIGN immediate; process within 60 seconds.
    INFO         — SOVEREIGN standard; next 60-second batch cycle.
    EOD          — Daily batch; flushed at 16:15 ET via SQLite-backed queue.
    AUTONOMOUS   — Suppressed; do not send, do not thread. Logged at DEBUG.
    AHMED_DIRECT — Bypass SOVEREIGN; Telegram direct to Ahmed immediately.
    """

    CRITICAL: str = "critical"
    INFO: str = "info"
    EOD: str = "eod"
    AUTONOMOUS: None = None          # type: ignore[assignment]
    AHMED_DIRECT: str = "ahmed"


# ---------------------------------------------------------------------------
# EOD SQLite Queue
# ---------------------------------------------------------------------------

_eod_db_lock = threading.Lock()
_eod_flush_thread_started = False
_eod_flush_thread_lock = threading.Lock()


def _eod_db_connect() -> sqlite3.Connection:
    """Open (and initialise if needed) the EOD SQLite queue database."""
    Path(EOD_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(EOD_DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eod_queue (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name  TEXT    NOT NULL,
            message_type TEXT   NOT NULL,
            payload     TEXT    NOT NULL,
            queued_at   TEXT    NOT NULL
        )
    """)
    conn.commit()
    return conn


def _eod_enqueue(agent_name: str, message_type: str, payload: Any) -> None:
    """Persist one EOD entry to SQLite. Never raises."""
    try:
        with _eod_db_lock:
            conn = _eod_db_connect()
            conn.execute(
                "INSERT INTO eod_queue (agent_name, message_type, payload, queued_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    agent_name,
                    message_type,
                    _serialize_payload(payload),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.error("sovereign_comms: EOD enqueue failed: %s — message lost", exc)


def _eod_flush_and_clear() -> List[Dict]:
    """Read all queued EOD rows, delete them, return as list of dicts. Never raises."""
    try:
        with _eod_db_lock:
            conn = _eod_db_connect()
            rows = conn.execute(
                "SELECT agent_name, message_type, payload, queued_at FROM eod_queue ORDER BY id"
            ).fetchall()
            conn.execute("DELETE FROM eod_queue")
            conn.commit()
            conn.close()
        return [
            {"agent": r[0], "message_type": r[1], "payload": r[2], "queued_at": r[3]}
            for r in rows
        ]
    except Exception as exc:  # noqa: BLE001
        logger.error("sovereign_comms: EOD flush failed: %s", exc)
        return []


def _eod_flush_worker() -> None:
    """
    Background daemon thread.
    Sleeps until 16:15 ET each day, flushes EOD queue to SOVEREIGN bus.
    Loops forever (one flush per calendar day).
    """
    import pytz  # soft import — already required by every nexus service

    ET = pytz.timezone("America/New_York")

    while True:
        try:
            now = datetime.now(ET)
            flush_today = now.replace(
                hour=EOD_FLUSH_HOUR, minute=EOD_FLUSH_MINUTE,
                second=0, microsecond=0,
            )
            if now >= flush_today:
                # Already past today's flush — sleep until tomorrow's
                from datetime import timedelta
                flush_today = flush_today + timedelta(days=1)
            sleep_seconds = (flush_today - now).total_seconds()
            logger.debug(
                "sovereign_comms: EOD flush scheduled in %.0f seconds", sleep_seconds
            )
            time.sleep(max(sleep_seconds, 1))

            entries = _eod_flush_and_clear()
            if not entries:
                logger.info("sovereign_comms: EOD flush — queue empty, nothing to send")
                continue

            body = {
                "from": "sovereign_comms_eod",
                "to": "sovereign",
                "message": f"eod_summary: {json.dumps(entries)}",
            }
            url = f"{SOVEREIGN_BUS_URL}/send"
            try:
                resp = requests.post(url, json=body, timeout=_HTTP_TIMEOUT)
                if resp.status_code < 300:
                    logger.info(
                        "sovereign_comms: EOD flush sent — %d entries", len(entries)
                    )
                else:
                    logger.warning(
                        "sovereign_comms: EOD flush HTTP %d — entries may be lost",
                        resp.status_code,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error("sovereign_comms: EOD flush bus POST failed: %s", exc)

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "sovereign_comms: EOD flush thread crash: %s — will retry next cycle", exc
            )
            time.sleep(60)


def _ensure_eod_flush_thread() -> None:
    """Start the EOD flush daemon thread exactly once per process."""
    global _eod_flush_thread_started
    with _eod_flush_thread_lock:
        if _eod_flush_thread_started:
            return
        t = threading.Thread(target=_eod_flush_worker, daemon=True, name="sovereign-eod-flush")
        t.start()
        _eod_flush_thread_started = True
        logger.info("sovereign_comms: EOD flush thread started (flushes at %02d:%02d ET)",
                    EOD_FLUSH_HOUR, EOD_FLUSH_MINUTE)


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _serialize_payload(payload: Any) -> str:
    """
    JSON-serialise payload. Falls back to str() on failure.

    :param payload: Arbitrary value to serialise.
    :return: JSON string or str() representation.
    """
    try:
        return json.dumps(payload)
    except (TypeError, ValueError) as exc:
        logger.error(
            "sovereign_comms: payload not JSON-serialisable (%s) — falling back to str()", exc
        )
        return str(payload)


# ---------------------------------------------------------------------------
# Bus / Telegram delivery helpers
# ---------------------------------------------------------------------------


def _post_to_bus(
    agent_name: str,
    message_type: str,
    payload: Any,
    escalation: Optional[str] = EscalationLevel.INFO,
) -> bool:
    """
    POST a message to the SOVEREIGN bus with up to 3 retries.

    :param agent_name: Sending agent name.
    :param message_type: Category string.
    :param payload: Message body.
    :param escalation: Escalation level string (included in message body).
    :return: True on success, False after all retries exhausted.
    """
    body = {
        "from": agent_name,
        "to": "sovereign",
        "message": f"{message_type}: {_serialize_payload(payload)}",
        "escalation": escalation or "info",
    }
    url = f"{SOVEREIGN_BUS_URL}/send"

    for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
        try:
            resp = requests.post(url, json=body, timeout=_HTTP_TIMEOUT)
            if resp.status_code < 300:
                return True
            logger.warning(
                "sovereign_comms: bus POST attempt %d returned HTTP %d",
                attempt, resp.status_code,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sovereign_comms: bus POST attempt %d failed: %s", attempt, exc)
        if attempt < len(_RETRY_DELAYS):
            time.sleep(delay)

    return False


def _post_telegram_direct(
    agent_name: str,
    message_type: str,
    payload: Any,
    prefix: str = "[DIRECT]",
) -> bool:
    """
    Post directly to Ahmed's Telegram. Used for AHMED_DIRECT and bus fallback.

    :param agent_name: Sending agent name.
    :param message_type: Category string.
    :param payload: Message body.
    :param prefix: Message prefix tag.
    :return: True on success, False on failure.
    """
    text = (
        f"{prefix} {agent_name.upper()} → {message_type}: "
        f"{_serialize_payload(payload)}"
    )
    url = f"https://api.telegram.org/bot{GENESIS_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": SOVEREIGN_TELEGRAM_FALLBACK_CHAT_ID, "text": text},
            timeout=_HTTP_TIMEOUT,
        )
        return resp.status_code < 300
    except Exception as exc:  # noqa: BLE001
        logger.error("sovereign_comms: Telegram direct failed: %s", exc)
        return False


def _report_worker(
    agent_name: str,
    message_type: str,
    payload: Any,
    escalation: Optional[str],
) -> None:
    """
    Daemon-thread worker for report().

    Routes:
      AHMED_DIRECT → Telegram direct (no bus)
      CRITICAL/INFO → bus with retry; fallback to Telegram on bus failure
    """
    try:
        if escalation == EscalationLevel.AHMED_DIRECT:
            if not _post_telegram_direct(agent_name, message_type, payload, prefix="[DIRECT]"):
                logger.error(
                    "sovereign_comms: AHMED_DIRECT delivery failed for %s/%s — message lost",
                    agent_name, message_type,
                )
            return

        # CRITICAL or INFO → bus first
        if _post_to_bus(agent_name, message_type, payload, escalation):
            return

        logger.warning(
            "sovereign_comms: bus unreachable after 3 retries — attempting Telegram fallback"
        )
        if _post_telegram_direct(
            agent_name, message_type, payload, prefix="[SOVEREIGN BUS DOWN]"
        ):
            return

        logger.error(
            "sovereign_comms: all delivery paths failed for %s/%s — message discarded",
            agent_name, message_type,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "sovereign_comms: unhandled exception in report worker: %s — message discarded", exc
        )


# ---------------------------------------------------------------------------
# Watermark helpers
# ---------------------------------------------------------------------------


def _watermark_path(agent_name: str) -> Path:
    env_key = f"NEXUS_{agent_name.upper()}_DIR"
    workdir = os.getenv(env_key, f"/Users/ahmedsadek/nexus/{agent_name}/")
    return Path(workdir) / ".sovereign_watermark"


def _read_watermark(path: Path) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if not path.exists():
        return epoch
    try:
        raw = path.read_text().strip()
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sovereign_comms: corrupt watermark at %s (%s) — resetting to epoch", path, exc
        )
        return epoch


def _write_watermark(path: Path, ts: datetime) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ts.isoformat())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sovereign_comms: cannot write watermark to %s (%s) — "
            "watermark held in memory only",
            path, exc,
        )


def _parse_ts(ts_str: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Instruction dispatch
# ---------------------------------------------------------------------------

# Registry of directive handlers: directive_name → callable(instr_dict) → None
# Agents can register their own handlers via register_directive_handler().
_directive_handlers: Dict[str, Callable[[Dict], None]] = {}
_directive_handlers_lock = threading.Lock()

# Built-in no-op directives that every agent honours silently
_NOOP_DIRECTIVES = frozenset({"PING", "NOP", "HEARTBEAT"})


def register_directive_handler(directive: str, handler: Callable[[Dict], None]) -> None:
    """
    Register a handler for a named SOVEREIGN directive.

    Directive names are case-insensitive. The handler receives the full
    instruction dict: {"id", "from", "message", "timestamp"}.

    :param directive: Directive name (e.g. "HALT", "RESUME", "STATUS").
    :param handler:   Callable that executes the directive. Must never raise.
    """
    with _directive_handlers_lock:
        _directive_handlers[directive.upper()] = handler
    logger.debug("sovereign_comms: registered handler for directive '%s'", directive.upper())


def dispatch_instruction(instr: Dict) -> None:
    """
    Parse and execute one SOVEREIGN instruction dict.

    Instruction message format: "DIRECTIVE" or "DIRECTIVE: <json_or_text>"
    Dispatches to a registered handler if available.
    Logs unrecognised directives at WARNING — never raises.

    :param instr: Instruction dict with keys: id, from, message, timestamp.
    """
    try:
        raw = instr.get("message", "").strip()
        if ":" in raw:
            directive, _, args_raw = raw.partition(":")
            directive = directive.strip().upper()
            args_raw = args_raw.strip()
            try:
                args = json.loads(args_raw)
            except (json.JSONDecodeError, ValueError):
                args = args_raw
        else:
            directive = raw.upper()
            args = {}

        if directive in _NOOP_DIRECTIVES:
            logger.debug("sovereign_comms: NOOP directive '%s' — acknowledged", directive)
            return

        with _directive_handlers_lock:
            handler = _directive_handlers.get(directive)

        if handler:
            logger.info("sovereign_comms: dispatching directive '%s'", directive)
            handler(instr)
        else:
            logger.warning(
                "sovereign_comms: unrecognised directive '%s' (id=%s) — no handler registered",
                directive, instr.get("id", "?"),
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "sovereign_comms: dispatch_instruction crashed for instr=%s: %s", instr, exc
        )


# ---------------------------------------------------------------------------
# Background polling loop
# ---------------------------------------------------------------------------

_polling_threads: Dict[str, threading.Thread] = {}
_polling_threads_lock = threading.Lock()


def start_polling_loop(
    agent_name: str,
    interval_seconds: int = POLL_INTERVAL_SECONDS,
    on_instruction: Optional[Callable[[Dict], None]] = None,
) -> None:
    """
    Launch a background daemon thread that polls SOVEREIGN every interval_seconds.

    Safe to call multiple times — only one thread is ever started per agent_name.
    Instructions are dispatched via dispatch_instruction() (registered handlers)
    and optionally also passed to on_instruction() callback.

    :param agent_name:        Agent name — used for inbox polling.
    :param interval_seconds:  Poll interval in seconds (default: 60).
    :param on_instruction:    Optional callback called for each new instruction.
    """
    with _polling_threads_lock:
        if agent_name in _polling_threads and _polling_threads[agent_name].is_alive():
            logger.debug(
                "sovereign_comms: polling loop already running for '%s'", agent_name
            )
            return

        def _loop() -> None:
            logger.info(
                "sovereign_comms: polling loop started for '%s' (interval=%ds)",
                agent_name, interval_seconds,
            )
            while True:
                try:
                    instructions = get_instructions(agent_name)
                    if instructions:
                        logger.info(
                            "sovereign_comms: %d instruction(s) from SOVEREIGN for '%s'",
                            len(instructions), agent_name,
                        )
                        for instr in instructions:
                            dispatch_instruction(instr)
                            if on_instruction:
                                try:
                                    on_instruction(instr)
                                except Exception as cb_exc:  # noqa: BLE001
                                    logger.error(
                                        "sovereign_comms: on_instruction callback error: %s",
                                        cb_exc,
                                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "sovereign_comms: polling loop error for '%s': %s", agent_name, exc
                    )
                time.sleep(interval_seconds)

        t = threading.Thread(
            target=_loop,
            daemon=True,
            name=f"sovereign-poll-{agent_name}",
        )
        t.start()
        _polling_threads[agent_name] = t
        logger.info(
            "sovereign_comms: polling loop thread launched for '%s'", agent_name
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def report(
    agent_name: str,
    message_type: str,
    payload: Dict[str, Any],
    escalation: Optional[str] = EscalationLevel.INFO,
) -> None:
    """
    Fire-and-forget push to SOVEREIGN with escalation-level routing.

    Escalation routing:
      AUTONOMOUS (None) → returns immediately, no thread, no bus call.
                          Logged at DEBUG: "considered and suppressed".
      EOD               → enqueued in SQLite; flushed at 16:15 ET daily.
      AHMED_DIRECT      → Telegram direct to Ahmed, bypasses SOVEREIGN bus.
      CRITICAL / INFO   → daemon thread → bus (3 retries) → Telegram fallback.

    Never raises under any circumstances.

    :param agent_name:   Sending agent name (e.g. "cipher", "atlas").
    :param message_type: Message category (e.g. "alert", "status", "escalation").
    :param payload:      Arbitrary JSON-serialisable dict.
    :param escalation:   EscalationLevel constant. Default: INFO.
    """
    # AUTONOMOUS — considered and suppressed
    if escalation is EscalationLevel.AUTONOMOUS:
        logger.debug(
            "sovereign_comms: AUTONOMOUS suppression — %s/%s not sent (by design)",
            agent_name, message_type,
        )
        return

    # EOD — SQLite queue
    if escalation == EscalationLevel.EOD:
        _eod_enqueue(agent_name, message_type, payload)
        _ensure_eod_flush_thread()
        logger.debug(
            "sovereign_comms: EOD enqueued — %s/%s", agent_name, message_type
        )
        return

    # CRITICAL, INFO, AHMED_DIRECT — daemon thread
    t = threading.Thread(
        target=_report_worker,
        args=(agent_name, message_type, payload, escalation),
        daemon=True,
    )
    t.start()


def get_instructions(agent_name: str) -> List[Dict[str, str]]:
    """
    Poll SOVEREIGN's outbox for new instructions, deduplicated via watermark.

    Never raises under any circumstances.

    :param agent_name: Agent polling its own inbox (e.g. "cipher").
    :return: List of unprocessed instruction dicts:
             {"id": str, "from": str, "message": str, "timestamp": str}.
             Returns [] on any failure.
    """
    url = f"{SOVEREIGN_BUS_URL}/inbox/{agent_name}"
    wm_path = _watermark_path(agent_name)
    watermark = _read_watermark(wm_path)

    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT)
        if resp.status_code >= 300:
            logger.warning(
                "sovereign_comms: GET inbox returned HTTP %d for agent %s",
                resp.status_code, agent_name,
            )
            return []
        data = resp.json()
        messages: List[Dict] = data.get("messages", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("sovereign_comms: get_instructions failed for %s: %s", agent_name, exc)
        return []

    new_messages: List[Dict[str, str]] = []
    latest_ts: datetime = watermark

    for msg in messages:
        ts_raw = msg.get("timestamp")
        if ts_raw is None:
            logger.warning(
                "sovereign_comms: skipping malformed message (no timestamp): %s", msg
            )
            continue
        ts = _parse_ts(ts_raw)
        if ts is None:
            logger.warning(
                "sovereign_comms: skipping message with unparseable timestamp: %s", ts_raw
            )
            continue
        if ts > watermark:
            new_messages.append(
                {
                    "id": str(msg.get("id", "")),
                    "from": str(msg.get("from", "")),
                    "message": str(msg.get("message", "")),
                    "timestamp": ts_raw,
                }
            )
            if ts > latest_ts:
                latest_ts = ts

    if new_messages:
        _write_watermark(wm_path, latest_ts)

    return new_messages


# ---------------------------------------------------------------------------
# V2 SOVEREIGN → AGENT command API
# ---------------------------------------------------------------------------

def push_directive(
    agent: str,
    directive: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Push a directive from SOVEREIGN to a named agent.

    Attempts HTTP POST to {agent_url}/sovereign/directive first.
    Falls back to bus POST on HTTP failure or non-200 response.
    Returns immediately if agent is not in AGENT_REGISTRY.

    Return dict keys: ok (bool), method (http|bus|failed), agent (str).
    Never raises.

    :param agent:     Target agent name (must be in AGENT_REGISTRY).
    :param directive: Directive string, e.g. "HALT", "STATUS", "SET_THRESHOLD".
    :param data:      Optional data payload dict.
    :return:          Result dict with ok, method, agent.
    """
    if agent not in AGENT_REGISTRY:
        logger.warning("sovereign_comms: push_directive to unknown agent '%s'", agent)
        return {"ok": False, "method": "failed", "agent": agent, "reason": "unknown_agent"}

    agent_url = AGENT_REGISTRY[agent]
    body: Dict[str, Any] = {"directive": directive, "data": data, "from": "sovereign"}

    # --- HTTP path ---
    try:
        resp = requests.post(
            f"{agent_url}/sovereign/directive",
            json=body,
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            try:
                result: Dict[str, Any] = resp.json()
            except Exception:
                result = {}
            result["ok"] = True
            result["method"] = "http"
            result["agent"] = agent
            return result
        logger.warning(
            "sovereign_comms: push_directive HTTP %d for '%s' → bus fallback",
            resp.status_code, agent,
        )
    except Exception as exc:
        logger.warning(
            "sovereign_comms: push_directive HTTP failed for '%s': %s → bus fallback",
            agent, exc,
        )

    # --- Bus fallback ---
    try:
        bus_body = {
            "from": "sovereign",
            "to": agent,
            "message": f"directive: {json.dumps({'directive': directive, 'data': data})}",
        }
        resp = requests.post(f"{SOVEREIGN_BUS_URL}/send", json=bus_body, timeout=_HTTP_TIMEOUT)
        if resp.status_code < 300:
            return {"ok": True, "method": "bus", "agent": agent}
        logger.warning(
            "sovereign_comms: push_directive bus HTTP %d for '%s'",
            resp.status_code, agent,
        )
    except Exception as exc:
        logger.warning(
            "sovereign_comms: push_directive bus failed for '%s': %s",
            agent, exc,
        )

    return {"ok": False, "method": "failed", "agent": agent}


def fleet_status(
    agents: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Query /health on each agent in parallel.

    Returns dict of agent_name → {ok, status, ...}.
    Unknown agents (not in AGENT_REGISTRY) return ok=False without a network call.
    Never raises.

    :param agents: List of agent names to query. Defaults to all in AGENT_REGISTRY.
    :return:       Dict of agent_name → health result dict.
    """
    targets = agents if agents is not None else list(AGENT_REGISTRY.keys())
    results: Dict[str, Dict[str, Any]] = {}
    lock = threading.Lock()

    def _check(name: str) -> None:
        if name not in AGENT_REGISTRY:
            with lock:
                results[name] = {"ok": False, "status": "unknown_agent"}
            return
        url = f"{AGENT_REGISTRY[name]}/health"
        try:
            resp = requests.get(url, timeout=_HTTP_TIMEOUT)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    data = {}
                data["ok"] = True
                with lock:
                    results[name] = data
            else:
                with lock:
                    results[name] = {"ok": False, "status": f"HTTP {resp.status_code}"}
        except Exception as exc:
            with lock:
                results[name] = {"ok": False, "status": str(exc)}

    threads = [threading.Thread(target=_check, args=(a,), daemon=True) for a in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_HTTP_TIMEOUT + 1)

    return results


def broadcast(
    directive: str,
    data: Optional[Dict[str, Any]] = None,
    targets: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Push a directive to multiple agents simultaneously.

    Calls push_directive() for each target sequentially.
    Returns dict of agent_name → push_directive result.
    Never raises.

    :param directive: Directive string.
    :param data:      Optional data payload dict.
    :param targets:   Agent names to target. Defaults to all in AGENT_REGISTRY.
    :return:          Dict of agent_name → result dict.
    """
    target_list = targets if targets is not None else list(AGENT_REGISTRY.keys())
    return {agent: push_directive(agent, directive, data) for agent in target_list}


def poll_and_execute(
    agent_name: str,
    dispatch_fn: Callable[[Dict[str, str]], None],
) -> int:
    """
    Poll the SOVEREIGN inbox for agent_name and dispatch each new instruction.

    Calls dispatch_fn for each new instruction. Exceptions from dispatch_fn
    are caught, logged, and do NOT stop processing of subsequent instructions.

    Returns the count of successfully dispatched instructions (i.e. those
    where dispatch_fn did not raise).

    Uses the same watermark deduplication as get_instructions().
    Never raises.

    :param agent_name:  Agent polling its own inbox.
    :param dispatch_fn: Callable called for each new instruction dict.
    :return:            Count of successfully dispatched instructions.
    """
    instructions = get_instructions(agent_name)
    count = 0
    for instr in instructions:
        try:
            dispatch_fn(instr)
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "sovereign_comms: poll_and_execute dispatch error for '%s' instr=%s: %s",
                agent_name, instr.get("id", "?"), exc,
            )
    return count
