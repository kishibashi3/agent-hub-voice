"""
agent-hub SSE push を listen し、新メッセージ到着時に callback を呼ぶ。
セッション単位でインスタンス化 (browser WS 接続 1 本に 1 インスタンス)。
"""
import asyncio
import json
import logging
from typing import Awaitable, Callable

import httpx

from mcp_client import AgentHubMCPClient

logger = logging.getLogger(__name__)

OnMessageCallback = Callable[[list[dict]], Awaitable[None]]


class PikonListener:
    """
    SSE push を常時 listen し、inbox 更新時に新メッセージを取得して callback を呼ぶ。
    """

    def __init__(
        self,
        mcp_client: AgentHubMCPClient,
        on_message: OnMessageCallback,
    ) -> None:
        self.mcp = mcp_client
        self.on_message = on_message
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def listen(self) -> None:
        """SSE 接続を確立してイベントを受信し続ける。切断時は自動再接続。"""
        url = self.mcp.sse_url()
        backoff = 2

        while not self._stopped:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "GET", url, headers=self.mcp._headers()
                    ) as response:
                        logger.info("PikonListener: SSE connected")
                        backoff = 2
                        async for line in response.aiter_lines():
                            if self._stopped:
                                return
                            if not line.startswith("data:"):
                                continue
                            try:
                                data = json.loads(line[5:].strip())
                            except json.JSONDecodeError:
                                continue
                            await self._handle_event(data)
            except Exception as e:
                if self._stopped:
                    return
                logger.warning(
                    "PikonListener SSE error: %s — retry in %ds", e, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_event(self, data: dict) -> None:
        method = data.get("method", "")
        if method != "notifications/resources/updated":
            return
        uri = data.get("params", {}).get("uri", "")
        if "inbox://" not in uri:
            return

        try:
            messages = await self.mcp.call_tool("get_messages", {"limit": 10})
            if messages and isinstance(messages, list):
                await self.on_message(messages)
        except Exception as e:
            logger.error("PikonListener get_messages error: %s", e)
