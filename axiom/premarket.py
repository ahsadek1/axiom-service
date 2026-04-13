"""
premarket.py — Axiom Pre-Market Brief Generator

Generates and sends the daily 8:45 AM pre-market risk brief to Ahmed via Telegram.
Uses DeepSeek for brief generation. Fail-safe: always sends something, even on error.
"""

import logging
import os
from datetime import datetime, timezone

import requests

from telegram import send_message

logger = logging.getLogger("axiom.premarket")

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"


def generate_premarket_brief(
    vix: float,
    regime_classification: str,
    strategy_bias: str,
    pool: list[str],
    bot_token: str,
    chat_id: str,
    deepseek_api_key: str,
) -> dict:
    """
    Generate and send the daily pre-market risk brief via Telegram.

    Calls DeepSeek for macro/regime context. Sends formatted brief to Ahmed.
    On DeepSeek failure, sends a minimal factual brief with VIX and regime only.

    Args:
        vix:                  Current VIX level.
        regime_classification: Regime string (NORMAL, ELEVATED, etc.).
        strategy_bias:        Human-readable strategy guidance.
        pool:                 List of today's candidate tickers.
        bot_token:            Telegram bot token.
        chat_id:              Ahmed's Telegram chat ID.
        deepseek_api_key:     DeepSeek API key.

    Returns:
        Dict with brief fields (always returns something — never raises).
    """
    today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    weekday   = datetime.now(timezone.utc).strftime("%A")
    pool_str  = ", ".join(pool[:10]) + ("..." if len(pool) > 10 else "")

    prompt = f"""You are Axiom — the Nexus trading system's risk intelligence layer.
Today is {weekday}, {today}. VIX is {vix:.1f} (regime: {regime_classification}).

Generate the pre-market risk brief for the Nexus trading system.
Be concise. Focus on RISK only — what could hurt us today.

Reply in this EXACT format (no other text):
TOP_RISK_1: [biggest risk factor today, ≤80 chars]
TOP_RISK_2: [second risk factor, ≤80 chars]
TOP_RISK_3: [third risk factor, ≤80 chars]
SECTORS_WATCH: [sectors with elevated event risk today, ≤80 chars]
GUIDANCE: [2 sentences max — what to watch for in today's session]"""

    brief = {
        "date":                today,
        "vix":                 vix,
        "classification":      regime_classification,
        "strategy_bias":       strategy_bias,
        "top_risk_1":          "N/A",
        "top_risk_2":          "N/A",
        "top_risk_3":          "N/A",
        "sectors_watch":       "N/A",
        "guidance":            "Standard operating day. Monitor positions.",
        "pool_count":          len(pool),
        "generated_at":        datetime.now(timezone.utc).isoformat(),
    }

    # Attempt DeepSeek generation
    try:
        resp = requests.post(
            DEEPSEEK_API_URL,
            headers={
                "Authorization": f"Bearer {deepseek_api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       "deepseek-chat",
                "messages":    [{"role": "user", "content": prompt}],
                "max_tokens":  300,
                "temperature": 0.3,
            },
            timeout=20,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()

        def extract(key: str) -> str:
            for line in text.split("\n"):
                if line.startswith(f"{key}:"):
                    return line.split(":", 1)[1].strip()
            return "N/A"

        brief.update({
            "top_risk_1":    extract("TOP_RISK_1"),
            "top_risk_2":    extract("TOP_RISK_2"),
            "top_risk_3":    extract("TOP_RISK_3"),
            "sectors_watch": extract("SECTORS_WATCH"),
            "guidance":      extract("GUIDANCE"),
        })

    except Exception as e:
        logger.warning("DeepSeek brief generation failed: %s — sending factual brief", e)

    # Send to Telegram
    emoji = {
        "NORMAL":      "🟢",
        "LOW_VOL":     "🟡",
        "ELEVATED":    "🟡",
        "STRESS":      "🟠",
        "HIGH_STRESS": "🔴",
        "CRISIS":      "🛑",
    }.get(regime_classification, "⚪")

    message = (
        f"🔷 <b>AXIOM PRE-MARKET BRIEF — {today}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} Regime: <b>{regime_classification}</b> | VIX: {vix:.1f}\n"
        f"Strategy: {strategy_bias}\n\n"
        f"⚠️ <b>Top Risks:</b>\n"
        f"1. {brief['top_risk_1']}\n"
        f"2. {brief['top_risk_2']}\n"
        f"3. {brief['top_risk_3']}\n\n"
        f"👁 Watch: {brief['sectors_watch']}\n\n"
        f"📋 {brief['guidance']}\n\n"
        f"Pool opens at 9:15 AM. {brief['pool_count']} candidates in scope."
    )

    send_message(bot_token, chat_id, message)
    logger.info("Pre-market brief sent for %s (VIX=%.1f, regime=%s)", today, vix, regime_classification)

    return brief
