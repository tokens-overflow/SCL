"""pytest 公共 fixture。

测试设计的两条原则：

1. **测试绝不依赖真实 LLM API。** 全部使用 MockLLMProvider。
   真实模型每次输出都可能不同，于是「测试挂了」就失去了信号价值——
   你分不清是代码错了还是模型飘了。

2. **每个测试一个独立的内存数据库。** 共享数据库会让测试之间
   产生隐性依赖，然后你会遇到「单跑通过、一起跑失败」这种最难查的问题。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

# 必须在导入 app 之前设置：配置是在首次 import 时读取的。
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("SEED_DEMO_DATA", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("STALE_RUNNING_SECONDS", "1")

from app.actions.registry import ToolRegistry, register_builtin_tools  # noqa: E402
from app.actions.tools.fault_injection import fault_injector  # noqa: E402
from app.control.policies import RateLimitPolicy  # noqa: E402
from app.control.policy_engine import PolicyEngine, build_default_policy_engine  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.examples.discount_workflow import seed_demo_data  # noqa: E402
from app.llm.mock_provider import MockLLMProvider  # noqa: E402
from app.operations.metrics import metrics  # noqa: E402
from app.runtime.events import EventBus  # noqa: E402
from app.runtime.orchestrator import Orchestrator  # noqa: E402
from app.state.database import Database  # noqa: E402


@pytest.fixture
def settings() -> Any:
    """测试配置。"""
    return get_settings()


@pytest_asyncio.fixture
async def database(settings: Any) -> AsyncIterator[Database]:
    """每个测试一个独立的内存数据库。

    Note:
        SQLite 内存库是**按连接**隔离的，所以这里必须让 SQLAlchemy
        复用同一个连接（默认的 SingletonThreadPool 对 aiosqlite
        已经能保证这一点）。表在每个测试开始时重建。
    """
    db = Database(settings)
    await db.create_all()
    try:
        yield db
    finally:
        await db.dispose()


@pytest_asyncio.fixture
async def session(database: Database) -> AsyncIterator[Any]:
    """事务性数据库会话。"""
    async with database.session() as s:
        yield s


@pytest_asyncio.fixture
async def seeded_session(database: Database) -> AsyncIterator[Any]:
    """带演示客户数据的会话。"""
    async with database.session() as s:
        await seed_demo_data(s)
        yield s


@pytest.fixture
def registry() -> ToolRegistry:
    """独立的工具注册表（避免测试之间互相污染）。"""
    reg = ToolRegistry()
    register_builtin_tools(reg)
    return reg


@pytest.fixture
def rate_limiter() -> RateLimitPolicy:
    """独立的限流器。"""
    return RateLimitPolicy()


@pytest.fixture
def policy_engine(registry: ToolRegistry, settings: Any, rate_limiter: RateLimitPolicy) -> PolicyEngine:
    """默认策略链。"""
    return build_default_policy_engine(registry, settings=settings, rate_limiter=rate_limiter)


@pytest.fixture
def llm() -> MockLLMProvider:
    """Mock LLM Provider。

    它的 `call_count` 在测试里很有用：
    可以断言「恢复流程里一次模型调用都没有」。
    """
    return MockLLMProvider()


@pytest.fixture
def event_bus_instance() -> EventBus:
    """独立的事件总线。"""
    return EventBus()


@pytest.fixture
def make_orchestrator(
    registry: ToolRegistry,
    policy_engine: PolicyEngine,
    llm: MockLLMProvider,
    settings: Any,
    event_bus_instance: EventBus,
) -> Any:
    """返回一个用给定 session 装配 Orchestrator 的工厂。"""

    def _factory(session: Any) -> Orchestrator:
        return Orchestrator(
            session=session,
            registry=registry,
            policy_engine=policy_engine,
            llm=llm,
            settings=settings,
            bus=event_bus_instance,
        )

    return _factory


@pytest.fixture
def orchestrator(make_orchestrator: Any, seeded_session: Any) -> Orchestrator:
    """带演示数据的 Orchestrator。"""
    return make_orchestrator(seeded_session)


@pytest.fixture(autouse=True)
def _reset_global_state() -> Any:
    """每个测试前后清空全局状态。

    故障注入器和指标是进程级的，不清理会造成测试之间互相污染——
    这类污染的症状是「单跑通过、一起跑失败」，非常难查。
    """
    fault_injector.clear()
    metrics.reset()
    yield
    fault_injector.clear()
    metrics.reset()
