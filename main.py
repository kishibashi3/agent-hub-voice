"""
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

from auth import OTPStore
from command_listener import CommandListener
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
        session = VoiceSession(
            browser_ws=ws,
            hub_url=AGENT_HUB_URL,
            hub_user=AGENT_HUB_USER,
            hub_pat=AGENT_HUB_GITHUB_PAT,
            hub_tenant=AGENT_HUB_TENANT,
            gemini_api_key=GEMINI_API_KEY,
            gemini_model=GEMINI_MODEL,
            system_prompt=AGENT_HUB_VOICE_PERSONA,
            otp_store=otp_store,
        )
        try:
            await session.run()
        except Exception:
            logger.exception("Session crashed")
        finally:
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


# ---- エントリポイント ---------------------------------------------------

async def main() -> None:
    # CommandListener (OTP 発行 + agent-hub inbox listen) 起動
    cmd_listener = CommandListener(
        hub_url=AGENT_HUB_URL,
        hub_user=AGENT_HUB_USER,
        hub_pat=AGENT_HUB_GITHUB_PAT,
        hub_tenant=AGENT_HUB_TENANT,
        otp_store=otp_store,
    )
    cmd_task = asyncio.create_task(cmd_listener.run(), name="command_listener")

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
        cmd_task.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
