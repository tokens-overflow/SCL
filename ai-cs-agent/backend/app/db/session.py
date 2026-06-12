"""Database connection management."""

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Session:
    """Return a new database session; callers must close it."""
    return SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务边界：正常退出 commit，异常 rollback，最终必 close。

    工具执行（registry.execute_tool）与日志落库都走这里，
    service 层只管业务逻辑，不再自己 commit。
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
