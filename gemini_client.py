"""
ADK Runner ファクトリ。

google-adk SDK の Agent + Runner + InMemorySessionService を
Gemini Live Audio セッション用に構成する。

旧 GeminiLiveClient (google-genai 直接接続) の代替。
Runner.run_live() + LiveRequestQueue パターンを使用する。
"""
import logging
import os
import ssl

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from hub_tools import VOICE_HUB_TOOLS

logger = logging.getLogger(__name__)

APP_NAME = "voice-gateway"

# Gemini API (非 Vertex AI) 使用時は ALPN を HTTP/1.1 に制限する。
# デフォルトの SSL コンテキストは h2 をネゴシエートするが、
# HTTP/2 では WebSocket Upgrade が使えないため Live API 接続がタイムアウトする。
# cooking-agent (server/app.py) と同様のパッチを適用する。
if not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
    _orig_create_default_context = ssl.create_default_context

    def _patched_create_default_context(*args, **kwargs):  # type: ignore[no-untyped-def]
        ctx = _orig_create_default_context(*args, **kwargs)
        ctx.set_alpn_protocols(["http/1.1"])
        return ctx

    ssl.create_default_context = _patched_create_default_context
    logger.info("SSL ALPN patched to HTTP/1.1 (non-Vertex AI mode)")


def create_voice_runner(
    api_key: str,
    model: str,
    system_prompt: str,
) -> tuple[Runner, InMemorySessionService]:
    """
    Gemini Live 音声セッション用の ADK Runner を構成して返す。

    Args:
        api_key: Gemini API キー (GOOGLE_API_KEY env var にも設定する)
        model:   Gemini モデル名 (e.g. "gemini-3.1-flash-live-preview")
        system_prompt: システムプロンプト (Agent.instruction に渡す)

    Returns:
        (runner, session_service) のタプル
    """
    # Fail-fast: API キー未設定は起動時に検出する (ecosystem 方針 #1)。
    # api_key="" (空文字列) は未設定と同義。接続後の認証エラーを防ぐ。
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY must be set. "
            "Set the GEMINI_API_KEY environment variable."
        )
    # GOOGLE_API_KEY を env に設定する。
    # NOTE: 環境変数はプロセス全体に影響する global mutation だが、
    #       voice-gateway は single-session のため複数 Runner が
    #       異なる API キーで並走することはない。
    os.environ["GOOGLE_API_KEY"] = api_key

    agent = Agent(
        name="voice_gateway",
        model=model,
        instruction=system_prompt,
        tools=VOICE_HUB_TOOLS,
    )

    session_service = InMemorySessionService()

    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    logger.info("ADK Runner initialized (model=%s)", model)
    return runner, session_service
