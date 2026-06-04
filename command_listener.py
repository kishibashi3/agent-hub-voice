"""
agent-hub の inbox を listen し、slash command を直接処理する。

@scheduler が /add /list を直接処理するのと同じパターン。
LLM (Gemini) を経由せず、CommandListener が即座に応答する。

対応 slash command:
  /generate-code  → 6 桁 OTP を生成し、送信者に返信

このリスナーは gateway 起動時に 1 つだけ起動され、
セッション (VoiceSession) とは独立して動作する。
"""
import asyncio
import json
import logging

import httpx

from auth import OTPStore
from mcp_client import AgentHubMCPClient

logger = logging.getLogger(__name__)

DISPLAY_NAME = "voice-gateway — Gemini Live voice interface (slash: /generate-code)"

# v2.0 以降: slash command は / prefix 必須
CMD_GENERATE_CODE = "/generate-code"


class CommandListener:
    """
    voice-gateway の agent-hub handle (@voice 等) の inbox を SSE で listen し、
    slash command を直接処理するサービス。

    /generate-code を受信すると:
      1. OTPStore で 6 桁コードを生成 (TTL 5 分)
      2. 送信者に send_message でコードを返信
      3. メッセージを mark_as_read

    LLM を経由しないため、遅延なく即座に応答できる。
    """

    def __init__(self, mcp_client: AgentHubMCPClient, otp_store: OTPStore) -> None:
        self.mcp = mcp_client
        self.otp_store = otp_store

    async def run(self) -> None:
        """初期化後、SSE listen ループを開始する（リトライ付き）。"""
        backoff = 5
        while True:
            try:
                await self.mcp.initialize()
                await self.mcp.register(DISPLAY_NAME)
                logger.info(
                    "CommandListener: registered as @%s, listening for slash commands",
                    self.mcp.user,
                )
                await self._listen()
            except Exception as e:
                logger.error(
                    "CommandListener error: %s — retry in %ds", e, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _listen(self) -> None:
        url = self.mcp.sse_url()
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET", url, headers=self.mcp._headers()
            ) as response:
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    await self._handle_event(data)

    async def _handle_event(self, data: dict) -> None:
        method = data.get("method", "")
        if method != "notifications/resources/updated":
            return
        uri = data.get("params", {}).get("uri", "")
        if "inbox://" not in uri:
            return
        await self._process_inbox()

    async def _process_inbox(self) -> None:
        try:
            messages = await self.mcp.call_tool("get_messages", {"limit": 20})
        except Exception as e:
            logger.error("CommandListener get_messages failed: %s", e)
            return

        if not isinstance(messages, list):
            return

        for msg in messages:
            # body をそのまま（strip のみ）で比較。大文字小文字は区別しない。
            body = (msg.get("body") or "").strip()
            sender = msg.get("from", "")
            msg_id = msg.get("id", "")

            if body.lower() == CMD_GENERATE_CODE:
                await self._handle_generate_code(sender, msg_id)
            # 未知の slash command は無視 (bare text は Gemini セッション側で処理)

    async def _handle_generate_code(self, sender: str, msg_id: str) -> None:
        """
        /generate-code 処理: OTP 生成 → 送信者に返信。

        @scheduler の slash command 処理と同じパターン:
        LLM を経由せず CommandListener が直接 send_message を呼ぶ。
        """
        code, ttl = self.otp_store.generate()
        ttl_min = ttl // 60
        reply = (
            f"🔑 **{code}** ({ttl_min}分有効)\n\n"
            f"スマホブラウザでこのコードを入力してセッションを開始してください。"
        )
        try:
            await self.mcp.call_tool("send_message", {"to": sender, "message": reply})
            await self.mcp.call_tool("mark_as_read", {"message_id": msg_id})
            # セキュリティ: コードの先頭 2 桁のみログに残す
            logger.info(
                "/generate-code: sent to %s (code=%s****)", sender, code[:2]
            )
        except Exception as e:
            logger.error(
                "/generate-code: failed to reply to %s: %s", sender, e
            )
