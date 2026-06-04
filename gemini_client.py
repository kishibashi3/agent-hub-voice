"""
Gemini Live API クライアントラッパー。
google-genai SDK の BidiGenerateContent セッションを管理する。
"""
import logging

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


class GeminiLiveClient:
    """
    Gemini Live WebSocket セッションのラッパー。
    audio input/output + function calling を担当する。
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        tools: list[types.Tool],
        system_prompt: str,
        voice_name: str = "Kore",
    ) -> None:
        self.client = genai.Client(
            api_key=api_key,
            http_options={"api_version": "v1beta"},
        )
        self.model = model
        self.config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                )
            ),
            tools=tools,
            system_instruction=types.Content(
                parts=[types.Part(text=system_prompt)]
            ),
        )
        logger.debug("GeminiLiveClient initialized (model=%s, voice=%s)", model, voice_name)

    def connect(self):
        """
        Gemini Live セッションに接続する。
        async context manager として使用:

            async with client.connect() as session:
                await session.send(...)
                async for message in session.receive():
                    ...
        """
        return self.client.aio.live.connect(model=self.model, config=self.config)
