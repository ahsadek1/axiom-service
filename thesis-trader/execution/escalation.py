"""
escalation.py — Alert & Escalation System
===========================================
Converts silent failures into explicit escalations.

Levels:
  INFO    → CHRONICLE log only
  WARN    → Telegram notification
  ALERT   → Escalate to SOVEREIGN
  CRITICAL → Halt system + escalate

Integration: Sends to CHRONICLE + Telegram + optionally SOVEREIGN
"""
import logging
import os
import asyncio
from typing import Optional
from datetime import datetime, timezone
from enum import Enum
import httpx
import requests

log = logging.getLogger("thesis.escalation")


class EscalationLevel(Enum):
    """Escalation severity levels."""
    INFO = "INFO"
    WARN = "WARN"
    ALERT = "ALERT"
    CRITICAL = "CRITICAL"


# Configuration
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("THESIS_CHAT_ID", os.getenv("AHMED_CHAT_ID", ""))
CHRONICLE_ENDPOINT = os.getenv("CHRONICLE_ENDPOINT", "http://localhost:8020/log")
SOVEREIGN_ENDPOINT = os.getenv("SOVEREIGN_ENDPOINT", "http://192.168.1.141:9999/escalate")


async def escalate(
    level: EscalationLevel,
    title: str,
    message: str,
    position_id: Optional[str] = None,
    component: Optional[str] = None,
) -> bool:
    """
    Escalate an issue to appropriate channels.
    
    Args:
        level: INFO | WARN | ALERT | CRITICAL
        title: short title
        message: detailed message
        position_id: related position (optional)
        component: which component failed (optional)
    
    Returns:
        True if escalation succeeded, False otherwise
    
    Routing:
        INFO → CHRONICLE only
        WARN → CHRONICLE + Telegram
        ALERT → CHRONICLE + Telegram + SOVEREIGN
        CRITICAL → CHRONICLE + Telegram + SOVEREIGN (halt system)
    """
    
    timestamp = datetime.now(timezone.utc).isoformat()
    
    # Build escalation payload
    payload = {
        "system": "THESIS",
        "level": level.value,
        "timestamp": timestamp,
        "title": title,
        "message": message,
        "position_id": position_id,
        "component": component,
    }
    
    try:
        # Always log to CHRONICLE
        await _log_to_chronicle(payload)
        
        # Based on level, escalate further
        if level == EscalationLevel.INFO:
            log.info("[INFO] %s — %s", title, message)
            return True
        
        elif level == EscalationLevel.WARN:
            log.warning("[WARN] %s — %s", title, message)
            await _notify_telegram(level, title, message, position_id)
            return True
        
        elif level == EscalationLevel.ALERT:
            log.error("[ALERT] %s — %s", title, message)
            await _notify_telegram(level, title, message, position_id)
            await _escalate_to_sovereign(payload)
            return True
        
        elif level == EscalationLevel.CRITICAL:
            log.critical("[CRITICAL] %s — %s", title, message)
            await _notify_telegram(level, title, message, position_id)
            await _escalate_to_sovereign(payload)
            # Signal system halt (caller must check)
            return True
    
    except Exception as exc:
        log.error("Escalation failed: %s", exc)
        return False


async def _log_to_chronicle(payload: dict) -> bool:
    """Log event to CHRONICLE."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(CHRONICLE_ENDPOINT, json=payload)
            if r.status_code == 200:
                log.debug("Logged to CHRONICLE: %s", payload["title"])
                return True
    except Exception as exc:
        log.warning("Failed to log to CHRONICLE: %s", exc)
    return False


async def _notify_telegram(
    level: EscalationLevel,
    title: str,
    message: str,
    position_id: Optional[str] = None,
) -> bool:
    """Send Telegram notification."""
    if not TG_TOKEN or not TG_CHAT:
        return False
    
    try:
        # Format message based on level
        emoji = {
            EscalationLevel.WARN: "⚠️",
            EscalationLevel.ALERT: "🚨",
            EscalationLevel.CRITICAL: "🔴",
        }.get(level, "ℹ️")
        
        text = f"{emoji} <b>THESIS {level.value}</b>\n"
        text += f"<b>{title}</b>\n"
        text += f"{message}\n"
        if position_id:
            text += f"Position: <code>{position_id}</code>"
        
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
        
        if r.status_code == 200:
            log.debug("Telegram notification sent: %s", title)
            return True
    
    except Exception as exc:
        log.warning("Failed to send Telegram notification: %s", exc)
    
    return False


async def _escalate_to_sovereign(payload: dict) -> bool:
    """Escalate to SOVEREIGN for intervention."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(SOVEREIGN_ENDPOINT, json=payload)
            if r.status_code == 200:
                log.info("Escalated to SOVEREIGN: %s", payload["title"])
                return True
    except Exception as exc:
        log.warning("Failed to escalate to SOVEREIGN: %s", exc)
    
    return False


# Convenience functions
async def escalate_info(title: str, message: str, position_id: str = None) -> bool:
    return await escalate(EscalationLevel.INFO, title, message, position_id)


async def escalate_warn(title: str, message: str, position_id: str = None) -> bool:
    return await escalate(EscalationLevel.WARN, title, message, position_id)


async def escalate_alert(title: str, message: str, position_id: str = None) -> bool:
    return await escalate(EscalationLevel.ALERT, title, message, position_id)


async def escalate_critical(title: str, message: str, position_id: str = None) -> bool:
    return await escalate(EscalationLevel.CRITICAL, title, message, position_id)


# Sync wrapper (for use in sync contexts)
def escalate_sync(
    level: str,
    title: str,
    message: str,
    position_id: Optional[str] = None,
) -> bool:
    """Synchronous wrapper. Call in sync contexts."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    level_enum = EscalationLevel[level.upper()]
    return loop.run_until_complete(escalate(level_enum, title, message, position_id))
