"""FastAPI 依赖注入。

把「怎么装配一个 Orchestrator」这件事收敛到一处。

**为什么 Orchestrator 是每请求新建，而不是单例？**

因为它绑定一个数据库会话，而会话不能跨请求共享——
共享会话会导致一个请求的未提交写入被另一个请求看到，
或者一个请求的回滚把另一个请求的写入一起清掉。

而注册表、策略引擎、LLM Provider 这些是**无状态**的，
所以做成进程级单例，避免每次请求都重建。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.registry import ToolRegistry, default_registry, register_builtin_tools
from app.control.policies import RateLimitPolicy
from app.control.policy_engine import PolicyEngine, build_default_policy_engine
from app.core.config import Settings, get_settings
from app.llm.base import LLMProvider
from app.llm.factory import create_llm_provider
from app.runtime.orchestrator import Orchestrator
from app.state.database import Database, get_database

#: 进程级单例。它们都是无状态的，可安全共享。
_registry: ToolRegistry | None = None
_policy_engine: PolicyEngine | None = None
_llm: LLMProvider | None = None
#: 限流器需要跨请求共享计数器，所以必须是单例。
_rate_limiter: RateLimitPolicy | None = None


def get_registry() -> ToolRegistry:
    """获取工具注册表（含内置工具）。"""
    global _registry
    if _registry is None:
        _registry = register_builtin_tools(default_registry)
    return _registry


def get_rate_limiter() -> RateLimitPolicy:
    """获取共享的限流器。"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimitPolicy()
    return _rate_limiter


def get_policy_engine(settings: Settings | None = None) -> PolicyEngine:
    """获取策略引擎。"""
    global _policy_engine
    if _policy_engine is None:
        _policy_engine = build_default_policy_engine(
            get_registry(), settings=settings or get_settings(), rate_limiter=get_rate_limiter()
        )
    return _policy_engine


def get_llm_provider(settings: Settings | None = None) -> LLMProvider:
    """获取 LLM Provider。

    Note:
        `allow_fallback=False`：配置了真实 Provider 却缺密钥时**明确报错**，
        绝不静默降级到 Mock。静默降级在生产环境是灾难——
        你以为在用真模型，实际在跑一套规则，而且没有任何告警。
    """
    global _llm
    if _llm is None:
        _llm = create_llm_provider(settings or get_settings(), allow_fallback=False)
    return _llm


def reset_singletons() -> None:
    """清空所有单例（测试用）。"""
    global _registry, _policy_engine, _llm, _rate_limiter
    _registry = None
    _policy_engine = None
    _llm = None
    _rate_limiter = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：提供事务性数据库会话。"""
    db: Database = get_database()
    async with db.session() as session:
        yield session


def build_orchestrator(session: Any, settings: Settings | None = None) -> Orchestrator:
    """装配一个 Orchestrator。

    Args:
        session: 数据库会话。
        settings: 配置对象。

    Returns:
        绑定该会话的 :class:`Orchestrator`。
    """
    settings = settings or get_settings()
    return Orchestrator(
        session=session,
        registry=get_registry(),
        policy_engine=get_policy_engine(settings),
        llm=get_llm_provider(settings),
        settings=settings,
    )


async def get_orchestrator(
    session: AsyncSession,
) -> Orchestrator:
    """FastAPI 依赖：提供 Orchestrator。"""
    return build_orchestrator(session)
