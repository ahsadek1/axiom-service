"""
agent_startup.py — Nexus Agent Startup Protocol

Every agent runs this at session start to load governance mandates
from CHRONICLE into working context.

Usage (add to agent's first-turn processing):
    from shared.agent_startup import load_governance_context
    context = load_governance_context(agent_name="Cipher")
    # context["precision_mandate"] contains the full mandate text
    # context["mandate_version"] is the version string
    # context["loaded_at"] is the load timestamp

If CHRONICLE is unreachable, this returns the local fallback text
from the agent's workspace CODE_PRECISION_STANDARD.md if it exists.
It never blocks startup.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("nexus.agent_startup")

# Canonical workspace root pattern for fallback
_WORKSPACE_PATTERN = "/Users/ahmedsadek/.openclaw/workspace-{agent_lower}"


def load_governance_context(
    agent_name: str,
    session_key: Optional[str] = None,
) -> dict:
    """
    Load all governance mandates from CHRONICLE for this agent.

    Attempts CHRONICLE first. Falls back to local workspace file if
    CHRONICLE is unreachable. Never raises — always returns a dict.

    Args:
        agent_name:  Canonical agent name (e.g. "Cipher", "GENESIS").
        session_key: Optional session identifier for audit trail.

    Returns:
        Dict with:
          precision_mandate (str): Full mandate text
          mandate_version   (str): Version loaded (e.g. "1.0")
          mandate_source    (str): "chronicle" or "local_fallback"
          loaded_at         (str): ISO timestamp
          error             (str | None): Error message if fallback used
    """
    result = {
        "precision_mandate": "",
        "mandate_version":   "unknown",
        "mandate_source":    "none",
        "loaded_at":         datetime.now(timezone.utc).isoformat(),
        "error":             None,
    }

    # ── Primary: Load from CHRONICLE ─────────────────────────────────────────
    try:
        import sys
        _shared = os.path.join(os.path.dirname(__file__))
        if _shared not in sys.path:
            sys.path.insert(0, _shared)

        from chronicle_reader import load_mandate
        mandate = load_mandate(
            "precision_mandate_v1",
            agent=agent_name,
            session_key=session_key,
        )
        if mandate:
            result["precision_mandate"] = mandate["content"]
            result["mandate_version"]   = mandate["version"]
            result["mandate_source"]    = "chronicle"
            logger.info(
                "Agent startup: %s loaded precision_mandate_v1 v%s from CHRONICLE",
                agent_name, mandate["version"],
            )
            return result
        else:
            result["error"] = "CHRONICLE returned no document for precision_mandate_v1"
    except Exception as e:
        result["error"] = f"CHRONICLE unavailable: {e}"
        logger.warning("Agent startup: CHRONICLE load failed for %s: %s", agent_name, e)

    # ── Fallback: local workspace file ────────────────────────────────────────
    workspace = _WORKSPACE_PATTERN.format(agent_lower=agent_name.lower())
    local_path = os.path.join(workspace, "CODE_PRECISION_STANDARD.md")
    if os.path.exists(local_path):
        try:
            with open(local_path) as f:
                content = f.read()
            result["precision_mandate"] = content
            result["mandate_version"]   = "local"
            result["mandate_source"]    = "local_fallback"
            logger.warning(
                "Agent startup: %s using LOCAL fallback for precision mandate "
                "(CHRONICLE unavailable). Ensure CHRONICLE is reachable.",
                agent_name,
            )
            return result
        except Exception as e:
            result["error"] = f"Local fallback read failed: {e}"

    logger.error(
        "Agent startup: %s could not load precision mandate from CHRONICLE or local file. "
        "Governance context is MISSING for this session.",
        agent_name,
    )
    return result


def print_startup_banner(agent_name: str, context: dict) -> None:
    """Print a concise startup banner confirming mandate load status."""
    source  = context.get("mandate_source", "none")
    version = context.get("mandate_version", "unknown")
    error   = context.get("error")

    if source == "chronicle":
        print(f"[{agent_name}] ✅ PRECISION MANDATE v{version} loaded from CHRONICLE")
    elif source == "local_fallback":
        print(f"[{agent_name}] ⚠️  PRECISION MANDATE loaded from LOCAL FALLBACK (CHRONICLE unreachable)")
        if error:
            print(f"[{agent_name}]    Error: {error}")
    else:
        print(f"[{agent_name}] ❌ PRECISION MANDATE NOT LOADED — governance context missing")
        if error:
            print(f"[{agent_name}]    Error: {error}")
