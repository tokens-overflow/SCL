import json

from backend.app.db.session import session_scope
from backend.app.repositories.chat_logs import add_chat_log


def log_message(session_id: str, role: str, content: str, meta: dict | None = None) -> None:
    with session_scope() as db:
        add_chat_log(db, session_id=session_id, role=role, content=content, meta=meta)


def log_tool_use(session_id: str, tool_name: str, tool_use_id: str, tool_input: dict) -> None:
    log_message(
        session_id,
        "tool_use",
        json.dumps(tool_input, ensure_ascii=False),
        {"tool": tool_name, "tool_use_id": tool_use_id},
    )


def log_tool_result(
    session_id: str,
    tool_name: str,
    tool_use_id: str,
    result: str,
    is_error: bool,
) -> None:
    log_message(
        session_id,
        "tool_result",
        result,
        {"tool": tool_name, "tool_use_id": tool_use_id, "is_error": is_error},
    )
