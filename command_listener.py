"""
agent-hub の inbox を listen し、slash command を直接処理する。

agent-hub-sdk の AgentHub.connect() + hub.inbox() を使用。
@scheduler が /add /list を直接処理するのと同じパターン。
LLM (Gemini) を経由せず、CommandListener が即座に応答する。

対応 slash command:
  /generate-code  → 6 桁 OTP を生成し、送信者に返信

このリスナーは gateway 起動時に 1 つだけ起動され、
セッション (VoiceSession) とは独立して動作する。
"""
import asyncio
import logging

from agent_hub_sdk import AgentHub

from auth import OTPStore

logger = logging.getLogger(__name__)

DISPLAY_NAME = "voice-gateway — Gemini Live voice interface (slash: /generate-code)"

# v2.0 以降: slash command は / prefix 必須
CMD_GENERATE_CODE = "/generate-code"


class CommandListener:
    """
    voice-gateway の agent-hub handle (@voice 等) の inbox を SDK で listen し、
    slash command を直接処理するサービス。

    /generate-code を受信すると:
      1. OTPStore で 6 桁コードを生成 (TTL 5 分)
      2. 送信者に send_message でコードを返信
      3. メッセージを ack (mark_as_read)

    LLM を経由しないため、遅延なく即座に応答できる。
    """

    def __init__(
        self,
        hub_url: str,
        hub_user: str,
        hub_pat: str,
        hub_tenant: str | None,
        otp_store: OTPStore,
    ) -> None:
        self._hub_url = hub_url
        self._hub_user = hub_user
        self._hub_pat = hub_pat
        self._hub_tenant = hub_tenant
        self.otp_store = otp_store

    async def run(self) -> None:
        """接続・登録後、inbox listen ループを開始する（リトライ付き）。"""
        backoff = 5
        while True:
            try:
                async with AgentHub.connect(
                    user=self._hub_user,
                    url=self._hub_url,
                    pat=self._hub_pat,
                    tenant=self._hub_tenant,
                    display_name=DISPLAY_NAME,
                ) as hub:
                    logger.info(
                        "CommandListener: connected as @%s, listening for slash commands",
                        self._hub_user,
                    )
                    backoff = 5  # 正常接続できたらリセット
                    async with hub.inbox() as messages:
                        async for msg in messages:
                            body = (msg.body or "").strip()
                            if body.lower() == CMD_GENERATE_CODE:
                                await self._handle_generate_code(hub, msg.sender, msg.id)
                            # 未知の slash command / bare text はそのまま ack して無視
                            # (bare text は Gemini セッション側で処理)
                            try:
                                await hub.ack(msg.id)
                            except Exception as ack_err:
                                logger.warning("ack failed for %s: %s", msg.id, ack_err)
            except Exception as e:
                logger.error(
                    "CommandListener error: %s — retry in %ds", e, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_generate_code(self, hub, sender: str, msg_id: str) -> None:
        """
        /generate-code 処理: OTP 生成 → 送信者に返信。

        @scheduler の slash command 処理と同じパターン:
        LLM を経由せず CommandListener が直接 send を呼ぶ。
        """
        code, ttl = self.otp_store.generate()
        ttl_min = ttl // 60
        reply = (
            f"🔑 **{code}** ({ttl_min}分有効)\n\n"
            f"スマホブラウザでこのコードを入力してセッションを開始してください。"
        )
        try:
            await hub.send(sender, reply)
            # セキュリティ: コードの先頭 2 桁のみログに残す
            logger.info(
                "/generate-code: sent to %s (code=%s****)", sender, code[:2]
            )
        except Exception as e:
            logger.error(
                "/generate-code: failed to reply to %s: %s", sender, e
            )
