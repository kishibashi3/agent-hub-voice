"""
VoiceSession — browser WebSocket ↔ Gemini Live 1:1 セッション (ADK ベース)。

ライフサイクル:
  1. OTP 認証 (auth.py)
  2. agent-hub SDK 接続 (AgentHub.connect)
  3. hub_tools モジュール変数にセッションを注入
  4. ADK セッション作成 (InMemorySessionService)
  5. Runner.run_live() で upstream / downstream 並行実行
     - upstream: browser PCM → LiveRequestQueue.send_realtime()
     - downstream: Runner イベント → browser WS (audio bytes / transcript JSON)
  6. pikon listener で hub inbox を監視し、未読を context 注入
  7. 切断時クリーンアップ (hub_tools.clear_hub_session / queue.close)
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

        # Step 2: agent-hub 接続
        try:
            async with AgentHub.connect(
                user=self._hub_user,
                url=self._hub_url,
                pat=self._hub_pat,
                tenant=self._hub_tenant,
            ) as hub:
                await self._run_with_hub(hub)
        except Exception as e:
            logger.exception("VoiceSession hub connect failed: %s", e)
            await self._send_error("hub_connect_failed", str(e))

    async def _run_with_hub(self, hub) -> None:
        """hub 接続中の処理。hub_tools モジュール変数を通じてツールに hub を渡す。"""
        hub_tools.set_hub_session(hub)
        self._hub = hub  # /generate-code インターセプト用に保持
        self._pikon_listener = PikonListener(hub=hub, on_message=self._on_pikon)

        try:
            await self._run_adk_session()
        except Exception as e:
            logger.exception("VoiceSession error: %s", e)
            await self._send_error("session_error", str(e))
        finally:
            hub_tools.clear_hub_session()
            self._hub = None
            if self._pikon_listener:
                self._pikon_listener.stop()

    async def _run_adk_session(self) -> None:
        """ADK セッションを作成し upstream / downstream / pikon を並行実行する。"""
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

    async def _main_loop(self, run_config: RunConfig) -> None:
        """upstream / downstream / pikon を並行実行し、どれかが終了したら全停止。"""
        upstream_task = asyncio.create_task(
            self._browser_recv_loop(), name="upstream"
        )
        downstream_task = asyncio.create_task(
            self._gemini_recv_loop(run_config), name="downstream"
        )
        pikon_task = asyncio.create_task(
            self._pikon_listener.listen(), name="pikon"
        )

        try:
            # どれか 1 つが完了 (= 切断 / エラー) したら残りをキャンセル
            await asyncio.wait(
                {upstream_task, downstream_task, pikon_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in [upstream_task, downstream_task, pikon_task]:
                t.cancel()
            await asyncio.gather(
                upstream_task, downstream_task, pikon_task, return_exceptions=True
            )

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

        AI 発話中 (is_gemini_speaking=True / turn_complete 待ち) はスキップする。
        ブラウザで再生された AI 音声がマイクに回り込み Gemini の VAD が誤検知する
        echo フィードバックループを断ち切るため。
        turn_complete または interrupted 受信後に自動的に再開する。
        """
        queue = self._queue
        if queue is None:
            return
        if self.is_gemini_speaking:
            return  # AI 発話中はミュート (echo フィードバック防止)
        try:
            queue.send_realtime(
                blob=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000")
            )
        except Exception as e:
            logger.error("Failed to send audio to queue: %s", e)

    async def _handle_control(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "interrupt":
            # 手動インタラプト (UI ボタン等): ミュートを解除し activity_start で停止要求
            logger.info("Interrupt requested by browser")
            self.is_gemini_speaking = False  # ミュート解除 → 次の音声から送信再開
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
