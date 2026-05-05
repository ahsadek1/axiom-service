"""
telegram.py — OMNI Telegram Synthesis Card

Sends the full OMNI synthesis card to Ahmed after every evaluation.
Format designed for rapid at-a-glance decision making.
Always sends something — never silently drops.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("omni.telegram")


def send_synthesis_card(
    bot_token:          str,
    chat_id:            str,
    concordance:        dict,
    verdict:            "SynthesisVerdict",  # type: ignore
    brain_results:      dict[str, dict],
    position_size:      float,
    execution_ok:       Optional[bool],
    psychology_overlay: Optional[dict] = None,
) -> bool:
    """
    Send the full synthesis card to Ahmed via Telegram.

    Args:
        bot_token:      Telegram bot token.
        chat_id:        Ahmed's Telegram chat ID.
        concordance:    Concordance payload from buffer.
        verdict:        SynthesisVerdict from synthesis engine.
        brain_results:  Raw brain results dict.
        position_size:  Final dollar position size.
        execution_ok:   True/False/None (None = not dispatched).

    Returns:
        True if sent successfully.
    """
    ticker    = concordance.get("ticker", "?")
    direction = concordance.get("direction", "?").upper()
    system    = concordance.get("system", "?").upper()
    pathway   = concordance.get("pathway", "?")
    window_id = concordance.get("window_id", "?")
    w_score   = concordance.get("weighted_score", 0)

    # Verdict emoji
    verdict_emoji = {
        "STRONG_GO":   "🟢🟢",
        "GO":          "🟢",
        "CONDITIONAL": "🟡",
        "NO_GO":       "🔴",
    }.get(verdict.verdict, "⚪")

    # System label
    system_label = "ALPHA (Options)" if system == "ALPHA" else "PRIME (Swing)"

    # Brain vote lines
    brain_lines = []
    for brain_name in ("claude", "o3mini", "gemini", "deepseek"):
        result = brain_results.get(brain_name, {})
        if result.get("error"):
            line = f"  ⚠️ {brain_name.upper():<9}: ERROR — {result['error'][:40]}"
        else:
            vote       = result.get("vote", "?")
            confidence = result.get("confidence", "?")
            concern    = result.get("concern_1") or "—"
            vote_emoji = "✅" if vote == "GO" else ("⛔" if vote == "NO_GO" else "🟡")
            line = (
                f"  {vote_emoji} {brain_name.upper():<9}: "
                f"{vote} ({confidence}) — {concern[:50]}"
            )
        brain_lines.append(line)

    # Echo chamber warning
    echo_line = ""
    if verdict.echo_chamber_flagged:
        echo_line = "\n⚠️ <b>ECHO CHAMBER DETECTED</b> — agent reasoning overlap\n"

    # Axiom block warning
    axiom_line = ""
    if verdict.axiom_blocked:
        stops_str = "; ".join(verdict.axiom_hard_stops[:2])
        axiom_line = f"\n🛑 <b>AXIOM BLOCKED</b>: {stops_str}\n"
    elif verdict.axiom_hard_stops:
        axiom_line = f"\n⚠️ Axiom flags: {'; '.join(verdict.axiom_hard_stops[:2])}\n"

    # Execution status
    if execution_ok is True:
        exec_line = f"✅ Routed to {system_label} execution"
    elif execution_ok is False:
        exec_line = f"⚠️ Execution routing FAILED — manual action required"
    else:
        exec_line = "⏸ Not dispatched (CONDITIONAL/NO_GO)"

    brain_block = "\n".join(brain_lines)

    # Psychology overlay line (optional)
    psych_line = ""
    if psychology_overlay and isinstance(psychology_overlay, dict):
        adj = psychology_overlay.get("total_adjustment", 0)
        summary = psychology_overlay.get("psychology_summary", "")
        adj_str = f"+{adj}" if adj > 0 else str(adj)
        if summary or adj != 0:
            psych_line = f"\n🧠 Psychology: {adj_str} | {summary[:80]}\n"

    message = (
        f"{verdict_emoji} <b>OMNI SYNTHESIS — {ticker} {direction}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"System:  {system_label}\n"
        f"Pathway: {pathway} | Agent score: {w_score:.1f}\n"
        f"Window:  {window_id}\n"
        f"\n"
        f"🗳 <b>VERDICT: {verdict.verdict}</b> "
        f"({verdict.votes_go}/4 brains GO)\n"
        f"\n"
        f"<b>Brain Votes:</b>\n"
        f"{brain_block}"
        f"{echo_line}"
        f"{axiom_line}"
        f"\n"
        f"💰 Position: ${position_size:,.0f} "
        f"(×{verdict.sizing_mult:.2f} sizing)\n"
        f"{psych_line}"
        f"{exec_line}"
    )

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        logger.warning("Telegram synthesis card failed: HTTP %d", resp.status_code)
        return False
    except Exception as e:
        logger.error("Telegram synthesis card exception: %s", e)
        return False


def send_conditional_alert(
    bot_token:    str,
    chat_id:      str,
    ticker:       str,
    direction:    str,
    system:       str,
    votes_go:     int,
    pathway:      str,
    window_id:    str,
    brain_summary: dict[str, str],
    expires_min:  int = 30,
) -> bool:
    """
    Send a CONDITIONAL alert requiring Ahmed's manual decision.

    Args:
        bot_token:     Telegram bot token.
        chat_id:       Ahmed's Telegram chat ID.
        ticker:        Stock ticker.
        direction:     bullish or bearish.
        system:        'alpha' or 'prime'.
        votes_go:      Number of GO votes (2 for CONDITIONAL).
        pathway:       Concordance pathway.
        window_id:     15-min window ID.
        brain_summary: Dict of brain_name → vote.
        expires_min:   Minutes until this opportunity expires.

    Returns:
        True if alert sent successfully.
    """
    summary_lines = "\n".join(
        f"  {name.upper()}: {vote}" for name, vote in brain_summary.items()
    )
    message = (
        f"🟡 <b>CONDITIONAL — Ahmed Decision Required</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Ticker:  {ticker} {direction.upper()}\n"
        f"System:  {system.upper()} | Pathway: {pathway}\n"
        f"Window:  {window_id}\n"
        f"\n"
        f"🗳 {votes_go}/4 brains voted GO\n"
        f"\n"
        f"<b>Brain votes:</b>\n"
        f"{summary_lines}\n"
        f"\n"
        f"⏰ Expires in {expires_min} minutes — no action = skip."
    )
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        logger.error("Conditional alert failed: %s", e)
        return False


def send_axiom_block_alert(
    bot_token:  str,
    chat_id:    str,
    ticker:     str,
    hard_stops: list,
    regime:     str,
) -> bool:
    """
    Notify Ahmed when Axiom's hard-stop gate blocks a GO/STRONG_GO verdict.

    Cipher Finding 6 fix: silent Axiom blocks left Ahmed unable to distinguish
    "no signals today" from "CRISIS regime blocked 3 signals today."

    Args:
        bot_token:  Telegram bot token.
        chat_id:    Ahmed's chat ID.
        ticker:     Ticker that was blocked.
        hard_stops: List of hard stop reasons from Axiom.
        regime:     Current regime classification.
    """
    stops_text = "\n".join(f"  ⛔ {s}" for s in hard_stops) if hard_stops else "  ⛔ AXIOM_UNREACHABLE"
    message = (
        f"🛑 <b>AXIOM HARD STOP — Trade Blocked</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Ticker:  {ticker}\n"
        f"Regime:  {regime}\n"
        f"\n"
        f"<b>Hard stops:</b>\n"
        f"{stops_text}\n"
        f"\n"
        f"OMNI verdict was GO/STRONG_GO — Axiom gate prevented execution.\n"
        f"No position was opened."
    )
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
        return resp.status_code == 200
    except Exception as e:
        logger.error("Axiom block alert failed: %s", e)
        return False
