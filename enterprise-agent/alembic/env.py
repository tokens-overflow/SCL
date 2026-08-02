"""Alembic 迁移环境。

设计要点：

1. **连接串从环境变量读**，不写进 alembic.ini——那个文件会进仓库。
2. **target_metadata 直接指向 ORM 的 Base.metadata**，
   所以 `alembic revision --autogenerate` 生成的迁移和
   `Database.create_all()` 建出来的表结构来自同一份定义，不会漂移。
3. 同时支持同步与异步驱动：SQLite 用 aiosqlite、PostgreSQL 用 asyncpg，
   两者都能跑同一套迁移脚本。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 导入所有模型，确保它们注册到 Base.metadata。
from app.core.config import get_settings
from app.state import models  # noqa: F401
from app.state.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 从应用配置注入连接串——单一事实来源。
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连数据库。

    生产环境常用这个模式：把 SQL 交给 DBA 审核后再执行。
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite 不支持大部分 ALTER，用 batch 模式重建表。
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式：直接对数据库执行迁移。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
