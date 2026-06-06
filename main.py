"""
@deprecated: このリポジトリは非推奨です。voice2 (agent-hub-voice2) に移行してください。

voice-gateway エントリポイント。

aiohttp を使って以下を 1 ポートで提供:
  GET /         → static/index.html (ブラウザ UI)
  GET /voice.js → static/voice.js
  GET /worklet.js → static/worklet.js
  GET /manifest.json → static/manifest.json
  GET /health   → 200 {"status":"ok","session_active":bool}
  GET /ws       → WebSocket upgrade (VoiceSession)

単一セッション制約:
  agent-hub は single PAT mode のため複数接続しても全員が同一 identity になる。
  そのため voice-gateway は同時接続を 1 セッションに制限する。
  2 本目の接続は "session_in_use" エラーで即時拒否。
"""
import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import web

from agent_hub_sdk import AgentHub

from auth import OTPStore
from command_listener import CommandListener, DISPLAY_NAME
import hub_tools
from session import VoiceSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---- 設定 ----------------------------------------------------------------

GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8765"))
AGENT_HUB_URL = os.environ.get("AGENT_HUB_URL", "http://agent-hub:3000/mcp")
AGENT_HUB_USER = os.environ.get("AGENT_HUB_USER", "voice")
AGENT_HUB_TENANT = os.environ.get("AGENT_HUB_TENANT") or None
# SDK は GitHub PAT を必須とする。trust mode は非対応 (production 専用)。
# docker-compose は AGENT_HUB_GITHUB_PAT を渡す。SDK の GITHUB_PAT env var とは別名の
# ため、ここで明示的に取得して connect() に渡す。
AGENT_HUB_GITHUB_PAT = os.environ.get("AGENT_HUB_GITHUB_PAT") or None
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
AGENT_HUB_VOICE_PERSONA = os.environ.get(
    "AGENT_HUB_VOICE_PERSONA",
    (
        "あなたは agent-hub ecosystem の音声インターフェース「voice」です。"
        "ユーザーの発話に応じてメッセージの送受信や参加者確認を行います。"
        "メッセージを送信する前には必ず送信先と内容をユーザーに確認してください。"
        "日本語で応答してください。"
    ),
)

STATIC_DIR = Path(__file__).parent / "static"

# Singleton OTP store (全セッション共有)
otp_store = OTPStore()

# 単一セッション強制ロック。
# asyncio.Lock は同一 event loop 内でのみ有効 (single-thread)。
# locked() チェックと acquire() の間に await がないため TOCTOU は発生しない。
_session_lock = asyncio.Lock()

# 共有 AgentHub セッション (_run_hub_with_reconnect が管理)
_active_hub = None

# アクティブな VoiceSession (None = 音声セッションなし)
# asyncio は single-thread のため、単純な代入でスレッドセーフ。
_active_session: VoiceSession | None = None


# ---- HTTP / WS ハンドラ -------------------------------------------------

async def handle_health(request: web.Request) -> web.Response:
    """ヘルスチェック。セッション状態も返す。"""
    return web.Response(
        text=json.dumps({
            "status": "ok",
            "session_active": _session_lock.locked(),
        }),
        content_type="application/json",
    )


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    """
    WebSocket ハンドラ。

    単一セッション制約:
      _session_lock.locked() が True の場合は session_in_use エラーを返して即閉鎖。
      locked() チェックと async with _session_lock の間に await がないため安全。
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # ---- 単一セッション強制 ----
    if _session_lock.locked():
        logger.warning("WS rejected (session in use): %s", request.remote)
        await ws.send_json({
            "type": "error",
            "code": "session_in_use",
            "message": "別のセッションが既にアクティブです。接続中のセッションが終了してから再接続してください。",
        })
        await asyncio.sleep(0.1)
        await ws.close()
        return ws

    logger.info("WS connected: %s", request.remote)
    async with _session_lock:
        hub = _active_hub
        if hub is None:
            logger.warning("WS rejected: hub not connected")
            await ws.send_json({
                "type": "error",
                "code": "hub_unavailable",
                "message": "agent-hub に接続中です。しばらくお待ちください。",
            })
            await asyncio.sleep(0.1)
            await ws.close()
            return ws

        global _active_session
        session = VoiceSession(
            browser_ws=ws,
            hub=hub,
            hub_user=AGENT_HUB_USER,
            gemini_api_key=GEMINI_API_KEY,
            gemini_model=GEMINI_MODEL,
            system_prompt=AGENT_HUB_VOICE_PERSONA,
            otp_store=otp_store,
        )
        _active_session = session
        try:
            await session.run()
        except Exception:
            logger.exception("Session crashed")
        finally:
            _active_session = None
            logger.info("WS disconnected: %s", request.remote)

    return ws


def _static_handler(filename: str):
    async def handler(request: web.Request) -> web.FileResponse:
        fp = STATIC_DIR / filename
        if not fp.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(fp)
    return handler


# ---- アプリケーション構築 -----------------------------------------------

def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/", _static_handler("index.html"))
    app.router.add_get("/voice.js", _static_handler("voice.js"))
    app.router.add_get("/worklet.js", _static_handler("worklet.js"))
    app.router.add_get("/manifest.json", _static_handler("manifest.json"))
    return app


# ---- Hub 管理 -----------------------------------------------------------

async def _run_hub_with_reconnect(cmd_listener: CommandListener) -> None:
    """単一の AgentHub セッションを管理する。切断時は自動再接続。

    inbox は 1 本だけ開き、メッセージを以下のルールで dispatch する:
      - VoiceSession がアクティブ → VoiceSession._on_pikon() に転送
      - VoiceSession なし → CommandListener.handle() で slash command 処理

    これにより同一 @voice ハンドルで 2 本の MCP セッションが並走する問題を解消する。
    """
    global _active_hub
    backoff = 5.0
    while True:
        try:
            async with AgentHub.connect(
                user=AGENT_HUB_USER,
                url=AGENT_HUB_URL,
                pat=AGENT_HUB_GITHUB_PAT,
                tenant=AGENT_HUB_TENANT,
                display_name=DISPLAY_NAME,
            ) as hub:
                _active_hub = hub
                hub_tools.set_hub_session(hub)
                # 音声セッションが再接続時にアクティブな場合は hub 参照を更新
                if _active_session is not None:
                    _active_session.update_hub(hub)
                backoff = 5.0  # 正常接続できたらリセット
                logger.info("Hub: @%s に接続しました", AGENT_HUB_USER)

                async with hub.inbox() as messages:
                    async for msg in messages:
                        session = _active_session
                        if session is not None:
                            # 音声セッションがアクティブ: pikon として転送
                            await session._on_pikon([msg])
                        else:
                            # 音声セッションなし: CommandListener が処理
                            await cmd_listener.handle(hub, msg)

        except asyncio.CancelledError:
            logger.info("Hub: シャットダウン")
            _active_hub = None
            raise
        except Exception as e:
            logger.error("Hub: 切断 (%s) — %.1fs 後に再接続", e, backoff)
            _active_hub = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ---- エントリポイント ---------------------------------------------------

async def main() -> None:
    # CommandListener (OTP 発行ハンドラ) を生成
    # hub 接続は _run_hub_with_reconnect が管理するため、ここでは接続しない
    cmd_listener = CommandListener(otp_store=otp_store)

    # 単一 Hub セッション管理タスク (inbox dispatch も担当)
    hub_task = asyncio.create_task(
        _run_hub_with_reconnect(cmd_listener), name="hub_manager"
    )

    # HTTP / WS サーバー起動
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", GATEWAY_PORT)
    await site.start()
    logger.info("voice-gateway started on :%d (single-session mode)", GATEWAY_PORT)

    try:
        await asyncio.get_event_loop().create_future()  # run forever
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down...")
    finally:
        hub_task.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
