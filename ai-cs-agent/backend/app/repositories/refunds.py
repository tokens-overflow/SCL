from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.domain.models import Refund


def get_latest_refund_for_order(session: Session, order_id: int) -> Refund | None:
    return session.scalar(
        select(Refund).where(Refund.order_id == order_id).order_by(Refund.id.desc())
    )


def count_refunds(session: Session) -> int:
    return len(session.scalars(select(Refund.refund_no)).all())


def add_refund(session: Session, refund: Refund) -> Refund:
    session.add(refund)
    session.flush()
    return refund
