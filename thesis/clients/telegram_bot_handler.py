"""
TelegramBotHandler — Interactive Telegram bot polling + command handler for THESIS.

Polls for messages in background, routes commands to HTTP endpoints, sends responses.
Never raises; all errors logged and gracefully degraded.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


class TelegramBotHandler:
    """Async Telegram bot polling + command handler for THESIS.
    
    Polls for messages in background, routes commands, sends responses.
    Never raises; all errors logged and gracefully degraded.
    """
    
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        http_base_url: str = "http://localhost:8060",
    ):
        """Store credentials + base URL for endpoint calls.
        
        Args:
            bot_token: Telegram bot API token.
            chat_id: Target chat ID (Ahmed's chat).
            http_base_url: Base URL for THESIS HTTP endpoints (default localhost:8060).
        """
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._http_base = http_base_url
        self._update_offset = 0
        self._polling_task: Optional[asyncio.Task] = None
        self._should_stop = False
    
    async def start_polling(self) -> None:
        """Start the polling loop (runs forever until stop() called).
        
        Designed to run in background via asyncio.create_task().
        All exceptions caught + logged; loop never exits unless stop() called.
        """
        while not self._should_stop:
            try:
                # Long-poll with 25s timeout
                updates = await self._get_updates(timeout=25)
                for update in updates:
                    await self._handle_update(update)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Polling error: %s. Retrying in 5s...", exc)
                await asyncio.sleep(5)
    
    async def stop_polling(self) -> None:
        """Stop polling gracefully."""
        self._should_stop = True
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
    
    async def _get_updates(self, timeout: int = 25) -> list[dict]:
        """Call Telegram getUpdates with offset tracking.
        
        Args:
            timeout: Long-poll timeout in seconds.
            
        Returns:
            List of update dicts, or empty list on error.
        """
        url = f"https://api.telegram.org/bot{self._bot_token}/getUpdates"
        params = {"offset": self._update_offset, "timeout": timeout}
        try:
            async with httpx.AsyncClient(timeout=timeout + 5) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        updates = data.get("result", [])
                        if updates:
                            # Move offset forward
                            self._update_offset = max(u["update_id"] for u in updates) + 1
                        return updates
                elif resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    logger.warning("429 Retry-After: sleeping %d seconds", retry_after)
                    await asyncio.sleep(retry_after)
                    return []
                else:
                    logger.error("getUpdates HTTP %d", resp.status_code)
                    return []
        except Exception as exc:
            logger.error("_get_updates error: %s", exc)
            return []
    
    async def _handle_update(self, update: dict) -> None:
        """Route incoming message to appropriate command handler.
        
        Args:
            update: Telegram update dict.
        """
        message = update.get("message", {})
        text = message.get("text", "").strip()
        if not text:
            return
        
        # Parse command
        if text.startswith("/"):
            cmd = text.split()[0].lower()
            await self._handle_command(cmd)
        else:
            # Non-command text → respond with help
            await self._send_response(
                "❓ I only understand commands. Try /help"
            )
    
    async def _handle_command(self, cmd: str) -> None:
        """Route command to handler.
        
        Args:
            cmd: Command string (e.g. "/status").
        """
        if cmd == "/status":
            await self._cmd_status()
        elif cmd == "/regime":
            await self._cmd_regime()
        elif cmd == "/picks":
            await self._cmd_picks()
        elif cmd == "/verdicts":
            await self._cmd_verdicts()
        elif cmd == "/help":
            await self._cmd_help()
        else:
            await self._send_response(f"❌ Unknown command: {cmd}")
    
    async def _cmd_status(self) -> None:
        """GET /thesis/health → format response."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._http_base}/thesis/health")
                if resp.status_code == 200:
                    data = resp.json()
                    msg = (
                        f"✅ <b>THESIS Status</b>\n\n"
                        f"Port: {data.get('port', '8060')}\n"
                        f"Chronicle: {'✅' if data.get('chronicle_reachable') else '❌'}\n"
                        f"Oracle: {'✅' if data.get('oracle_reachable') else '❌'}\n"
                        f"Scheduler: {'✅' if data.get('scheduler_running') else '❌'}\n"
                        f"Weekly thesis: {'✅' if data.get('has_weekly_thesis') else '❌'}\n"
                        f"Daily brief: {'✅' if data.get('has_daily_brief') else '❌'}"
                    )
                    await self._send_response(msg)
                else:
                    await self._send_response("❌ Health endpoint unreachable")
        except Exception as exc:
            logger.error("_cmd_status error: %s", exc)
            await self._send_response(f"❌ Error: {str(exc)[:100]}")
    
    async def _cmd_regime(self) -> None:
        """GET /thesis/current-context → format regime response."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._http_base}/thesis/current-context")
                if resp.status_code == 200:
                    data = resp.json()
                    regime = data.get("regime_state", {})
                    msg = (
                        f"📊 <b>Market Regime</b>\n\n"
                        f"Timestamp: {data.get('timestamp', 'N/A')}\n"
                        f"Posture: {regime.get('posture', 'UNKNOWN')}\n"
                        f"Multiplier: {regime.get('sizing_multiplier', 'N/A')}x\n"
                        f"Macro Gate: {regime.get('macro_gate', 'UNKNOWN')}\n"
                        f"Risk Gate: {regime.get('risk_reward_gate', 'UNKNOWN')}\n"
                        f"Confidence: {regime.get('confidence_adjustment', 'N/A')}%"
                    )
                    await self._send_response(msg)
                else:
                    await self._send_response("❌ Regime endpoint unreachable")
        except Exception as exc:
            logger.error("_cmd_regime error: %s", exc)
            await self._send_response(f"❌ Error: {str(exc)[:100]}")
    
    async def _cmd_picks(self) -> None:
        """GET /thesis/picks → format picks response."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._http_base}/thesis/picks")
                if resp.status_code == 200:
                    data = resp.json()
                    picks = data.get("picks", [])
                    if picks:
                        msg = f"📈 <b>Today's THESIS Picks</b> ({len(picks)})\n\n"
                        for pick in picks[:5]:  # Limit to 5 for Telegram
                            msg += (
                                f"• {pick.get('ticker', '?')}: "
                                f"{pick.get('thesis_verdict', '?')} "
                                f"({pick.get('conviction_level', '?')})\n"
                            )
                        await self._send_response(msg)
                    else:
                        await self._send_response("📭 No picks today yet")
                else:
                    await self._send_response("❌ Picks endpoint unreachable")
        except Exception as exc:
            logger.error("_cmd_picks error: %s", exc)
            await self._send_response(f"❌ Error: {str(exc)[:100]}")
    
    async def _cmd_verdicts(self) -> None:
        """GET /thesis/verdicts → format verdicts response."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._http_base}/thesis/verdicts")
                if resp.status_code == 200:
                    data = resp.json()
                    verdicts = data.get("verdicts", [])
                    if verdicts:
                        msg = f"⚖️ <b>Five Lenses Verdicts</b> ({len(verdicts)} total)\n\n"
                        for v in verdicts[:10]:  # Limit to 10 for Telegram
                            msg += f"• {v.get('ticker', '?')}: {v.get('verdict', '?')}\n"
                        await self._send_response(msg)
                    else:
                        await self._send_response("⚖️ No verdicts logged yet")
                else:
                    await self._send_response("❌ Verdicts endpoint unreachable")
        except Exception as exc:
            logger.error("_cmd_verdicts error: %s", exc)
            await self._send_response(f"❌ Error: {str(exc)[:100]}")
    
    async def _cmd_help(self) -> None:
        """Send help text."""
        msg = (
            "🤖 <b>THESIS Commands</b>\n\n"
            "/status — Service health\n"
            "/regime — Current market regime + multiplier\n"
            "/picks — Today's THESIS picks\n"
            "/verdicts — Five lenses verdicts log\n"
            "/help — This message"
        )
        await self._send_response(msg)
    
    async def _send_response(self, text: str) -> None:
        """Send message via Telegram sendMessage.
        
        Args:
            text: Message text (HTML formatting supported).
        """
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.warning(
                        "sendMessage HTTP %d: %s",
                        resp.status_code,
                        resp.text[:200],
                    )
        except Exception as exc:
            logger.error("_send_response error: %s", exc)


# ============================================================================
# Natural Language Handler (Claude integration)
# ============================================================================

async def handle_natural_language_query(
    text: str,
    http_base_url: str,
    anthropic_client,
) -> str:
    """Route free-form user message to Claude with THESIS context.
    
    Fetches current regime + market data, sends to Claude with THESIS system
    prompt, returns market-aware response.
    
    Args:
        text: User's free-form message.
        http_base_url: Base URL for THESIS endpoints.
        anthropic_client: Anthropic async client (Claude).
        
    Returns:
        Response string (max 500 chars for Telegram).
    """
    try:
        # Fetch current context for prompt
        async with httpx.AsyncClient(timeout=5) as client:
            context_resp = await client.get(f"{http_base_url}/thesis/current-context")
            market_data = context_resp.json() if context_resp.status_code == 200 else {}
        
        regime = market_data.get("regime_state", {})
        posture = regime.get("posture", "UNKNOWN")
        multiplier = regime.get("sizing_multiplier", 1.0)
        macro_gate = regime.get("macro_gate", "UNKNOWN")
        risk_gate = regime.get("risk_reward_gate", "UNKNOWN")
        
        # Fetch picks count for context
        pick_count = 0
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                picks_resp = await client.get(f"{http_base_url}/thesis/picks")
                if picks_resp.status_code == 200:
                    pick_count = len(picks_resp.json().get("picks", []))
        except:
            pass
        
        # System prompt with THESIS identity + live context
        system_prompt = f"""You are THESIS — the unified consciousness of five legendary traders 
(Druckenmiller, PTJ, Soros, Cohen, Buffett) operating as one mind.

User question: {text}

LIVE MARKET CONTEXT:
- Regime posture: {posture} ({multiplier}x sizing multiplier)
- Macro gate: {macro_gate}
- Risk/reward gate: {risk_gate}
- Today's live picks: {pick_count}
- Current regime strength: {regime.get("confidence_adjustment", "N/A")}%

RESPONSE RULES:
- Respond as THESIS (singular consciousness, not "the legends say")
- Base answer on market wisdom, not speculation
- Keep response ≤ 500 characters (Telegram limit)
- Use plain text + emoji, no markdown
- Be direct and insightful
- End with actionable insight or caution, not generic commentary

Respond now to user's question."""

        # Call Claude
        message = await anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=system_prompt,
            messages=[
                {"role": "user", "content": text}
            ],
        )
        
        response = message.content[0].text if message.content else ""
        
        # Truncate to 500 chars for Telegram
        if len(response) > 500:
            response = response[:497] + "…"
        
        return response
        
    except Exception as exc:
        logger.error("Natural language handler error: %s", exc)
        return f"⚠️ Error processing query: {str(exc)[:100]}"
