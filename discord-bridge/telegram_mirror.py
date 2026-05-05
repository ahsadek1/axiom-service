"""
telegram_mirror.py — Telegram → Discord Mirror Poller
Polls OpenClaw agent session JSONL files every 10 seconds.
Forwards new assistant messages to Discord via /mirror endpoint.

Agent → Discord channel routing matches channel_map.json.
Skips: NO_REPLY, HEARTBEAT_OK, tool calls, tool results.
Only mirrors role=assistant text content.

Author: GENESIS — 2026-04-28
"""
import json
import logging
import os
import time
import threading
from pathlib import Path
from typing import Dict, Optional
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s telegram_mirror %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("telegram_mirror")

# ── Config ────────────────────────────────────────────────────────────────────
SESSIONS_BASE   = Path("/Users/ahmedsadek/.openclaw/agents")
BRIDGE_URL      = os.environ.get("DISCORD_BRIDGE_URL", "http://localhost:8011")
BRIDGE_SECRET   = os.environ.get("DISCORD_BRIDGE_SECRET", "")
POLL_INTERVAL_S = int(os.environ.get("MIRROR_POLL_INTERVAL_S", "10"))

# Unified command-center channel — mirrors the Health & Integrity group conversation
# All agents active in that group route here so Ahmed sees full dialogue in one feed
COMMAND_CENTER_CHANNEL = "nexus-command-center"

# Session keys that represent the Health & Integrity group (both V1 and V2)
HEALTH_GROUP_SESSION_PATTERNS = [
    "group:-1003954790884",   # NEXUS V1 HEALTH & INTEGRITY GROUP
    "group:-1003579956463",   # NEXUS V2 HEALTH & INTEGRITY GROUP
]

# Agents to mirror and their Discord channel
AGENT_CHANNEL_MAP = {
    "genesis":   "findings-stream-a",
    "omni":      "omni-synthesis",
    "cipher":    "ahmed-decisions",
    "atlas":     "go-verdicts",
    "sage":      "sage-macro",
    "sovereign": "daily-health-report",
    "primus":    "primus-sqs",
    "vector":    "vector-investigations",
    "axiom":     "axiom-risk",
    "main":      "daily-health-report",
}

AGENT_COLOR_MAP = {
    "genesis":   "green",
    "omni":      "teal",
    "cipher":    "gold",
    "atlas":     "blue",
    "sage":      "indigo",
    "sovereign": "purple",
    "primus":    "orange",
    "vector":    "gray",
    "axiom":     "blue",
    "main":      "blue",
}

# Messages to skip
SKIP_MESSAGES = {"NO_REPLY", "HEARTBEAT_OK", "HEARTBEAT_OK\n"}

# ── State tracking — last seen message ID per agent ───────────────────────────
_last_seen: Dict[str, str] = {}   # agent → last message ID mirrored
_state_file = Path("/Users/ahmedsadek/nexus/discord-bridge/mirror_state.json")


def _load_state() -> None:
    """Load last-seen state from disk."""
    global _last_seen
    if _state_file.exists():
        try:
            _last_seen = json.loads(_state_file.read_text())
            logger.info("Mirror state loaded: %d agents tracked", len(_last_seen))
        except Exception as e:
            logger.warning("Failed to load mirror state: %s", e)
            _last_seen = {}


def _save_state() -> None:
    """Persist last-seen state to disk."""
    try:
        _state_file.write_text(json.dumps(_last_seen))
    except Exception as e:
        logger.warning("Failed to save mirror state: %s", e)


def _get_active_session(agent: str) -> Optional[Path]:
    """Return the most recently modified non-reset JSONL session file."""
    sessions_dir = SESSIONS_BASE / agent / "sessions"
    if not sessions_dir.exists():
        return None
    files = [
        f for f in sessions_dir.glob("*.jsonl")
        if ".reset." not in f.name and ".lock" not in f.name
    ]
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime)


def _get_all_sessions(agent: str) -> list:
    """Return recent non-reset JSONL session files for an agent.
    Capped to files modified in last 48h, max 20 per agent — prevents CPU spike
    from globbing thousands of historical session files on every poll cycle.
    """
    import time as _time
    sessions_dir = SESSIONS_BASE / agent / "sessions"
    if not sessions_dir.exists():
        return []
    cutoff = _time.time() - (48 * 3600)  # 48 hours ago
    files = []
    try:
        for f in sessions_dir.glob("*.jsonl"):
            if ".reset." in f.name or ".lock" in f.name:
                continue
            try:
                if f.stat().st_mtime >= cutoff:
                    files.append(f)
            except OSError:
                continue
    except Exception:
        return []
    # Sort by mtime desc, cap at 20
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files[:20]


# Cache: session stem → bool (health group or not)
# Avoids re-scanning files on every 10s poll cycle
_health_session_cache: Dict[str, bool] = {}


def _is_health_group_session(session_file: Path) -> bool:
    """Return True if this session file corresponds to the Health & Integrity group.
    Result is cached per file stem so each file is only scanned once.
    """
    stem = session_file.stem
    if stem in _health_session_cache:
        return _health_session_cache[stem]

    result = False
    try:
        with open(session_file, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i > 20:  # Only check header entries
                    break
                if any(pattern in line for pattern in HEALTH_GROUP_SESSION_PATTERNS):
                    result = True
                    break
    except Exception:
        pass

    _health_session_cache[stem] = result
    return result


def _extract_text(content) -> str:
    """Extract plain text from message content (string or list of blocks)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", "").strip())
        return "\n".join(p for p in parts if p)
    return ""


def _should_skip(text: str) -> bool:
    """Return True if this message should not be mirrored."""
    if not text:
        return True
    stripped = text.strip()
    if stripped in SKIP_MESSAGES:
        return True
    if stripped.startswith("HEARTBEAT"):
        return True
    # Skip very short system acks
    if len(stripped) < 3:
        return True
    return False


def _post_mirror(agent: str, text: str, msg_id: str, override_channel: str = "") -> bool:
    """POST message to /mirror endpoint. Returns True on success."""
    if not BRIDGE_SECRET:
        logger.warning("DISCORD_BRIDGE_SECRET not set — skipping mirror")
        return False
    try:
        payload = {
            "agent":   agent,
            "message": text,
            "session": msg_id,
        }
        if override_channel:
            payload["channel"] = override_channel
        resp = requests.post(
            f"{BRIDGE_URL}/mirror",
            json=payload,
            headers={"X-Mirror-Secret": BRIDGE_SECRET},
            timeout=5,
        )
        if resp.status_code == 202:
            return True
        if resp.status_code == 200:
            # Skipped (system message)
            return True
        logger.warning("Mirror rejected %s: %d %s", agent, resp.status_code, resp.text[:100])
        return False
    except requests.exceptions.ConnectionError:
        logger.warning("Bridge not reachable — skipping mirror for %s", agent)
        return False
    except Exception as e:
        logger.warning("Mirror error for %s: %s", agent, e)
        return False


# Sender ID for Ahmed — messages from him get mirrored as "Ahmed" not as an agent
AHMED_SENDER_ID = "8573754783"
AHMED_DISPLAY = "ahmed"   # pseudo-agent name for color/emoji routing


def _poll_session(agent: str, session_file: Path, state_key: str, target_channel: str) -> None:
    """Poll a specific session file and mirror new messages to target_channel.

    For health-group sessions: mirrors BOTH assistant (agent) messages AND
    user messages from Ahmed — giving full bidirectional conversation on Discord.
    For agent-only sessions: mirrors assistant messages only (original behaviour).
    """
    is_health_session = target_channel == COMMAND_CENTER_CHANNEL
    last_id = _last_seen.get(state_key)
    new_last_id = last_id
    found_last = (last_id is None)
    new_messages = []  # list of (msg_id, text, speaker)

    try:
        with open(session_file, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                if entry.get("type") != "message":
                    continue

                msg_id  = entry.get("id", "")
                message = entry.get("message", {})
                role    = message.get("role", "")

                if not found_last:
                    if msg_id == last_id:
                        found_last = True
                    continue

                # ── User messages (Ahmed) ──
                if role == "user" and is_health_session:
                    content = message.get("content", "")
                    # Skip forwarded Discord messages to avoid echo loops
                    raw_text = _extract_text(content) if content else ""
                    if raw_text and "📡 *[Discord" not in raw_text and not _should_skip(raw_text):
                        # Only include if it looks like a real human message
                        # (skip system injections, tool results, etc.)
                        if isinstance(content, str) or (
                            isinstance(content, list) and
                            all(isinstance(b, dict) and b.get("type") in ("text", "image")
                                for b in content)
                        ):
                            new_messages.append((msg_id, raw_text, AHMED_DISPLAY))
                    new_last_id = msg_id
                    continue

                # ── Assistant messages (agents) ──
                if role != "assistant":
                    new_last_id = msg_id
                    continue

                content = message.get("content", "")
                if isinstance(content, list):
                    has_text = any(
                        isinstance(b, dict) and b.get("type") == "text"
                        for b in content
                    )
                    if not has_text:
                        new_last_id = msg_id
                        continue

                text = _extract_text(content)
                if not _should_skip(text):
                    new_messages.append((msg_id, text, agent))
                new_last_id = msg_id

    except Exception as e:
        logger.warning("Error reading session %s for %s: %s", session_file.name[:8], agent, e)
        return

    if last_id is None:
        if new_last_id:
            _last_seen[state_key] = new_last_id
            _save_state()
        return

    for msg_id, text, speaker in new_messages:
        ok = _post_mirror(speaker, text, msg_id, override_channel=target_channel)
        if ok:
            logger.info("Mirrored %s message %s → #%s (%d chars)",
                        speaker, msg_id[:8], target_channel, len(text))
        _last_seen[state_key] = msg_id

    if new_messages:
        _save_state()


def _poll_agent(agent: str) -> None:
    """Poll one agent's active session for new assistant messages."""
    # First: check ALL sessions for this agent — route health group ones to command center
    all_sessions = _get_all_sessions(agent)
    health_group_files = [f for f in all_sessions if _is_health_group_session(f)]
    non_health_files = [f for f in all_sessions if not _is_health_group_session(f)]

    # Mirror health-group sessions to #nexus-command-center
    for sf in health_group_files:
        state_key = f"{agent}:health:{sf.stem}"
        _poll_session(agent, sf, state_key, COMMAND_CENTER_CHANNEL)

    # Mirror the most recent non-health session to the agent's own channel (original behavior)
    if non_health_files:
        active = max(non_health_files, key=lambda f: f.stat().st_mtime)
        _poll_session(agent, active, agent, AGENT_CHANNEL_MAP.get(agent, COMMAND_CENTER_CHANNEL))
    elif not health_group_files:
        # Fallback: original behavior for active session
        pass
    session_file = _get_active_session(agent)
    if not session_file:
        return

    # (body moved to _poll_session — this stub kept for reference)
    pass


def run_mirror_loop() -> None:
    """Main polling loop — runs forever at POLL_INTERVAL_S."""
    _load_state()
    logger.info(
        "Telegram mirror started — polling %d agents every %ds",
        len(AGENT_CHANNEL_MAP), POLL_INTERVAL_S,
    )

    while True:
        for agent in AGENT_CHANNEL_MAP:
            try:
                _poll_agent(agent)
            except Exception as e:
                logger.error("Unhandled error polling %s: %s", agent, e)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    # Load env from bridge .env
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    # Re-read config after env load
    BRIDGE_SECRET = os.environ.get("DISCORD_BRIDGE_SECRET", "")
    POLL_INTERVAL_S = int(os.environ.get("MIRROR_POLL_INTERVAL_S", "10"))

    run_mirror_loop()
