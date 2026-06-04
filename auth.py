"""
OTP (ワンタイムコード) 認証ストア。
voice-gateway への WebSocket 接続に使用する 6 桁コードを管理する。
"""
import logging
import secrets
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

OTP_TTL_SECONDS = 300  # 5 分


@dataclass
class _OTPEntry:
    code: str
    expires_at: float  # time.monotonic() ベース


class OTPStore:
    """
    シングルトン OTP ストア。
    同時に有効なコードは 1 つのみ（新規生成で旧コードを上書き）。
    """

    def __init__(self) -> None:
        self._entry: _OTPEntry | None = None

    def generate(self) -> tuple[str, int]:
        """
        新しい 6 桁 OTP を生成して返す。
        既存コードがあれば無効化する。

        Returns:
            (code, ttl_seconds)
        """
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._entry = _OTPEntry(
            code=code,
            expires_at=time.monotonic() + OTP_TTL_SECONDS,
        )
        logger.info("OTP generated (ttl=%ds)", OTP_TTL_SECONDS)
        return code, OTP_TTL_SECONDS

    def validate(self, code: str) -> bool:
        """
        コードを検証して消費する。有効なら True を返す（使い捨て）。
        """
        if self._entry is None:
            return False
        if time.monotonic() > self._entry.expires_at:
            self._entry = None
            logger.debug("OTP expired")
            return False
        if self._entry.code != code:
            logger.debug("OTP mismatch")
            return False
        self._entry = None  # 使い捨て
        logger.info("OTP validated and consumed")
        return True

    def ttl_remaining(self) -> int:
        """残り有効秒数（なければ 0）。"""
        if self._entry is None:
            return 0
        remaining = self._entry.expires_at - time.monotonic()
        return max(0, int(remaining))
