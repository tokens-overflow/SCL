"""退款/工单号生成：前缀 + 年月 + 当月 4 位序号（如 RFD2026060001）。

序号取自当月已有单号的最大值 + 1，跨月自动从 0001 重新开始。
非并发安全（demo 限制），生产应换数据库序列或分布式 ID。
"""
from datetime import datetime

from sqlalchemy.orm import Session

from backend.app.repositories.refunds import latest_refund_no
from backend.app.repositories.tickets import latest_ticket_no


def _next_no(prefix: str, latest: str | None) -> str:
    seq = int(latest[len(prefix):]) + 1 if latest else 1
    return f"{prefix}{seq:04d}"


def next_refund_no(session: Session) -> str:
    prefix = f"RFD{datetime.now():%Y%m}"
    return _next_no(prefix, latest_refund_no(session, prefix))


def next_ticket_no(session: Session) -> str:
    prefix = f"TKT{datetime.now():%Y%m}"
    return _next_no(prefix, latest_ticket_no(session, prefix))
