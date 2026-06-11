from sqlalchemy.orm import Session

from backend.app.domain.models import ChatLog


def add_chat_log(
    session: Session,
    session_id: str,
    role: str,
    content: str,
    meta: dict | None = None,
) -> ChatLog:
    log = ChatLog(session_id=session_id, role=role, content=content, meta=meta)
    session.add(log)
    session.flush()
    return log
