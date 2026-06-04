"""
agent-hub MCP HTTP クライアント。
initialize / tools/call / register を担当する。
"""
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class AgentHubMCPClient:
    """
    agent-hub MCP server への HTTP クライアント (httpx ベース)。
    MCP session を保持し、ツール呼び出しを行う。
    """

    def __init__(
        self,
        url: str,
        user: str,
        tenant: str | None = None,
        auth_token: str | None = None,
    ) -> None:
        self.base_url = url          # http://agent-hub:3000/mcp
        self.user = user
        self.tenant = tenant
        self.auth_token = auth_token  # GitHub PAT or None (trust mode)
        self.session_id: str | None = None
        self._id_counter = 0

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.auth_token:
            h["Authorization"] = f"Bearer {self.auth_token}"
        else:
            h["X-User-Id"] = self.user
        if self.tenant:
            h["X-Tenant-Id"] = self.tenant
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    async def initialize(self) -> dict:
        """MCP セッションを確立する。session_id を取得してキャッシュ。"""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                self.base_url,
                headers=self._headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "clientInfo": {"name": "voice-gateway", "version": "0.1.0"},
                        "capabilities": {},
                    },
                },
            )
            r.raise_for_status()
            self.session_id = r.headers.get("mcp-session-id")
            logger.info("MCP initialized (session=%s, user=%s)", self.session_id, self.user)
            return r.json()

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        """
        MCP tools/call を実行し、result content[0].text をパースして返す。
        エラー時は RuntimeError を raise。
        """
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                self.base_url,
                headers=self._headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                },
            )
            r.raise_for_status()
            data = r.json()

        if "error" in data:
            raise RuntimeError(f"MCP tool error [{name}]: {data['error']}")

        content = data.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])
            except json.JSONDecodeError:
                return content[0]["text"]
        return data.get("result")

    async def register(self, display_name: str) -> None:
        """voice-gateway を agent-hub に登録する。"""
        try:
            await self.call_tool("register", {"display_name": display_name})
            logger.info("Registered as @%s (%s)", self.user, display_name)
        except Exception as e:
            logger.warning("register failed (continuing): %s", e)

    def sse_url(self) -> str:
        """SSE エンドポイント URL を返す。"""
        return self.base_url.replace("/mcp", "") + "/sse"
