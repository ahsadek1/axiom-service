"""
registry.py — Multi-System Guardian Registry
=============================================
One guardian per Alpaca account. All systems registered here.
Single source of truth for all guardian instances.

Systems:
  THESIS      — Options, PA3M7UXKKFHV, $200K
  ALPHA       — V2 options/equity, PA3L7BB6PR35, $366K
  PRIME       — Swing options, PA36ZW2DI965, $200K
  SQS-ATG     — ATG trading
  SQS-ATM     — ATM trading
  SQS-0DTE    — 0DTE trading
  SQS-INTRADAY — Intraday trading
"""
from __future__ import annotations
import logging
import os
import threading
from typing import Optional

import requests as _requests

from .guardian import AlpacaGuardian

log = logging.getLogger("nexus.alpaca.registry")

_registry: dict[str, AlpacaGuardian] = {}
_registry_lock = threading.Lock()


def _make_notify(bot_token: str, chat_id: str):
    """Create Telegram notification function for a guardian."""
    def _notify(msg: str) -> None:
        if not bot_token or not chat_id:
            return
        try:
            _requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id":    chat_id,
                    "text":       msg[:4000],
                    "parse_mode": "HTML",
                },
                timeout=5,
            )
        except Exception:
            pass
    return _notify


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def build_all_guardians() -> dict[str, AlpacaGuardian]:
    """
    Instantiate guardians for all known systems.
    Called once at system startup by Autonomous Action Engine.
    """
    TG_TOKEN  = _env("TELEGRAM_BOT_TOKEN")
    HEALTH_GRP = _env("NEXUS_HEALTH_GROUP_ID", "-5241272802")
    AHMED_ID   = _env("AHMED_CHAT_ID", "8573754783")

    # Use health group for system alerts, Ahmed directly for P0
    health_notify = _make_notify(TG_TOKEN, HEALTH_GRP)
    ahmed_notify  = _make_notify(TG_TOKEN, AHMED_ID)

    def p0_notify(msg: str) -> None:
        """P0 errors go to both health group and Ahmed directly."""
        health_notify(msg)
        ahmed_notify(msg)

    systems = [
        {
            "system_id":  "THESIS",
            "api_key":    _env("THESIS_ALPACA_KEY",   _env("ALPACA_API_KEY",    "PKUYQBMBAIZC34BUJSSAG64EFA")),
            "secret_key": _env("THESIS_ALPACA_SECRET", _env("ALPACA_SECRET_KEY", "")),
            "db_path":    _env("THESIS_POSITIONS_DB",  "/Users/ahmedsadek/nexus/data/thesis_positions.db"),
            "positions_table": "positions",
            "status_column":   "status",
            "ticker_column":   "ticker",
            "open_value":      "OPEN",
            "notify_fn":  health_notify,
        },
        {
            "system_id":  "ALPHA",
            "api_key":    _env("ALPHA_ALPACA_KEY",   "PKGGXWNZTITUTZUNVK2QBLZJQL"),
            "secret_key": _env("ALPHA_ALPACA_SECRET", ""),
            "db_path":    _env("ALPHA_DB_PATH",       "/Users/ahmedsadek/nexus/data/alpha_execution.db"),
            "positions_table": "positions",
            "status_column":   "status",
            "ticker_column":   "ticker",
            "open_value":      "open",
            "notify_fn":  health_notify,
        },
        {
            "system_id":  "PRIME",
            "api_key":    _env("PRIME_ALPACA_KEY",   "PKCRMH6WDBDLQGM3WUDL5KZVFB"),
            "secret_key": _env("PRIME_ALPACA_SECRET", "9CE8nmBghar3dkNuWPQhkEkkbmXUF4YANu5dGfKBXUSe"),
            "db_path":    _env("PRIME_EXEC_DB_PATH",  "/Users/ahmedsadek/nexus/data/prime_execution.db"),
            "positions_table": "positions",
            "status_column":   "status",
            "ticker_column":   "ticker",
            "open_value":      "open",
            "notify_fn":  health_notify,
        },
    ]

    # SQS systems — Sigma Quant Systems (each has dedicated Alpaca account)
    SQS_TG = _make_notify(_env("ATG_TELEGRAM_TOKEN", TG_TOKEN),
                          _env("ATG_TELEGRAM_CHAT_ID", "-5130564161"))
    sqs_systems = [
        {
            "system_id":       "ATG_SWING",
            "api_key":         "PKIBQ3OUYO6E7HLCRENNIE6CS4",
            "secret_key":      "HX1djCz3Yg5skh6S1r5UW8FXJfuvyN654ycwsYPhUeqk",
            "db_path":         "/Users/ahmedsadek/sqs/atg-swing/data/atg-swing.db",
            "positions_table": "swing_positions",
            "status_column":   "status",
            "ticker_column":   "symbol",
            "open_value":      "OPEN",
            "notify_fn":       SQS_TG,
        },
        {
            "system_id":       "ATG_INTRADAY",
            "api_key":         "PKQJZGD2EFDLVBROZ7E37PS3N4",
            "secret_key":      "HCCps8mX51UVUxD2S5reSJWs63nNjs9VDe336pRxKRhy",
            "db_path":         "/Users/ahmedsadek/sqs/atg-intraday/data/atg_intraday.db",
            "positions_table": "positions",
            "status_column":   "status",
            "ticker_column":   "symbol",
            "open_value":      "OPEN",
            "notify_fn":       SQS_TG,
        },
        {
            "system_id":       "ATM_MULTIWEEK",
            "api_key":         "PKVUTVRA6FA276ZZYAIE5KOFKC",
            "secret_key":      "78to1tA3LC7vZNVH5fLRTdpGXdyDnq4tVjNJNCSSYSN7",
            "db_path":         "/Users/ahmedsadek/sqs/atm-multi-week/data/atm-multi-week.db",
            "positions_table": "trades",
            "status_column":   "status",
            "ticker_column":   "symbol",
            "open_value":      "open",
            "notify_fn":       SQS_TG,
        },
        {
            "system_id":       "ATM_0DTE",
            "api_key":         "PKVUTVRA6FA276ZZYAIE5KOFKC",
            "secret_key":      "78to1tA3LC7vZNVH5fLRTdpGXdyDnq4tVjNJNCSSYSN7",
            "db_path":         "/Users/ahmedsadek/sqs/atm-0dte/data/atm_0dte.db",
            "positions_table": "positions",
            "status_column":   "status",
            "ticker_column":   "symbol",
            "open_value":      "open",
            "notify_fn":       SQS_TG,
        },
    ]
    systems.extend(sqs_systems)
    log.info("SQS systems registered: ATG_SWING, ATG_INTRADAY, ATM_MULTIWEEK, ATM_0DTE")

    with _registry_lock:
        for cfg in systems:
            system_id = cfg["system_id"]
            if not cfg.get("api_key") or not cfg.get("secret_key"):
                log.warning("Skipping %s — missing credentials", system_id)
                continue
            try:
                guardian = AlpacaGuardian(
                    system_id        = system_id,
                    api_key          = cfg["api_key"],
                    secret_key       = cfg["secret_key"],
                    db_path          = cfg.get("db_path"),
                    notify_fn        = cfg["notify_fn"],
                    positions_table  = cfg.get("positions_table", "positions"),
                    status_column    = cfg.get("status_column",   "status"),
                    ticker_column    = cfg.get("ticker_column",   "ticker"),
                    open_value       = cfg.get("open_value",      "OPEN"),
                )
                _registry[system_id] = guardian
                log.info("Guardian registered: %s", system_id)
            except Exception as exc:
                log.error("Failed to register guardian %s: %s", system_id, exc)

    return _registry


def start_all_guardians() -> dict[str, AlpacaGuardian]:
    """Build and start all guardians. Called at boot."""
    guardians = build_all_guardians()
    for system_id, guardian in guardians.items():
        try:
            guardian.start()
            log.info("Guardian started: %s (state=%s)", system_id, guardian.state)
        except Exception as exc:
            log.error("Guardian start failed: %s: %s", system_id, exc)
    return guardians


def get_guardian(system_id: str) -> Optional[AlpacaGuardian]:
    """Get guardian for a specific system."""
    with _registry_lock:
        return _registry.get(system_id)


def get_all_status() -> dict:
    """Get status of all guardians — for health mesh API."""
    from datetime import datetime, timezone
    with _registry_lock:
        return {
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "guardians":  {
                sid: g.status()
                for sid, g in _registry.items()
            },
            "total":      len(_registry),
            "healthy":    sum(1 for g in _registry.values() if g.is_healthy),
            "can_trade":  sum(1 for g in _registry.values() if g.can_trade),
        }


def run_all_ghost_scans() -> dict:
    """Run ghost scan on all systems. Called manually or on schedule."""
    results = {}
    with _registry_lock:
        for system_id, guardian in _registry.items():
            try:
                result = guardian.run_ghost_scan()
                results[system_id] = result
            except Exception as exc:
                results[system_id] = {"error": str(exc)}
    return results
