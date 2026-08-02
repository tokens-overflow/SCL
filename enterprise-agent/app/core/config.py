"""集中式配置。

设计要点：

1. **所有阈值都是配置，不是散落在代码里的魔法数字**。折扣上限、重试次数、超时时长
   都属于「业务风险等级决定的参数」，写死在函数体里会导致改一个阈值要翻遍全仓库。

2. **绝不把 Secret 写入源码**。API Key 一律从环境变量读取，仓库里只有 `.env.example`。
   配置对象自身也提供了脱敏的 `safe_dump()`，避免有人顺手把 settings 打进日志。

3. **默认必须能跑**：默认 LLM Provider 是 mock，默认数据库是 SQLite，
   所以 `git clone` 之后不配任何环境变量就能启动完整流程。
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。全部字段都可以通过环境变量或 `.env` 覆盖。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- 应用
    app_name: str = "enterprise-agent"
    environment: Literal["dev", "test", "staging", "prod"] = "dev"
    debug: bool = True

    # ---------------------------------------------------------------- 数据库
    #: 默认 SQLite（开发零依赖）。切 PostgreSQL 只需要改这一个环境变量：
    #:   DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/agent
    #: 仓库层完全基于 SQLAlchemy 2.x 的 ORM API，没有任何 SQLite 方言的裸 SQL，
    #: 所以切换数据库不需要改业务代码。
    database_url: str = "sqlite+aiosqlite:///./enterprise_agent.db"
    database_echo: bool = False
    #: SQLite 不支持真正的连接池参数，切到 PG 时这两个才生效。
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ---------------------------------------------------------------- LLM
    #: 默认 mock：**不需要任何外部 API Key 就能跑通完整流程**。
    llm_provider: Literal["mock", "openai", "anthropic"] = "mock"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2

    openai_api_key: str | None = Field(default=None, repr=False)
    openai_base_url: str | None = None
    openai_model: str = "gpt-4o-mini"

    anthropic_api_key: str | None = Field(default=None, repr=False)
    anthropic_base_url: str | None = None
    #: 默认模型 ID 从环境变量覆盖；这里给一个稳定可用的默认值。
    anthropic_model: str = "claude-sonnet-4-5"
    anthropic_max_tokens: int = 2048

    # ---------------------------------------------------------------- 运行时可靠性
    #: 单次工具执行的默认超时。工具可以在自己的 `default_timeout_seconds` 上覆盖。
    tool_timeout_seconds: float = 10.0
    #: 默认最大重试次数。注意：只对**可重试错误白名单**里的错误生效。
    max_retries: int = 3
    retry_base_delay_seconds: float = 0.5
    retry_max_delay_seconds: float = 30.0
    #: 抖动系数（0~1）。多个任务同时失败时，没有抖动会造成重试风暴同时打到下游。
    retry_jitter_ratio: float = 0.2
    #: RUNNING 超过这个时长仍无更新的步骤视为「悬挂」，恢复时标记为 UNKNOWN 并进对账。
    stale_running_seconds: int = 300
    #: 审批超时回收。没有这条，等审批的任务会永远悬着，永远没有终态。
    approval_timeout_seconds: int = 86_400

    # ---------------------------------------------------------------- 业务规则（折扣示例）
    #: 普通客服可自助批准的折扣上限。
    discount_auto_approve_max: float = 0.05
    #: 需要经理审批的折扣上限；超过这个值直接拒绝。
    discount_manager_approve_max: float = 0.15
    #: VIP 客户的额外放宽额度（仍然要过 Control 层，不是直接放行）。
    discount_vip_bonus: float = 0.03

    # ---------------------------------------------------------------- 安全 / 合规
    #: 是否在送给 LLM 之前对上下文做脱敏。生产环境必须为 True。
    enable_masking: bool = True
    #: 策略版本号，会写进每一条 PolicyDecision，用于事后回放「当时用的是哪版规则」。
    policy_version: str = "2026.07.1"

    # ---------------------------------------------------------------- 可观测性
    log_level: str = "INFO"
    log_json: bool = False
    #: OpenTelemetry 默认关闭：接口预留好，不引入运行期强依赖。
    otel_enabled: bool = False
    otel_service_name: str = "enterprise-agent"
    otel_exporter_otlp_endpoint: str | None = None

    # ---------------------------------------------------------------- 演示开关
    #: 启动时是否写入演示业务数据（客户等）。生产应设为 False。
    seed_demo_data: bool = True

    @field_validator("retry_jitter_ratio")
    @classmethod
    def _check_jitter(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("retry_jitter_ratio 必须在 0~1 之间")
        return v

    @property
    def is_sqlite(self) -> bool:
        """当前是否使用 SQLite。仅用于引擎参数微调，业务代码不应该关心。"""
        return self.database_url.startswith("sqlite")

    def safe_dump(self) -> dict[str, Any]:
        """返回**脱敏后**的配置快照，可安全写入日志与 `/health` 响应。

        规则：任何名字里含 key / secret / token / password 的字段一律只输出
        ``"***set***"`` 或 ``None``，绝不输出原值。
        """
        secret_markers = ("key", "secret", "token", "password")
        out: dict[str, Any] = {}
        for name, value in self.model_dump().items():
            if any(marker in name.lower() for marker in secret_markers):
                out[name] = "***set***" if value else None
            else:
                out[name] = value
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例。

    用 ``lru_cache`` 而不是模块级变量，是为了让测试可以通过
    ``get_settings.cache_clear()`` 强制重载（例如切换到临时数据库）。
    """
    return Settings()


def override_settings(**kwargs: Any) -> Settings:
    """测试专用：覆盖配置并重建单例。

    Args:
        **kwargs: 要覆盖的配置项。

    Returns:
        新的配置单例。
    """
    get_settings.cache_clear()
    for key, value in kwargs.items():
        os.environ[key.upper()] = str(value)
    return get_settings()
