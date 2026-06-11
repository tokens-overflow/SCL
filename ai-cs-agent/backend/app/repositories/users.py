from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.domain.models import User


def get_user_by_phone(session: Session, phone: str) -> User | None:
    return session.scalar(select(User).where(User.phone == phone))


def get_user_by_id(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)
