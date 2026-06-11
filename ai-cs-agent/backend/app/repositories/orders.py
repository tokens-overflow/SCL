from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.domain.enums import OrderStatus
from backend.app.domain.models import Order


def get_order_by_no(session: Session, order_no: str) -> Order | None:
    return session.scalar(select(Order).where(Order.order_no == order_no))


def list_orders_for_user(
    session: Session, user_id: int, status: OrderStatus | None = None
) -> list[Order]:
    stmt = select(Order).where(Order.user_id == user_id).order_by(Order.created_at.desc())
    if status is not None:
        stmt = stmt.where(Order.status == status)
    return list(session.scalars(stmt).all())
