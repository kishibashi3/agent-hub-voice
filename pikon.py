"""
agent-hub inbox を SDK の hub.inbox() で listen し、
新メッセージ到着時に callback を呼ぶ。
セッション単位でインスタンス化 (browser WS 接続 1 本に 1 インスタンス)。

hub.inbox() は push (SSE) + safety-net poll + heartbeat を内包しているため、
手動 SSE 実装は不要。
"""
import logging
from typing import Awaitable, Callable

from agent_hub_sdk import HubSession, IncomingMessage

logger = logging.getLogger(__name__)

OnMessageCallback = Callable[[list[IncomingMessage]], Awaitable[None]]


class PikonListener:
    """
    SDK hub.inbox() を常時 listen し、メッセージ到着時に callback を呼ぶ。

    VoiceSession が hub を所有し、PikonListener は参照のみ保持する。
    hub.inbox() のシングルコンシューマー制約により、1 セッションで
    PikonListener は 1 インスタンスのみ生成すること。

    NOTE: ack は行わない。Gemini セッション (VoiceSession) が
    mark_as_read ツール呼び出しで明示的に ack する設計。
    hub.inbox() の in_flight_ids dedup により、同一セッション内では
    同一メッセージが重複 yield されない。
    """

    def __init__(
        self,
        hub: HubSession,
        on_message: OnMessageCallback,
    ) -> None:
        self.hub = hub
        self.on_message = on_message

    def stop(self) -> None:
        """停止要求。asyncio タスクキャンセルで実際に停止するため no-op。"""
        pass

    async def listen(self) -> None:
        """hub.inbox() を開き、メッセージ到着のたびに callback を呼ぶ。"""
        async with self.hub.inbox() as messages:
            logger.info("PikonListener: inbox listening")
            async for msg in messages:
                try:
                    await self.on_message([msg])
                except Exception as e:
                    logger.error("PikonListener on_message error: %s", e)
