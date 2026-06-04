"""
agent-hub MCP ツールの ADK Python function ラッパー。

ADK の function calling は Python async 関数として定義する。
関数の docstring と type hint から ADK が Gemini 向けスキーマを自動生成する。

単一セッションサービスのため、モジュールレベル変数 _hub で
接続中の HubSession を保持する。

set_hub_session() / clear_hub_session() で VoiceSession から制御する。

expose するのは read/write 系の 5 ツールのみ。
破壊的・管理系ツール (register / create_team / delete_* 等) は expose しない
（音声誤操作防止）。
"""
import logging

logger = logging.getLogger(__name__)

# モジュールレベル hub セッション (single-session service なので安全)
_hub = None


def set_hub_session(hub) -> None:
    """VoiceSession 開始時に hub セッションを登録する。"""
    global _hub
    _hub = hub
    logger.debug("Hub session set")


def clear_hub_session() -> None:
    """VoiceSession 終了時に hub セッションを解除する。"""
    global _hub
    _hub = None
    logger.debug("Hub session cleared")


async def send_message(to: str, message: str) -> dict:
    """agent-hub でメッセージを送信する。

    宛先は @handle 形式で指定。送信前に必ずユーザーに宛先と内容を確認すること。

    Args:
        to: 宛先 handle (@alice, @team-review 等)
        message: 送信するメッセージ本文

    Returns:
        送信結果
    """
    hub = _hub
    if hub is None:
        return {"error": "hub not connected", "success": False}
    try:
        await hub.send(to, message)
        return {"result": "sent", "success": True}
    except Exception as e:
        logger.error("send_message error: %s", e)
        return {"error": str(e), "success": False}


async def get_messages(limit: int = 20) -> dict:
    """自分の未読メッセージを取得する。

    Args:
        limit: 取得件数上限

    Returns:
        未読メッセージのリスト
    """
    hub = _hub
    if hub is None:
        return {"error": "hub not connected", "success": False}
    try:
        messages = await hub.get_unread()
        result = [
            {
                "id": m.id,
                "from": m.sender,
                "body": m.body,
                "timestamp": m.timestamp,
            }
            for m in messages[:limit]
        ]
        return {"result": result, "success": True}
    except Exception as e:
        logger.error("get_messages error: %s", e)
        return {"error": str(e), "success": False}


async def get_history(
    with_participant: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
) -> dict:
    """メッセージ履歴を取得する。特定の相手との履歴やキーワード検索が可能。

    Args:
        with_participant: 相手の @handle (optional)
        keyword: 検索キーワード (optional)
        limit: 取得件数上限

    Returns:
        メッセージ履歴
    """
    hub = _hub
    if hub is None:
        return {"error": "hub not connected", "success": False}
    try:
        args: dict = {"limit": limit}
        if with_participant:
            args["with_participant"] = with_participant
        if keyword:
            args["keyword"] = keyword
        # TODO: hub SDK が get_history を直接メソッドとして expose したら
        #       hub._call_tool_raw() を置き換える (内部 API 依存のため)
        text = await hub._call_tool_raw("get_history", args)
        return {"result": text, "success": True}
    except Exception as e:
        logger.error("get_history error: %s", e)
        return {"error": str(e), "success": False}


async def get_participants() -> dict:
    """agent-hub に登録されている参加者一覧を取得する。is_online で在席確認も可。

    Returns:
        参加者リスト (name, display_name, mode, is_online)
    """
    hub = _hub
    if hub is None:
        return {"error": "hub not connected", "success": False}
    try:
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
    except Exception as e:
        logger.error("get_participants error: %s", e)
        return {"error": str(e), "success": False}


async def mark_as_read(message_id: str) -> dict:
    """指定メッセージを既読にする。

    Args:
        message_id: 既読にするメッセージの ID

    Returns:
        処理結果
    """
    hub = _hub
    if hub is None:
        return {"error": "hub not connected", "success": False}
    try:
        await hub.ack(message_id)
        return {"result": "marked", "success": True}
    except Exception as e:
        logger.error("mark_as_read error: %s", e)
        return {"error": str(e), "success": False}


# ADK に渡すツールリスト
VOICE_HUB_TOOLS = [
    send_message,
    get_messages,
    get_history,
    get_participants,
    mark_as_read,
]
