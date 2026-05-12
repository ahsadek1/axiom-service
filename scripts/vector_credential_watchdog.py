#!/usr/bin/env python3
"""
vector_credential_watchdog.py — External Dependency Expiry Tracking (Block 8)
===============================================================================
Prevention spec: External tokens/keys expire silently. Self-healing capability
disappears without warning. This watchdog tracks expiry and validates liveness.

Runs weekly (Monday 8 AM ET) via LaunchAgent.
Also run on-demand: python3 vector_credential_watchdog.py

Credentials tracked:
  - Alpaca API key (paper trading)
  - Anthropic API key
  - OpenAI API key
  - Gemini API key
  - DeepSeek API key
  - Railway API token (if configured)

Alert thresholds:
  - 30 days to expiry → warning to group
  - 14 days to expiry → escalate to Ahmed directly
  - Token invalid NOW → P0 alert immediately

Author: VECTOR 🔧 | Block 8 — RESILIENCE_SPEC_v2.0 | May 1, 2026
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import sys as _sys
_sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
from alert_client import send_alert as _send_alert

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ET = ZoneInfo("America/New_York")
NEXUS_DIR = Path("/Users/ahmedsadek/nexus")
CHRONICLE_DB = NEXUS_DIR / "data" / "chronicle.db"
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8736004775:AAG3v_7tcXk8SXh5whgpKRT3Dr3-C71VtQI"
)
AHMED_TELEGRAM_ID = "8573754783"
GROUP_CHAT_ID = "-1003579956463"
MESSAGE_BUS = "http://192.168.1.141:9999"

WARN_DAYS  = 30
ALERT_DAYS = 14

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] vector.credentials: %(message)s",
)
log = logging.getLogger("vector.credentials")


# ---------------------------------------------------------------------------
# Env loader — reads .env files from service directories
# ---------------------------------------------------------------------------

def _load_env(service: str) -> dict:
    """Load .env from a service directory. Returns {} if not found."""
    env_path = NEXUS_DIR / service / ".env"
    if not env_path.exists():
        return {}
    env = {}
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as e:
        log.warning("Failed to read %s: %s", env_path, e)
    return env


# ---------------------------------------------------------------------------
# API probes
# ---------------------------------------------------------------------------

def _probe_alpaca(key: str, secret: str, base_url: str) -> tuple[bool, str]:
    """Validate Alpaca key against /v2/account."""
    try:
        r = requests.get(
            f"{base_url.rstrip('/')}/v2/account",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=10,
        )
        if r.status_code == 200:
            return True, "ok"
        return False, f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return False, str(e)[:100]


def _probe_anthropic(key: str) -> tuple[bool, str]:
    """Validate Anthropic key against /v1/models."""
    try:
        r = requests.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout=10,
        )
        if r.status_code == 200:
            return True, "ok"
        return False, f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return False, str(e)[:100]


def _probe_openai(key: str) -> tuple[bool, str]:
    """Validate OpenAI key against /v1/models."""
    try:
        r = requests.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if r.status_code == 200:
            return True, "ok"
        return False, f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return False, str(e)[:100]


def _probe_gemini(key: str) -> tuple[bool, str]:
    """Validate Gemini key against models list endpoint."""
    try:
        r = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
            timeout=10,
        )
        if r.status_code == 200:
            return True, "ok"
        return False, f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return False, str(e)[:100]


def _probe_deepseek(key: str) -> tuple[bool, str]:
    """Validate DeepSeek key against /v1/models."""
    try:
        r = requests.get(
            "https://api.deepseek.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if r.status_code == 200:
            return True, "ok"
        return False, f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return False, str(e)[:100]


def _probe_railway(token: str) -> tuple[bool, str]:
    """Validate Railway API token against /graphql endpoint."""
    try:
        r = requests.post(
            "https://backboard.railway.app/graphql/v2",
            json={"query": "{ me { id email } }"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if "errors" in data:
                return False, f"GraphQL error: {data['errors'][0].get('message','unknown')[:100]}"
            return True, f"ok — user: {data.get('data',{}).get('me',{}).get('email','unknown')}"
        return False, f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return False, str(e)[:100]


# ---------------------------------------------------------------------------
# CHRONICLE — credential registry
# ---------------------------------------------------------------------------

def _ensure_credential_table() -> None:
    """Create credential_registry table in CHRONICLE if it doesn't exist."""
    conn = sqlite3.connect(str(CHRONICLE_DB), timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS credential_registry (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            credential   TEXT NOT NULL UNIQUE,
            last_valid   TEXT,
            last_checked TEXT,
            status       TEXT,   -- 'ok' | 'invalid' | 'expiring_soon' | 'unknown'
            detail       TEXT,
            updated_at   TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _update_chronicle(credential: str, status: str, detail: str) -> None:
    """Upsert credential status into CHRONICLE. Never raises."""
    try:
        _ensure_credential_table()
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(str(CHRONICLE_DB), timeout=10)
        conn.execute("""
            INSERT INTO credential_registry (credential, last_valid, last_checked, status, detail, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(credential) DO UPDATE SET
                last_valid   = CASE WHEN excluded.status='ok' THEN excluded.last_checked ELSE last_valid END,
                last_checked = excluded.last_checked,
                status       = excluded.status,
                detail       = excluded.detail,
                updated_at   = excluded.updated_at
        """, (
            credential,
            now if status == "ok" else None,
            now, status, detail, now,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("CHRONICLE update failed for %s: %s", credential, e)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

ALERT_BROKER = "http://192.168.1.141:9998"


def _send_alert_broker(message: str, level: str = "high", key: str = None) -> bool:
    """Route through Alert Broker (.141:9998). Returns True on success."""
    try:
        payload = {"source": "vector-credential-watchdog", "level": level, "message": message}
        if key:
            payload["key"] = key
        r = requests.post(f"{ALERT_BROKER}/alert", json=payload, timeout=5)
        return r.status_code == 200
    except Exception as e:
        log.warning("Alert Broker unreachable: %s", e)
        return False


def _send_telegram(message: str, target: str = GROUP_CHAT_ID) -> None:
    """Route through Alert Broker (target kept for signature compat)."""
    targets = ["ahmed"] if target == AHMED_TELEGRAM_ID else ["nexus_health_group"]
    _send_alert(
        source="vector-credential-watchdog",
        level="CRITICAL",
        title=message[:200],
        body=message[200:] if len(message) > 200 else "",
        targets=targets,
    )


def _notify_sovereign(message: str) -> None:
    try:
        requests.post(
            f"{MESSAGE_BUS}/send",
            json={"from": "vector", "to": "sovereign", "message": message},
            timeout=5,
        )
    except Exception as e:
        log.warning("Message bus notify failed: %s", e)


# ---------------------------------------------------------------------------
# Main validation run
# ---------------------------------------------------------------------------

def main() -> int:
    log.info("VECTOR credential watchdog starting")
    _ensure_credential_table()

    # Load credentials from service .env files
    alpha_env  = _load_env("alpha-execution")
    omni_env   = _load_env("omni")
    oracle_env = _load_env("oracle")

    credentials = []

    # Alpaca
    alpaca_key    = alpha_env.get("ALPACA_API_KEY", "")
    alpaca_secret = alpha_env.get("ALPACA_SECRET_KEY", "")
    alpaca_url    = alpha_env.get("ALPACA_PAPER_URL", "https://paper-api.alpaca.markets")
    if alpaca_key and alpaca_secret:
        credentials.append(("alpaca", lambda: _probe_alpaca(alpaca_key, alpaca_secret, alpaca_url), True))
    else:
        log.warning("Alpaca key not found in alpha-execution/.env")

    # Anthropic
    anthropic_key = omni_env.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        credentials.append(("anthropic", lambda k=anthropic_key: _probe_anthropic(k), True))
    else:
        log.warning("Anthropic key not found in omni/.env")

    # OpenAI
    openai_key = omni_env.get("OPENAI_API_KEY", "")
    if openai_key:
        credentials.append(("openai", lambda k=openai_key: _probe_openai(k), True))
    else:
        log.warning("OpenAI key not found in omni/.env")

    # Gemini
    gemini_key = omni_env.get("GEMINI_API_KEY", "")
    if gemini_key:
        credentials.append(("gemini", lambda k=gemini_key: _probe_gemini(k), False))
    else:
        log.warning("Gemini key not found in omni/.env")

    # DeepSeek
    deepseek_key = omni_env.get("DEEPSEEK_API_KEY", "")
    if deepseek_key:
        credentials.append(("deepseek", lambda k=deepseek_key: _probe_deepseek(k), False))
    else:
        log.warning("DeepSeek key not found in omni/.env")

    # Railway token (optional — may not exist)
    railway_token = os.environ.get("RAILWAY_API_TOKEN", "")
    if not railway_token:
        # Try reading from a dedicated secrets file
        railway_secrets = NEXUS_DIR / ".railway-secrets"
        if railway_secrets.exists():
            for line in railway_secrets.read_text().splitlines():
                if line.startswith("RAILWAY_API_TOKEN="):
                    railway_token = line.split("=", 1)[1].strip().strip('"')
    if railway_token:
        credentials.append(("railway", lambda t=railway_token: _probe_railway(t), True))
    else:
        log.info("Railway API token not configured — skipping Railway probe")
        _update_chronicle("railway", "unknown", "token not configured — add RAILWAY_API_TOKEN to environment")

    # Run probes
    failed_critical = []
    failed_warning  = []
    results         = []

    for name, probe_fn, is_critical in credentials:
        log.info("Probing %s...", name)
        try:
            ok, detail = probe_fn()
        except Exception as e:
            ok, detail = False, str(e)[:100]

        status = "ok" if ok else "invalid"
        _update_chronicle(name, status, detail)
        results.append((name, ok, detail, is_critical))

        if ok:
            log.info("✅ %s: valid", name)
        else:
            log.error("❌ %s: INVALID — %s", name, detail)
            if is_critical:
                failed_critical.append((name, detail))
            else:
                failed_warning.append((name, detail))

    # Report
    if failed_critical or failed_warning:
        lines = ["🔴 *VECTOR — CREDENTIAL WATCHDOG ALERT*\n"]

        if failed_critical:
            lines.append("*CRITICAL — trading capability at risk:*")
            for name, detail in failed_critical:
                lines.append(f"  ❌ `{name}`: {detail}")

        if failed_warning:
            lines.append("\n*WARNING — non-critical:*")
            for name, detail in failed_warning:
                lines.append(f"  ⚠️ `{name}`: {detail}")

        lines.append(f"\nTime: {datetime.now(ET).strftime('%H:%M ET')}")
        lines.append("Action required: rotate/renew affected credentials immediately.")
        alert_msg = "\n".join(lines)

        # Route through Alert Broker; fall back to direct Telegram
        level = "critical" if failed_critical else "high"
        broker_ok = _send_alert_broker(alert_msg, level=level, key="vector-credential-alert")
        if not broker_ok:
            _send_telegram(alert_msg, GROUP_CHAT_ID)

        if failed_critical:
            _send_telegram(alert_msg, AHMED_TELEGRAM_ID)  # Always direct DM Ahmed on critical
            _notify_sovereign(
                f"VECTOR CREDENTIAL ALERT: {', '.join(n for n,_ in failed_critical)} "
                f"credential(s) INVALID. Trading capability at risk. Immediate action required."
            )

        log.error("Credential watchdog: %d critical, %d warning failures",
                  len(failed_critical), len(failed_warning))
        return 1

    # All clear
    ok_names = [r[0] for r in results if r[1]]
    log.info("Credential watchdog: all clear — %s", ", ".join(ok_names))

    # Summary to group (weekly run — let them know it ran)
    summary_lines = ["✅ *VECTOR — Credential Watchdog*\n"]
    for name, ok, detail, _ in results:
        icon = "✅" if ok else "❌"
        summary_lines.append(f"{icon} `{name}`")
    summary_lines.append(f"\n_{datetime.now(ET).strftime('%A %b %d, %H:%M ET')}_")
    _send_telegram("\n".join(summary_lines), GROUP_CHAT_ID)

    return 0


if __name__ == "__main__":
    sys.exit(main())
