"""数据库引擎与会话管理。

**为什么状态必须持久化，而不能只放在 LLM 上下文或进程内存里？**

因为进程会挂。容器会被回收、机器会重启、部署会滚动。
如果「任务做到第几步」只存在于内存或模型上下文里，那么进程一没，
这个任务就成了孤儿：外部系统那边可能已经生效（钱动了），
而你的库里查无此事。重试时只能整单重来 → 重复下单、重复付款、重复发通知。

所以状态层的第一性要求是：**每一步开始前先落库登记意图，结束后再落盘结果。**
两者中间挂掉，恢复时会看到一条 RUNNING 的悬挂记录，
拿幂等键去外部系统对账就能查明真相。

关于数据库可替换性：

* 默认 `sqlite+aiosqlite`，开发零依赖。
* 切 PostgreSQL 只需要改 `DATABASE_URL` 环境变量为
  `postgresql+asyncpg://user:pass@host:5432/agent`，业务代码零改动。
* 为此，仓库层只使用 SQLAlchemy 2.x 的 ORM API，
  不写任何方言相关的裸 SQL，也不依赖 SQLite 特有行为。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import Settings, get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""


class Database:
    """封装引擎与会话工厂。

    做成类而不是模块级全局变量，是为了让测试能够干净地创建独立实例
    （每个测试一个临时库），而不是靠 monkeypatch 全局状态。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        """懒加载的异步引擎。"""
        if self._engine is None:
            kwargs: dict[str, Any] = {
                "echo": self.settings.database_echo,
                "future": True,
            }
            if not self.settings.is_sqlite:
                # 连接池参数只对真实数据库有意义；SQLite 用默认即可。
                kwargs["pool_size"] = self.settings.db_pool_size
                kwargs["max_overflow"] = self.settings.db_max_overflow
                kwargs["pool_pre_ping"] = True
            self._engine = create_async_engine(self.settings.database_url, **kwargs)
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """会话工厂。

        `expire_on_commit=False` 很关键：提交之后我们经常还要读对象属性
        （例如写审计日志），默认行为会触发额外的 lazy load，
        在 async 场景下会直接抛 `MissingGreenlet`。
        """
        if self._session_factory is None:
            self._session_factory = async_sessionmaker(
                bind=self.engine,
                expire_on_commit=False,
                autoflush=False,
            )
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """提供一个事务性会话。

        Yields:
            AsyncSession。正常退出时提交，异常时回滚。

        Warning:
            **数据库事务回滚 ≠ 业务补偿。**
            这里的回滚只能撤销「我们自己库里的写入」，
            对已经打到外部系统的副作用（已发放的折扣、已发出的短信）毫无作用。
            撤销外部副作用必须走 :mod:`app.actions.compensation` 的补偿流程。
        """
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_all(self) -> None:
        """建表。

        Demo 与测试用；生产环境应使用 Alembic 迁移（见 `alembic/`）。
        两者的表结构定义来自同一份 ORM 模型，不会漂移。
        """
        # 导入以确保所有模型都已注册到 Base.metadata。
        from app.state import models  # noqa: F401

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        """删表（仅测试用）。"""
        from app.state import models  # noqa: F401

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        """释放连接池。"""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None


#: 进程级默认实例。FastAPI 依赖注入与脚本共用它。
_database: Database | None = None


def get_database(settings: Settings | None = None) -> Database:
    """获取全局 Database 实例。"""
    global _database
    if _database is None:
        _database = Database(settings)
    return _database


def set_database(db: Database) -> None:
    """替换全局 Database 实例（测试用）。"""
    global _database
    _database = db


async def reset_database() -> None:
    """释放并清空全局实例（测试用）。"""
    global _database
    if _database is not None:
        await _database.dispose()
    _database = None
