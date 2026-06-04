"""
agent-hub MCP ツールの Gemini Live FunctionDeclaration マッピング。

expose するのは read/write 系の 5 ツールのみ。
破壊的・管理系ツール (register / create_team / delete_* 等) は expose しない
（音声誤操作防止）。
"""
from google.genai import types

VOICE_FUNCTION_DECLARATIONS: list[types.FunctionDeclaration] = [
    types.FunctionDeclaration(
        name="send_message",
        description=(
            "agent-hub でメッセージを送信する。"
            "宛先は @handle 形式で指定。送信前に必ずユーザーに宛先と内容を確認すること。"
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "to": types.Schema(
                    type=types.Type.STRING,
                    description="宛先 handle (@alice, @team-review 等)",
                ),
                "message": types.Schema(
                    type=types.Type.STRING,
                    description="送信するメッセージ本文",
                ),
            },
            required=["to", "message"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_messages",
        description="自分の未読メッセージを取得する。",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="取得件数上限 (default: 20)",
                ),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="get_history",
        description=(
            "メッセージ履歴を取得する。"
            "特定の相手との履歴やキーワード検索が可能。"
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "with_participant": types.Schema(
                    type=types.Type.STRING,
                    description="相手の @handle (optional)",
                ),
                "keyword": types.Schema(
                    type=types.Type.STRING,
                    description="検索キーワード (optional)",
                ),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="取得件数上限 (default: 20)",
                ),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="get_participants",
        description=(
            "agent-hub に登録されている参加者一覧を取得する。"
            "is_online で在席確認も可。"
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={},
        ),
    ),
    types.FunctionDeclaration(
        name="mark_as_read",
        description="指定メッセージを既読にする。",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "message_id": types.Schema(
                    type=types.Type.STRING,
                    description="既読にするメッセージの ID",
                ),
            },
            required=["message_id"],
        ),
    ),
]

# Gemini Live に渡す tools リスト
VOICE_TOOLS = [types.Tool(function_declarations=VOICE_FUNCTION_DECLARATIONS)]
