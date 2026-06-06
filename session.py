"""
VoiceSession — browser WebSocket ↔ Gemini Live 1:1 セッション (ADK ベース)。

ライフサイクル:
  1. OTP 認証 (auth.py)
  2. ADK セッション作成 (InMemorySessionService)
  3. upstream / downstream / hub_manager を並行実行
     - upstream: browser PCM → LiveRequestQueue.send_realtime()
     - downstream: Runner イベント → browser WS (audio bytes / transcript JSON)
     - hub_manager: agent-hub SDK 接続 + PikonListener + 自動再接続ループ
  4. hub_manager が hub_tools にセッションを注入 (再接続ごとに更新)
  5. 切断時クリーンアップ (hub_tools.clear_hub_session / queue.close)

## hub 再接続設計 (issue: セッション切断問題)

agent-hub サーバーは MCP セッションを 30 秒ごとに ping し、応答がなければ
切断する (PING_MAX_RETRIES=2, PING_TIMEOUT_MS=5000)。ADK 音声 I/O と並行実行中、
asyncio イベントループの競合で pong が 5 秒以内に届かず、サーバーが MCP セッションを
切断するケースがある。

修正前: MCP セッション切断 → pikon_task 失敗 → _main_loop 終了 → WS 切断
修正後: MCP セッション切断 → hub_manager が自動再接続 → Gemini セッション継続

hub_manager_loop は upstream / downstream の wait 対象に含めない。
upstream (ブラウザ切断) か downstream (Gemini エラー) が終了したときのみ
セッション全体を停止する。
"""
import asyncio
import json
import logging
import uuid

from aiohttp import WSMsgType, web
from google.adk.agents import LiveRequestQueue
from google.adk.runners import RunConfig
from google.genai import types

from agent_hub_sdk import AgentHub, IncomingMessage

from auth import OTPStore
from command_listener import CMD_GENERATE_CODE
import hub_tools
from gemini_client import APP_NAME, create_voice_runner
from pikon import PikonListener

logger = logging.getLogger(__name__)

AUTH_TIMEOUT = 30  # 秒: OTP 入力待ちタイムアウト
VOICE_NAME = "Kore"

# hub 再接続バックオフ設定
_HUB_RECONNECT_BACKOFF_MIN_S = 1.0
_HUB_RECONNECT_BACKOFF_MAX_S = 30.0


class VoiceSession:
    """
    1 ブラウザ接続 = 1 VoiceSession。
    aiohttp.web.WebSocketResponse を browser_ws として受け取る。
    """

    def __init__(
        self,
        browser_ws: web.WebSocketResponse,
        hub_url: str,
        hub_user: str,
        hub_pat: str,
        hub_tenant: str | None,
        gemini_api_key: str,
        gemini_model: str,
        system_prompt: str,
        otp_store: OTPStore,
    ) -> None:
        self.ws = browser_ws
        self._hub_url = hub_url
        self._hub_user = hub_user
        self._hub_pat = hub_pat
        self._hub_tenant = hub_tenant
        self.otp_store = otp_store

        # ADK Runner (セッションごとに生成)
        self._runner, self._session_service = create_voice_runner(
            api_key=gemini_api_key,
            model=gemini_model,
            system_prompt=system_prompt,
        )
        self._adk_session_id: str | None = None

        self.is_gemini_speaking = False
        self._pending_messages: list[IncomingMessage] = []
        self._pikon_listener: PikonListener | None = None
        self._queue: LiveRequestQueue | None = None
        self._hub = None  # session 中の hub 参照 (_on_pikon での直接返答用)

        # ADK は partial (ストリーミング中チャンク) と non-partial (最終完全テキスト)
        # 両方の transcription event を yield する。non-partial は partial の集積と
        # 同じテキストを含むため、両方を送信すると browser で2重表示になる。
        # partial が送信済みなら non-partial をスキップするためのフラグ。
        # turn_complete / interrupted でリセットする。
        self._user_transcript_partial_sent = False
        self._model_transcript_partial_sent = False

    # =========================================================================
    # メインエントリ
    # =========================================================================

    async def run(self) -> None:
        # Step 1: OTP 認証
        if not await self._authenticate():
            return

        # Step 2: Gemini セッション起動 (hub は内部で再接続ループ管理)
        try:
            await self._run_session()
        except Exception as e:
            logger.exception("VoiceSession error: %s", e)
            await self._send_error("session_error", str(e))

    async def _run_session(self) -> None:
        """ADK セッションを作成し upstream / downstream / hub_manager を並行実行する。

        hub_manager は MCP セッション切断時に自動再接続するため、
        Gemini セッションは hub の状態に依存しない。
        upstream (ブラウザ切断) か downstream (Gemini エラー) が終了したときのみ
        セッション全体を停止する。
        """
        # ADK セッション作成
        self._adk_session_id = str(uuid.uuid4())
        await self._session_service.create_session(
            app_name=APP_NAME,
            user_id=self._hub_user,
            session_id=self._adk_session_id,
        )

        self._queue = LiveRequestQueue()
        run_config = RunConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=VOICE_NAME
                    )
                )
            ),
            # Gemini の server-side VAD がユーザー発話を検知したら
            # 即座にモデルを停止 (デフォルト動作だが明示的に設定)。
            # これにより AI 発話中にユーザーが話し始めると Gemini 側で
            # インタラプトが発生し event.interrupted=True イベントが届く。
            realtime_input_config=types.RealtimeInputConfig(
                activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
            ),
        )

        await self._send_json({"type": "session_ready"})
        logger.info(
            "VoiceSession ready (user=%s, adk_session=%s)",
            self._hub_user,
            self._adk_session_id,
        )

        try:
            await self._main_loop(run_config)
        finally:
            if self._queue:
                self._queue.close()
                self._queue = None
            self._adk_session_id = None
            # hub_manager_loop の CancelledError 後に確実にクリア (二重呼び出し安全)
            hub_tools.clear_hub_session()
            self._hub = None

    async def _main_loop(self, run_config: RunConfig) -> None:
        """upstream / downstream / hub_manager を並行実行。

        upstream (ブラウザ切断) か downstream (Gemini エラー) が終了したら全停止。
        hub_manager は自動再接続するため wait 対象に含めない — hub 切断だけでは
        セッションを終了しない。
        """
        upstream_task = asyncio.create_task(
            self._browser_recv_loop(), name="upstream"
        )
        downstream_task = asyncio.create_task(
            self._gemini_recv_loop(run_config), name="downstream"
        )
        hub_task = asyncio.create_task(
            self._hub_manager_loop(), name="hub_manager"
        )

        try:
            # upstream / downstream どちらかが完了 (= ブラウザ切断 / Gemini エラー)
            # したら残りをキャンセル。hub_manager は対象外 (再接続ループのため)。
            await asyncio.wait(
                {upstream_task, downstream_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in [upstream_task, downstream_task, hub_task]:
                t.cancel()
            await asyncio.gather(
                upstream_task, downstream_task, hub_task, return_exceptions=True
            )

    async def _hub_manager_loop(self) -> None:
        """agent-hub 接続を確立し、切断時に自動再接続する。

        MCP セッションが server-side ping 失敗等で切断されても Gemini セッションを
        継続させるための再接続ループ。再接続中は hub_tools が "hub not connected" を
        返すが、音声会話自体は途切れない。

        バックオフ: 初回 1s → 最大 30s (指数)。
        """
        backoff = _HUB_RECONNECT_BACKOFF_MIN_S
        while True:
            try:
                async with AgentHub.connect(
                    user=self._hub_user,
                    url=self._hub_url,
                    pat=self._hub_pat,
                    tenant=self._hub_tenant,
                ) as hub:
                    hub_tools.set_hub_session(hub)
                    self._hub = hub
                    self._pikon_listener = PikonListener(
                        hub=hub, on_message=self._on_pikon
                    )
                    backoff = _HUB_RECONNECT_BACKOFF_MIN_S  # 接続成功でリセット
                    logger.info("hub_manager: agent-hub に接続しました")
                    try:
                        await self._pikon_listener.listen()
                    finally:
                        hub_tools.clear_hub_session()
                        self._hub = None
                        self._pikon_listener = None
            except asyncio.CancelledError:
                # タスクキャンセル (セッション終了) — ループを抜ける
                raise
            except Exception as e:
                logger.warning(
                    "hub_manager: 切断 (reason: %s) — %.1fs 後に再接続", e, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _HUB_RECONNECT_BACKOFF_MAX_S)

    # =========================================================================
    # 認証
    # =========================================================================

    async def _authenticate(self) -> bool:
        """OTP 認証を行う。タイムアウトまたは失敗時は False を返す。"""
        try:
            msg = await asyncio.wait_for(self.ws.receive(), timeout=AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            await self._send_error("auth_timeout", "OTP 入力がタイムアウトしました")
            return False

        if msg.type != WSMsgType.TEXT:
            await self._send_error(
                "auth_failed", "最初のメッセージは JSON テキストである必要があります"
            )
            return False

        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            await self._send_error("auth_failed", "Invalid JSON")
            return False

        if data.get("type") != "auth":
            await self._send_error(
                "auth_failed",
                '{"type":"auth","code":"123456"} を送信してください',
            )
            return False

        code = str(data.get("code", ""))
        if not self.otp_store.validate(code):
            await self._send_error("auth_failed", "OTP が無効または期限切れです")
            logger.warning("Auth failed: invalid OTP attempt")
            return False

        await self._send_json({"type": "auth_ok"})
        logger.info("Auth OK")
        return True

    # =========================================================================
    # browser → ADK (upstream)
    # =========================================================================

    async def _browser_recv_loop(self) -> None:
        """ブラウザからのメッセージ (PCM audio / control JSON) を処理する。"""
        async for msg in self.ws:
            if msg.type == WSMsgType.BINARY:
                self._send_audio_to_queue(msg.data)
            elif msg.type == WSMsgType.TEXT:
                try:
                    await self._handle_control(json.loads(msg.data))
                except json.JSONDecodeError:
                    pass
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                logger.info("Browser WS closed")
                return
            elif msg.type == WSMsgType.ERROR:
                logger.error("Browser WS error")
                return

    def _send_audio_to_queue(self, pcm: bytes) -> None:
        """PCM 音声データを LiveRequestQueue に送信する (同期)。

        AI 発話中も含めて常に送信する。
        Gemini の server-side VAD (activity_handling=START_OF_ACTIVITY_INTERRUPTS)
        がユーザー音声を検知したタイミングで自律的にモデルを停止させる。
        ミュートによるゲートは廃止: ミュートするとユーザー発話が Gemini に届かず
        VAD が発火しないため、AI が止まらなくなる。
        """
        queue = self._queue
        if queue is None:
            return
        try:
            queue.send_realtime(
                blob=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000")
            )
        except Exception as e:
            logger.error("Failed to send audio to queue: %s", e)

    async def _handle_control(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "interrupt":
            # 手動インタラプト (UI ボタン等): activity_start で AI を停止要求。
            # 通常は Gemini VAD による自律インタラプトで十分だが、
            # 明示的な停止要求として activity_start を送る。
            logger.info("Interrupt requested by browser")
            self.is_gemini_speaking = False  # pikon pending メッセージを即注入させる
            queue = self._queue
            if queue:
                try:
                    queue.send_activity_start()
                    logger.info("activity_start sent via explicit browser interrupt")
                except Exception as e:
                    logger.error("Failed to send activity_start: %s", e)
        elif t == "stop_session":
            await self.ws.close()

    # =========================================================================
    # ADK → browser (downstream)
    # =========================================================================

    async def _gemini_recv_loop(self, run_config: RunConfig) -> None:
        """ADK Runner からのイベント (audio / transcript / turn_complete) を処理する。"""
        try:
            async for event in self._runner.run_live(
                user_id=self._hub_user,
                session_id=self._adk_session_id,
                live_request_queue=self._queue,
                run_config=run_config,
            ):
                # インタラプト: ユーザー発話でモデルが途中停止したことをブラウザに通知
                # ブラウザ側で _playbackQueue をフラッシュして再生を即止める。
                if event.interrupted:
                    self.is_gemini_speaking = False
                    self._model_transcript_partial_sent = False
                    # interrupted 後は新たなユーザー発話シーケンスが始まるため
                    # user transcript フラグもリセットする。
                    # (turn_complete と非対称だった点を修正 — reviewer 指摘)
                    self._user_transcript_partial_sent = False
                    await self._send_json({"type": "interrupted"})
                    logger.info("Model interrupted — sent interrupted to browser")

                # 音声コンテンツをブラウザに転送
                if event.content and event.content.parts:
                    self.is_gemini_speaking = True
                    for part in event.content.parts:
                        if part.inline_data:
                            try:
                                await self.ws.send_bytes(part.inline_data.data)
                            except Exception:
                                return  # WS closed

                # トランスクリプト
                # ADK は partial (streaming chunk) と non-partial (最終完全テキスト)
                # 両方の transcription event を yield する。
                # partial を送信済みの場合は non-partial をスキップして browser での
                # 2重表示を防ぐ。partial が未送信 (ASR が最終結果のみ返すケース) は
                # non-partial を送信する。
                # NOTE: ADK バージョンによって transcription フィールドが
                #       str または .text 属性を持つオブジェクトになる場合がある。
                #       runtime で正規化して送信する。
                if event.input_transcription:
                    text = event.input_transcription
                    if hasattr(text, "text"):
                        text = text.text
                    text_str = str(text)
                    if text_str:
                        if event.partial is True:
                            self._user_transcript_partial_sent = True
                            await self._send_json({
                                "type": "transcript",
                                "speaker": "user",
                                "text": text_str,
                            })
                        elif not self._user_transcript_partial_sent:
                            # partial 未送信: non-partial (最終結果) を送信
                            await self._send_json({
                                "type": "transcript",
                                "speaker": "user",
                                "text": text_str,
                            })
                        # else: partial 送信済み → non-partial は集積テキストの重複のためスキップ

                if event.output_transcription:
                    text = event.output_transcription
                    if hasattr(text, "text"):
                        text = text.text
                    text_str = str(text)
                    if text_str:
                        if event.partial is True:
                            self._model_transcript_partial_sent = True
                            await self._send_json({
                                "type": "transcript",
                                "speaker": "model",
                                "text": text_str,
                            })
                        elif not self._model_transcript_partial_sent:
                            # partial 未送信: non-partial (最終結果) を送信
                            await self._send_json({
                                "type": "transcript",
                                "speaker": "model",
                                "text": text_str,
                            })
                        # else: partial 送信済み → non-partial は集積テキストの重複のためスキップ

                # ターン完了: フラグリセット → browser 通知 → pending メッセージ注入
                if event.turn_complete:
                    self.is_gemini_speaking = False
                    self._model_transcript_partial_sent = False
                    self._user_transcript_partial_sent = False
                    await self._send_json({"type": "turn_complete"})
                    await self._inject_pending_messages()

        except Exception as e:
            logger.error("Gemini recv loop error: %s", e)
            await self._send_error("gemini_error", str(e))

    # =========================================================================
    # pikon! 通知
    # =========================================================================

    async def _on_pikon(self, messages: list[IncomingMessage]) -> None:
        """SDK inbox() による新メッセージ到着通知。

        /generate-code はセッション接続中でも最優先で処理する:
          1. OTP を生成して sender に返信（CommandListener と同じ形式）
          2. ブラウザに session_terminated を通知してから WS を閉じる
          3. WS 閉鎖 → upstream_task 終了 → _main_loop クリーンアップ

        /generate-code を送った = 新しい接続を取りに行く意思があるため、
        既存セッションを拒否するのではなく切断して制御を渡す設計。
        通常メッセージは Gemini (ADK セッション) に転送する。
        """
        if not messages:
            return

        # /generate-code を Gemini に転送せず直接処理する
        forwarded: list[IncomingMessage] = []
        disconnect_requested = False
        for msg in messages:
            body = (msg.body or "").strip()
            if body.lower() == CMD_GENERATE_CODE:
                # セッション接続中: OTP を発行して既存セッションを切断する。
                # /generate-code を発行した = 新しい接続を取りに行く意思があるため、
                # 拒否するのではなく既存セッションを切断して制御を渡す。
                try:
                    if self._hub is not None:
                        code, ttl = self.otp_store.generate()
                        ttl_min = ttl // 60
                        reply = (
                            f"🔑 **{code}** ({ttl_min}分有効)\n\n"
                            "スマホブラウザでこのコードを入力してセッションを開始してください。"
                        )
                        await self._hub.send(msg.sender, reply)
                        await self._hub.ack(msg.id)
                        logger.info(
                            "/generate-code during session: OTP sent to %s (code=%s****), disconnecting",
                            msg.sender,
                            code[:2],
                        )
                        disconnect_requested = True
                except Exception as e:
                    logger.error(
                        "Failed to handle /generate-code during session: %s", e
                    )
            else:
                forwarded.append(msg)

        if disconnect_requested:
            # ブラウザに切断通知を送ってから WS を閉じる。
            # WS が閉じると upstream_task が終了し、_main_loop のクリーンアップが走る。
            await self._send_json({
                "type": "session_terminated",
                "reason": "new_session_requested",
            })
            await asyncio.sleep(0.1)
            try:
                await self.ws.close()
            except Exception:
                pass
            return

        if not forwarded:
            return

        # pikon! 通知を browser に送信
        first = forwarded[0]
        preview = (first.body or "")[:50]
        await self._send_json({
            "type": "pikon",
            "from": first.sender,
            "preview": preview,
        })

        # Gemini が発話中なら pending に積んで次の turnComplete を待つ
        if self.is_gemini_speaking:
            self._pending_messages.extend(forwarded)
        else:
            await self._inject_messages_to_gemini(forwarded)

    async def _inject_pending_messages(self) -> None:
        """turnComplete 時に pending メッセージを Gemini context に注入する。

        PikonListener (hub.inbox()) が push/poll でメッセージを届けるため、
        ここでは _pending_messages バッファのみを処理する。
        """
        if not self._pending_messages:
            return

        # 重複排除 (id で)
        seen: set[str] = set()
        unique: list[IncomingMessage] = []
        for m in self._pending_messages:
            if m.id not in seen:
                seen.add(m.id)
                unique.append(m)

        self._pending_messages.clear()
        await self._inject_messages_to_gemini(unique[:5])  # 最大 5 件

    async def _inject_messages_to_gemini(self, messages: list[IncomingMessage]) -> None:
        """メッセージリストを LiveRequestQueue 経由で Gemini context に注入する。"""
        queue = self._queue
        if queue is None or not messages:
            return

        lines = ["【agent-hub 未読メッセージ】"]
        for m in messages:
            lines.append(f"  {m.sender}: {m.body}")
        context_text = "\n".join(lines)

        try:
            # NOTE: send_content() はモデルに即時応答を促す挙動になる場合がある。
            # 注入は常に turn_complete 直後 (ユーザーが発話していないタイミング) に
            # 行うことで、発話中に割り込まない設計にしている。
            # ただしモデルが即座に音声で返答するかは実装依存。
            queue.send_content(
                content=types.Content(
                    role="user",
                    parts=[types.Part(text=context_text)],
                )
            )
            logger.info("Injected %d messages to Gemini context", len(messages))
        except Exception as e:
            logger.error("Failed to inject messages to Gemini: %s", e)

    # =========================================================================
    # ユーティリティ
    # =========================================================================

    async def _send_json(self, obj: dict) -> None:
        try:
            await self.ws.send_json(obj)
        except Exception:
            pass  # WS 既に閉じている場合は無視

    async def _send_error(self, code: str, message: str) -> None:
        await self._send_json({"type": "error", "code": code, "message": message})
        await asyncio.sleep(0.1)
        try:
            await self.ws.close()
        except Exception:
            pass
