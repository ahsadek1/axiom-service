#!/usr/bin/env python3
"""
Telegram Group Reader — polls all 3 Nexus groups for agent messages
and posts them to the message bus (to:sovereign).

Runs on .141 (zero Telegram API conflict with SOVEREIGN's bot).
SOVEREIGN polls its inbox on .42 and reads everything.

No dependencies beyond stdlib.
"""
import json, logging, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("SOVEREIGN_BOT_TOKEN", "8611028745:AAGEwi0AcHSsrS1Y8u69wpWB86Jt75pbWyQ")
BUS_URL     = os.environ.get("BUS_URL", "http://localhost:9999/send")
STATE_FILE  = os.environ.get("STATE_FILE", "/tmp/telegram_group_reader_state.json")
POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_S", "30"))

GROUPS = {
    "-1003954790884": {"name": "NEXUS V1 HEALTH & INTEGRITY"},
    "-1003579956463": {"name": "NEXUS V2 HEALTH & INTEGRITY"},
    "-5130564161":    {"name": "SIGMA QUANT SYSTEMS"},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] tg-reader: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("/tmp/telegram_group_reader.log")],
)
logger = logging.getLogger("tg-reader")

OUR_BOT_ID = None


def tg_api(method, params=None):
    """Call Telegram Bot API."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(params).encode() if params else None
    try:
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        logger.error(f"TG API error ({method}): {e.code} {body}")
        return None
    except Exception as e:
        logger.error(f"TG API exception ({method}): {e}")
        return None


def get_our_id():
    """Discover our bot's user ID."""
    global OUR_BOT_ID
    if OUR_BOT_ID:
        return OUR_BOT_ID
    me = tg_api("getMe")
    if me and me.get("ok"):
        OUR_BOT_ID = me["result"]["id"]
        logger.info(f"Our bot ID: {OUR_BOT_ID}")
    return OUR_BOT_ID


def identify_agent_sender(sender):
    """Given a sender dict, return the agent name string."""
    uname = (sender.get("username") or "").replace("bot", "").replace("Bot", "").upper()
    fname = (sender.get("first_name") or "").upper()
    return uname or fname or "UNKNOWN_BOT"


def load_state():
    """Load per-chat offsets."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"offsets": {}, "total_messages": 0}


def save_state(state):
    """Persist state."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def post_to_bus(from_agent, message_text, group_name):
    """Post captured message to SOVEREIGN's inbox via message bus."""
    payload = json.dumps({
        "from": from_agent.lower(),
        "to": "sovereign",
        "message": f"[{group_name}] {from_agent}: {message_text[:2000]}",
    }).encode()
    try:
        req = urllib.request.Request(BUS_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True
    except Exception as e:
        logger.warning(f"Bus post failed: {e}")
        return None


def poll_groups(state):
    """Poll all groups for new messages."""
    offsets = state.get("offsets", {})
    total = state.get("total_messages", 0)
    our_id = get_our_id()
    
    for chat_id in offsets:
        offset = offsets[chat_id]
        
        result = tg_api("getUpdates", {
            "offset": offset,
            "timeout": 2,
            "allowed_updates": ["message"],
            "limit": 50,
        })
        
        if not result or not result.get("ok"):
            continue
        
        updates = result.get("result", [])
        max_offset = offset
        group_name = GROUPS[chat_id]["name"]
        
        for update in updates:
            update_id = update.get("update_id", 0)
            max_offset = max(max_offset, update_id + 1)
            
            message = update.get("message", {})
            msg_chat_id = str(message.get("chat", {}).get("id", ""))
            
            if msg_chat_id != chat_id:
                continue
            
            sender = message.get("from", {})
            sender_id = sender.get("id", 0)
            text = message.get("text", "") or message.get("caption", "") or ""
            
            # Skip our own messages
            if sender_id == our_id:
                continue
            
            # Skip empty/pings
            if not text or text.strip() in ("NO_REPLY", "HEARTBEAT_OK", ""):
                continue
            
            agent_name = identify_agent_sender(sender)
            logger.info(f"[{group_name}] {agent_name}: {text[:120]}")
            
            post_to_bus(agent_name, text, group_name)
            total += 1
        
        if max_offset > offset:
            offsets[chat_id] = max_offset
    
    state["offsets"] = offsets
    state["total_messages"] = total
    return state


def main():
    logger.info("Telegram Group Reader starting...")
    get_our_id()
    
    state = load_state()
    
    # Initialize offsets if empty
    for chat_id in GROUPS:
        if chat_id not in state.get("offsets", {}):
            state.setdefault("offsets", {})[chat_id] = 0
    
    # First poll
    state = poll_groups(state)
    save_state(state)
    logger.info(f"Initial poll complete. Total captured: {state['total_messages']}")
    
    # Continuous loop
    logger.info(f"Entering continuous poll loop ({POLL_INTERVAL_S}s)...")
    
    while True:
        try:
            state = poll_groups(state)
            save_state(state)
        except Exception as e:
            logger.error(f"Poll error: {e}", exc_info=True)
        
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
