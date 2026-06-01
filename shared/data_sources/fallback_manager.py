"""
fallback_manager.py — Automatic Fallback & Recovery Manager
============================================================
Manages active data source for each category.
When primary fails: switch to secondary immediately.
When primary recovers: switch back automatically.
Notifies all systems of active source changes.
"""
from __future__ import annotations
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional
import requests
from .registry import (
    ALL_SOURCES, DataSource, SourceStatus, SourceTier,
    SourceCategory, get_by_category,
)

log = logging.getLogger("nexus.data_sources.fallback")
DB_PATH  = os.getenv("PRIME_V2_DB", "/Users/ahmedsadek/nexus/data/prime_v2.db")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.getenv("AHMED_CHAT_ID", "8573754783")

# Active source per category — starts as primary
_active_sources: dict[str, str] = {}  # category -> source_id


def init_active_sources() -> None:
    """Initialize active source to primary for each category."""
    global _active_sources
    for cat in SourceCategory:
        sources = sorted(
            get_by_category(cat),
            key=lambda s: [SourceTier.PRIMARY, SourceTier.SECONDARY,
                          SourceTier.EMERGENCY].index(s.tier)
        )
        if sources:
            _active_sources[cat.value] = sources[0].source_id
            log.info("Active source [%s]: %s", cat.value, sources[0].source_id)


def get_active_source_id(category: SourceCategory) -> Optional[str]:
    return _active_sources.get(category.value)


def get_active_source(category: SourceCategory) -> Optional[DataSource]:
    sid = get_active_source_id(category)
    return ALL_SOURCES.get(sid) if sid else None


def _notify(msg: str) -> None:
    if not TG_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg[:4000], "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


def _find_best_fallback(failed_source: DataSource) -> Optional[DataSource]:
    """Find next available source in the fallback chain."""
    # Follow the chain
    current = failed_source
    visited = set()
    while current.fallback_source_id:
        if current.fallback_source_id in visited:
            break
        visited.add(current.fallback_source_id)
        next_source = ALL_SOURCES.get(current.fallback_source_id)
        if not next_source:
            break
        if next_source.status != SourceStatus.FAILED:
            return next_source
        current = next_source

    # If chain exhausted, try any non-failed source in category
    sources = get_by_category(failed_source.category)
    tier_order = [SourceTier.SECONDARY, SourceTier.EMERGENCY, SourceTier.PRIMARY]
    for tier in tier_order:
        for s in sources:
            if s.source_id != failed_source.source_id and s.tier == tier:
                if s.status != SourceStatus.FAILED:
                    return s
    return None


def on_source_failed(source: DataSource) -> None:
    """
    Called by SourceMonitor when a source transitions to FAILED.
    Immediately switches to best available fallback.
    """
    cat = source.category.value
    current_active = _active_sources.get(cat)

    # Only act if the failed source is currently active
    if current_active != source.source_id:
        log.info("Source %s failed but not active — no switch needed", source.source_id)
        return

    fallback = _find_best_fallback(source)
    if fallback:
        _active_sources[cat] = fallback.source_id
        log.warning("FALLBACK: %s → %s for category %s",
                    source.source_id, fallback.source_id, cat)

        msg = (
            f"🔄 <b>FALLBACK DEPLOYED</b>\n"
            f"Category: {cat}\n"
            f"Failed: {source.name}\n"
            f"Now using: {fallback.name} ({fallback.tier.value})\n"
            f"All systems automatically redirected."
        )
        _notify(msg)
        _log_switch(source.source_id, fallback.source_id, cat, "FAILOVER")

    else:
        log.error("NO FALLBACK AVAILABLE for %s category %s", source.source_id, cat)
        msg = (
            f"🚨 <b>NO FALLBACK AVAILABLE</b>\n"
            f"Category: {cat}\n"
            f"All sources failed!\n"
            f"Affected systems will use emergency static defaults."
        )
        _notify(msg)


def on_source_recovered(source: DataSource) -> None:
    """
    Called by SourceMonitor when a source transitions to HEALTHY.
    Switches back to primary if this source is primary tier.
    """
    cat = source.category.value

    # Only switch back if this is the primary source
    if source.tier != SourceTier.PRIMARY:
        log.info("Secondary/emergency %s recovered — not switching back", source.source_id)
        return

    current_active = _active_sources.get(cat)
    if current_active == source.source_id:
        log.info("Primary %s recovered and already active", source.source_id)
        return

    # Switch back to primary
    prev = ALL_SOURCES.get(current_active)
    _active_sources[cat] = source.source_id
    log.info("RESTORED: %s → %s for category %s",
             current_active, source.source_id, cat)

    msg = (
        f"✅ <b>PRIMARY RESTORED</b>\n"
        f"Category: {cat}\n"
        f"Restored: {source.name}\n"
        f"Previous fallback: {prev.name if prev else 'unknown'}\n"
        f"All systems switched back to primary."
    )
    _notify(msg)
    _log_switch(current_active, source.source_id, cat, "RESTORE")


def _log_switch(from_id: str, to_id: str, category: str, event_type: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS data_source_switches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category    TEXT,
                from_source TEXT,
                to_source   TEXT,
                event_type  TEXT,
                ts          REAL,
                created_at  TEXT
            )
        """)
        conn.execute("""
            INSERT INTO data_source_switches
            (category, from_source, to_source, event_type, ts, created_at)
            VALUES (?,?,?,?,?,?)
        """, (category, from_id, to_id, event_type, time.time(), now))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.debug("Switch log failed: %s", exc)


def get_active_summary() -> dict:
    """Get summary of all active sources."""
    return {
        cat: {
            "active_source_id": sid,
            "name": ALL_SOURCES[sid].name if sid in ALL_SOURCES else "unknown",
            "tier": ALL_SOURCES[sid].tier.value if sid in ALL_SOURCES else "unknown",
            "status": ALL_SOURCES[sid].status.value if sid in ALL_SOURCES else "unknown",
        }
        for cat, sid in _active_sources.items()
    }
