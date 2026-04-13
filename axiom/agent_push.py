"""
agent_push.py — Axiom Agent Pool Push

Pushes updated pool to all 3 agent webhooks simultaneously on every Tier 2 refresh.
Parallel execution — total push time = slowest agent, not sum of all.
Fail-closed: agent failure is logged and tracked. Never blocks the pool refresh.
3 consecutive failures → SERVICE DOWN alert to Ahmed.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import requests

from database import log_agent_push, update_agent_health, get_agent_health
from telegram import send_service_down_alert

logger = logging.getLogger("axiom.agent_push")

PUSH_TIMEOUT_SECONDS    = 5
DOWN_THRESHOLD          = 3    # consecutive failures before DOWN alert


def push_pool_to_agents(
    pool_payload: dict,
    agent_webhooks: dict[str, str],
    db_path: str,
    bot_token: str,
    chat_id: str,
    window_id: str,
) -> dict[str, str]:
    """
    Push the updated pool to all agent webhooks in parallel.

    Args:
        pool_payload:   Complete pool payload dict (pool, regime, window_id, etc.).
        agent_webhooks: Dict mapping agent name to webhook URL.
        db_path:        Path to Axiom SQLite database (for push log).
        bot_token:      Telegram bot token (for DOWN alerts).
        chat_id:        Ahmed's Telegram chat ID.
        window_id:      Current 15-min window ID.

    Returns:
        Dict mapping agent name to 'success', 'timeout', or 'error'.
    """
    results: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(agent_webhooks)) as executor:
        future_to_agent = {
            executor.submit(
                _push_to_agent,
                agent_name,
                url,
                pool_payload,
            ): agent_name
            for agent_name, url in agent_webhooks.items()
        }

        for future in as_completed(future_to_agent):
            agent_name = future_to_agent[future]
            try:
                status, response_ms = future.result(timeout=PUSH_TIMEOUT_SECONDS + 2)
            except Exception as e:
                logger.warning("Agent push future failed for %s: %s", agent_name, e)
                status      = "error"
                response_ms = None

            results[agent_name] = status

            # Log to DB
            try:
                log_agent_push(db_path, window_id, agent_name, status, response_ms)
            except Exception as e:
                logger.warning("Failed to log agent push for %s: %s", agent_name, e)

            # Update health tracker
            success      = status == "success"
            consec_fails = update_agent_health(db_path, agent_name, success)

            if not success:
                logger.warning(
                    "Agent push FAILED for %s (status=%s, consecutive=%d)",
                    agent_name,
                    status,
                    consec_fails,
                )

                # Send SERVICE DOWN alert on threshold breach
                if consec_fails == DOWN_THRESHOLD:
                    agent_health = get_agent_health(db_path)
                    last_success = agent_health.get(agent_name, {}).get("last_success_at")
                    send_service_down_alert(
                        bot_token   = bot_token,
                        chat_id     = chat_id,
                        service_name= f"{agent_name} Agent",
                        consecutive_failures=consec_fails,
                        last_success= last_success,
                    )
            else:
                logger.info("Agent push SUCCESS for %s (%dms)", agent_name, response_ms or 0)

    return results


def _push_to_agent(
    agent_name: str,
    url: str,
    payload: dict,
) -> tuple[str, Optional[int]]:
    """
    Push pool payload to a single agent webhook.

    Args:
        agent_name: Name of the agent (for logging).
        url:        Agent webhook URL.
        payload:    Pool payload dict to POST.

    Returns:
        Tuple of (status, response_ms).
        status: 'success', 'timeout', or 'error'.
        response_ms: Response time in ms or None.
    """
    start_ms = int(time.time() * 1000)
    try:
        resp = requests.post(
            url,
            json=payload,
            timeout=PUSH_TIMEOUT_SECONDS,
            headers={"Content-Type": "application/json"},
        )
        response_ms = int(time.time() * 1000) - start_ms

        if resp.status_code in (200, 201, 202):
            return "success", response_ms

        logger.warning(
            "Agent %s returned non-2xx status %d (%dms)",
            agent_name,
            resp.status_code,
            response_ms,
        )
        return "error", response_ms

    except requests.exceptions.Timeout:
        response_ms = int(time.time() * 1000) - start_ms
        logger.warning("Agent %s timed out after %dms", agent_name, response_ms)
        return "timeout", response_ms

    except Exception as e:
        response_ms = int(time.time() * 1000) - start_ms
        logger.warning("Agent %s push error: %s (%dms)", agent_name, e, response_ms)
        return "error", response_ms
