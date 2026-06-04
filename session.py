"""
VoiceSession — browser WebSocket ↔ Gemini Live 1:1 セッション。

ライフサイクル:
  1. OTP 認証 (auth.py)
  2. agent-hub SDK 接続 (AgentHub.connect)
  3. Gemini Live 接続
  4. 音声・制御メッセージの双方向中継
  5. 切断時クリーンアップ
"""
import asyncio
import base64
import json
import logging

from aiohttp import WSMsgType, web
from google.genai import types

from agent_hub_sdk import AgentHub, IncomingMessage

from auth import OTPStore
from functions import VOICE_TOOLS
from gemini_client import GeminiLiveClient
from pikon import PikonListener

logger = logging.getLogger(__name__)

AUTH_TIMEOUT = 30  # 秒: OTP 入力待ちタイムアウト


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
        self.gemini_api_key = gemini_api_key
        self.gemini_model = gemini_model
        self.system_prompt = system_prompt
        self.otp_store = otp_store

        self.gemini_session = None
        self.is_gemini_speaking = False
        self._pending_messages: list[IncomingMessage] = []
        self._pikon_listener: PikonListener | None = None
        self._hub = None  # 接続中の HubSession (tool dispatch 用)

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
                self._hub = hub
                await self._run_with_hub(hub)
        except Exception as e:
            logger.exception("VoiceSession hub connect failed: %s", e)
            await self._send_error("hub_connect_failed", str(e))
        finally:
            self._hub = None

    async def _run_with_hub(self, hub) -> None:
        # Step 3: Gemini Live 接続 + メインループ
        gemini = GeminiLiveClient(
            api_key=self.gemini_api_key,
            model=self.gemini_model,
            tools=VOICE_TOOLS,
            system_prompt=self.system_prompt,
        )

        self._pikon_listener = PikonListener(
            hub=hub,
            on_message=self._on_pikon,
        )

        try:
            async with gemini.connect() as session:
                self.gemini_session = session
                await self._send_json({"type": "session_ready"})
                logger.info("VoiceSession ready (user=%s)", self._hub_user)
                await self._main_loop(session)
        except Exception as e:
            logger.exception("VoiceSession error: %s", e)
            await self._send_error("session_error", str(e))
        finally:
            self.gemini_session = None
            if self._pikon_listener:
                self._pikon_listener.stop()

    async def _main_loop(self, session) -> None:
        """browser_recv / gemini_recv / pikon を並行実行し、どれかが終了したら全停止。"""
        browser_task = asyncio.create_task(
            self._browser_recv_loop(), name="browser_recv"
        )
        gemini_task = asyncio.create_task(
            self._gemini_recv_loop(session), name="gemini_recv"
        )
        pikon_task = asyncio.create_task(
            self._pikon_listener.listen(), name="pikon"
        )

        try:
            # どれか 1 つが完了 (= 切断 / エラー) したら残りをキャンセル
            await asyncio.wait(
                {browser_task, gemini_task, pikon_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in [browser_task, gemini_task, pikon_task]:
                t.cancel()
            await asyncio.gather(browser_task, gemini_task, pikon_task, return_exceptions=True)

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
            await self._send_error("auth_failed", "最初のメッセージは JSON テキストである必要があります")
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
    # browser → Gemini
    # =========================================================================

    async def _browser_recv_loop(self) -> None:
        """ブラウザからのメッセージ (PCM audio / control JSON) を処理する。"""
        async for msg in self.ws:
            if msg.type == WSMsgType.BINARY:
                await self._send_audio_to_gemini(msg.data)
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

    async def _send_audio_to_gemini(self, pcm: bytes) -> None:
        if not self.gemini_session:
            return
        try:
            await self.gemini_session.send(
                input=types.LiveClientRealtimeInput(
                    media_chunks=[
                        types.Blob(
                            data=base64.b64encode(pcm).decode(),
                            mime_type="audio/pcm;rate=16000",
                        )
                    ]
                )
            )
        except Exception as e:
            logger.error("Failed to send audio to Gemini: %s", e)

    async def _handle_control(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "interrupt":
            logger.info("Interrupt requested")
            self.is_gemini_speaking = False
            # NOTE: Gemini Live は現状 interrupt API を未提供。
            #       セッション再接続で対応する場合は将来実装。
        elif t == "stop_session":
            await self.ws.close()

    # =========================================================================
    # Gemini → browser
    # =========================================================================

    async def _gemini_recv_loop(self, session) -> None:
        """Gemini からのメッセージ (audio / function call) を処理する。"""
        async for message in session.receive():
            if message.server_content:
                await self._handle_server_content(message.server_content)
            elif message.tool_call:
                await self._handle_tool_call(message.tool_call)

    async def _handle_server_content(self, sc) -> None:
        # model audio → browser
        if sc.model_turn:
            self.is_gemini_speaking = True
            for part in sc.model_turn.parts:
                if part.inline_data:
                    audio = base64.b64decode(part.inline_data.data)
                    try:
                        await self.ws.send_bytes(audio)
                    except Exception:
                        return  # WS closed

        # transcript → browser
        if sc.input_transcription:
            await self._send_json({
                "type": "transcript",
                "speaker": "user",
                "text": sc.input_transcription.text,
            })
        if sc.output_transcription:
            await self._send_json({
                "type": "transcript",
                "speaker": "model",
                "text": sc.output_transcription.text,
            })

        # turnComplete: ユーザー発話確定 → 未読メッセージ注入
        if sc.turn_complete:
            self.is_gemini_speaking = False
            await self._inject_pending_messages()

    async def _handle_tool_call(self, tool_call) -> None:
        """Gemini function call を hub tool call に変換して実行する。"""
        responses = []
        for fn in tool_call.function_calls:
            logger.info("Tool call: %s(%s)", fn.name, fn.args)
            result = await self._dispatch_function(fn.name, dict(fn.args or {}))
            responses.append(
                types.FunctionResponse(name=fn.name, id=fn.id, response=result)
            )
        if self.gemini_session:
            await self.gemini_session.send(
                input=types.LiveClientToolResponse(function_responses=responses)
            )

    async def _dispatch_function(self, name: str, args: dict) -> dict:
        """Gemini から呼ばれた function を hub API に変換して実行する。"""
        hub = self._hub
        if hub is None:
            return {"error": "hub not connected", "success": False}
        try:
            if name == "send_message":
                await hub.send(args["to"], args["message"])
                return {"result": "sent", "success": True}

            elif name == "get_messages":
                messages = await hub.get_unread()
                result = [
                    {
                        "id": m.id,
                        "from": m.sender,
                        "body": m.body,
                        "timestamp": m.timestamp,
                    }
                    for m in messages
                ]
                return {"result": result, "success": True}

            elif name == "get_participants":
                participants = await hub.get_participants()
                result = [
                    {
                        "name": p.name,
                        "display_name": p.display_name,
                        "mode": p.mode,
                        "is_online": p.is_online,
                    }
                    for p in participants
                ]
                return {"result": result, "success": True}

            elif name == "mark_as_read":
                await hub.ack(args["message_id"])
                return {"result": "marked", "success": True}

            elif name == "get_history":
                text = await hub._call_tool_raw("get_history", args)
                return {"result": text, "success": True}

            else:
                return {"error": f"unknown tool: {name}", "success": False}

        except Exception as e:
            logger.error("Hub tool error [%s]: %s", name, e)
            return {"error": str(e), "success": False}

    # =========================================================================
    # pikon! 通知
    # =========================================================================

    async def _on_pikon(self, messages: list[IncomingMessage]) -> None:
        """SDK inbox() による新メッセージ到着通知。"""
        if not messages:
            return

        # pikon! 通知を browser に送信
        first = messages[0]
        preview = (first.body or "")[:50]
        await self._send_json({
            "type": "pikon",
            "from": first.sender,
            "preview": preview,
        })

        # Gemini が発話中なら pending に積んで次の turnComplete を待つ
        if self.is_gemini_speaking:
            self._pending_messages.extend(messages)
        else:
            await self._inject_messages_to_gemini(messages)

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
        """メッセージリストを Gemini context として注入する。"""
        if not self.gemini_session or not messages:
            return

        lines = ["【agent-hub 未読メッセージ】"]
        for m in messages:
            lines.append(f"  {m.sender}: {m.body}")
        context_text = "\n".join(lines)

        try:
            await self.gemini_session.send(
                input=types.LiveClientContent(
                    turns=[
                        types.Content(
                            role="user",
                            parts=[types.Part(text=context_text)],
                        )
                    ],
                    turn_complete=False,  # 音声入力を待つ（Gemini に即答させない）
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
