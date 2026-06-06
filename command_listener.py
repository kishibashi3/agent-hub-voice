"""
agent-hub の inbox メッセージをハンドリングする。

CommandListener は hub 接続を持たない。
main.py が単一の AgentHub セッションを管理し、
inbox から受信したメッセージを CommandListener.handle() 経由で渡す。

対応 slash command:
  /generate-code  → 6 桁 OTP を生成し、送信者に返信
  その他の / コマンド → エラーガイダンスを返す

このリスナーは VoiceSession がアクティブでない場合のみ呼ばれる。
VoiceSession がアクティブな場合は VoiceSession._on_pikon() が処理する。

bare text (slash prefix なし) は VoiceSession がアクティブな場合に Gemini が処理する。
VoiceSession が非アクティブな場合は ack のみ・返信なし (意図しない spam を防ぐ)。
"""
import logging

from agent_hub_sdk import HubSession, IncomingMessage

from auth import OTPStore

logger = logging.getLogger(__name__)

DISPLAY_NAME = "voice-gateway — Gemini Live voice interface (slash: /generate-code)"

# v2.0 以降: slash command は / prefix 必須
CMD_GENERATE_CODE = "/generate-code"

UNKNOWN_CMD_REPLY = (
    "コマンドが認識できません。\n"
    "使用可能なコマンド: /generate-code"
)


class CommandListener:
    """
    voice-gateway の agent-hub inbox メッセージをハンドリングするサービス。

    hub 接続は持たない。main.py が単一の AgentHub セッションを管理し、
    inbox メッセージを handle() 経由でここに渡す設計。

    /generate-code を受信すると:
      1. OTPStore で 6 桁コードを生成 (TTL 5 分)
      2. 送信者に send_message でコードを返信
      3. メッセージを ack (mark_as_read)

    未知のスラッシュコマンドを受信すると:
      1. UNKNOWN_CMD_REPLY を返信 (利用可能なコマンドをガイド)
      2. メッセージを ack

    bare text (スラッシュなし) はエラーレスポンスを返さず ack のみ。
    VoiceSession がアクティブな場合、bare text は VoiceSession._on_pikon() → Gemini が処理する。

    LLM を経由しないため、遅延なく即座に応答できる。
    """

    def __init__(self, otp_store: OTPStore) -> None:
        self.otp_store = otp_store

    async def handle(self, hub: HubSession, msg: IncomingMessage) -> None:
        """
        inbox から受け取った 1 件のメッセージを処理する。

        VoiceSession がアクティブでない場合のみ呼ばれる。
        VoiceSession がアクティブな場合は VoiceSession._on_pikon() が処理する。
        """
        body = (msg.body or "").strip()  # ログ・判定に使用。返信には含めない (injection 防止)
        if body.lower() == CMD_GENERATE_CODE:
            await self._handle_generate_code(hub, msg.sender, msg.id)
        elif body.startswith("/"):
            # 未知のスラッシュコマンド: ガイダンスを返す
            await self._handle_unknown(hub, msg.sender, msg.id, body)
        # bare text (slash なし): ack のみ。VoiceSession 非アクティブ時は無視。
        # (VoiceSession アクティブ時は _on_pikon() 経由で Gemini が処理するため、
        #  CommandListener.handle() は呼ばれない)
        try:
            await hub.ack(msg.id)
        except Exception as ack_err:
            logger.warning("ack failed for %s: %s", msg.id, ack_err)

    async def _handle_unknown(
        self, hub: HubSession, sender: str, msg_id: str, body: str
    ) -> None:
        """
        未知のスラッシュコマンドへのエラーレスポンス。

        ユーザーが認識できないコマンドを送った場合に利用可能なコマンドを案内する。
        bare text は対象外 (呼び元でフィルタ済み)。
        """
        try:
            await hub.send(sender, UNKNOWN_CMD_REPLY, caused_by=msg_id)
            # body はログのみに使用。返信文には含めない。
            logger.info("unknown command from %s: %r — replied with guidance", sender, body)
        except Exception as e:
            logger.error("unknown command: failed to reply to %s: %s", sender, e)

    async def _handle_generate_code(
        self, hub: HubSession, sender: str, msg_id: str
    ) -> None:
        """
        /generate-code 処理: OTP 生成 → 送信者に返信。

        LLM を経由せず CommandListener が直接 send を呼ぶ。
        """
        code, ttl = self.otp_store.generate()
        ttl_min = ttl // 60
        reply = (
            f"🔑 **{code}** ({ttl_min}分有効)\n\n"
            f"スマホブラウザでこのコードを入力してセッションを開始してください。"
        )
        try:
            await hub.send(sender, reply, caused_by=msg_id)
            # セキュリティ: コードの先頭 2 桁のみログに残す
            logger.info(
                "/generate-code: sent to %s (code=%s****)", sender, code[:2]
            )
        except Exception as e:
            logger.error(
                "/generate-code: failed to reply to %s: %s", sender, e
            )
