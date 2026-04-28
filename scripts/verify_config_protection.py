#!/usr/bin/env python3
"""
verify_config_protection.py — openclaw.json immutability watchdog.

Verifies that openclaw.json has the macOS uchg (user-immutable) flag set.
If the flag has been removed (tampered with or accidentally cleared), re-sets it
and sends a Tier-1 alert to Telegram.

Runs as part of Cipher's blocker sweep (Phase 2 — deep scan).
Can also be run standalone: python3 verify_config_protection.py

Exit codes:
  0 — flag was already set (or was re-set successfully)
  1 — could not inspect or re-apply the flag
"""
import subprocess
import sys
import os

PROTECTED_FILE = os.path.expanduser("~/.openclaw/openclaw.json")


def _flag_is_set(path: str) -> bool:
    """Return True if the uchg (user-immutable) flag is active on path."""
    try:
        result = subprocess.run(
            ["ls", "-lO", path],
            capture_output=True, text=True, timeout=5,
        )
        return "uchg" in result.stdout
    except Exception:
        return False


def _set_flag(path: str) -> bool:
    """Apply uchg flag. Returns True on success."""
    try:
        subprocess.run(
            ["chflags", "uchg", path],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return True
    except Exception:
        return False


def main() -> int:
    if not os.path.exists(PROTECTED_FILE):
        print(f"[config-guard] WARNING: {PROTECTED_FILE} does not exist — skipping check")
        return 0

    if _flag_is_set(PROTECTED_FILE):
        print(f"[config-guard] ✅ openclaw.json is immutable (uchg flag set)")
        return 0

    # Flag was removed — re-apply and alert
    print(f"[config-guard] ⚠️  TAMPER DETECTED: uchg flag missing on openclaw.json — re-applying")
    success = _set_flag(PROTECTED_FILE)

    if success:
        print(f"[config-guard] ✅ uchg flag re-applied successfully")
        # Alert via Guardian Angel / Telegram if available
        try:
            import requests
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "8573754783")
            if token:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": (
                            "🔴 *CONFIG PROTECTION ALERT*\n\n"
                            "`openclaw.json` immutability flag was removed and has been re-applied.\n"
                            "Investigate which process cleared the `uchg` flag.\n\n"
                            "_April 10 incident: direct write caused 3.5h gateway outage._"
                        ),
                        "parse_mode": "Markdown",
                    },
                    timeout=5,
                )
        except Exception:
            pass  # Alert failure should not block the re-protection
        return 0
    else:
        print(f"[config-guard] ❌ FAILED to re-apply uchg flag — manual intervention required")
        return 1


if __name__ == "__main__":
    sys.exit(main())
