"""运行时配置 —— 全部从 ``config.yaml`` 加载。

设计要点：
- 单一配置源：``backend/config.yaml`` 同时承载 LLM provider 与应用调优项
- env 仅用于 ``${VAR}`` 占位（secret 类字段），文件位置可由 ``CONFIG_PATH`` 覆盖
- 嵌套 ``pydantic.BaseModel`` 校验，不再依赖 ``pydantic_settings``
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

Language = Literal["zh", "en"]

# ---------------------------------------------------------------------------
# ${VAR} 占位展开
# ---------------------------------------------------------------------------
# 支持两种形态：
#   ${VAR}              未设环境变量时替换为空串
#   ${VAR:-default}     未设环境变量时替换为 "default"
#                       （兼容 bash 的 ${VAR:-X} 风格，便于云端模板写默认值）
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2) or ""
        return os.environ.get(name, default)

    return _ENV_PATTERN.sub(_sub, value)


def _resolve_env(obj: Any) -> Any:
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {k: _resolve_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# LLM 节（保持原 yaml 结构：active + providers）
# ---------------------------------------------------------------------------
class LlmSection(BaseModel):
    """LLM provider 选择与定义。

    保持原 yaml 形态：``active`` 选一个 key，``providers`` 是 ``name -> dict``。
    具体 provider 配置字段由各 adapter 自行消费，此处不强校验。
    """

    active: str
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def active_provider(self) -> dict[str, Any]:
        if self.active not in self.providers:
            raise RuntimeError(
                f"config.yaml: active provider '{self.active}' 未在 llm.providers 节中定义"
            )
        return self.providers[self.active]


# ---------------------------------------------------------------------------
# App 节
# ---------------------------------------------------------------------------
class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"]
    )
    log_level: str = "INFO"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value


class AgentConfig(BaseModel):
    max_tasks: int = Field(default=5, ge=1, le=10)
    task_concurrency: int = Field(default=3, ge=1, le=8)
    default_language: Language = "zh"


class SchedulerConfig(BaseModel):
    task_stall_timeout_seconds: float = Field(default=30.0, gt=0)


class HttpConfig(BaseModel):
    timeout_seconds: float = Field(default=20.0, gt=0)


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_mult: float = Field(default=0.5, gt=0)
    backoff_max: float = Field(default=8.0, gt=0)


class RetryConfig(BaseModel):
    maps: RetryPolicy = Field(default_factory=RetryPolicy)
    llm: RetryPolicy = Field(default_factory=RetryPolicy)


class CacheConfig(BaseModel):
    dir: str = "./.cache/maps"
    ttl_seconds: int = Field(default=86_400, ge=0)
    in_memory_lru_size: int = Field(default=256, ge=1)

    @property
    def path(self) -> Path:
        return Path(self.dir).expanduser().resolve()


class TemperaturesConfig(BaseModel):
    report: float = Field(default=0.3, ge=0.0, le=2.0)
    itinerary: float = Field(default=0.4, ge=0.0, le=2.0)


class LlmDefaultsConfig(BaseModel):
    max_tokens: int = Field(default=8192, ge=64)
    temperatures: TemperaturesConfig = Field(default_factory=TemperaturesConfig)


class MapsConfig(BaseModel):
    api_key: str = ""
    default_radius: int = Field(default=3000, ge=100, le=50_000)
    default_places_limit: int = Field(default=8, ge=1, le=20)
    default_language_code: str = "zh-CN"


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    llm_defaults: LlmDefaultsConfig = Field(default_factory=LlmDefaultsConfig)
    maps: MapsConfig = Field(default_factory=MapsConfig)


# ---------------------------------------------------------------------------
# 顶层 Configuration
# ---------------------------------------------------------------------------
class Configuration(BaseModel):
    llm: LlmSection
    app: AppConfig = Field(default_factory=AppConfig)
    config_path: Path

    # ------------------------------------------------------------------
    def assert_ready(self) -> None:
        """缺失必要凭证或 yaml 字段时抛出异常。"""
        if not self.app.maps.api_key:
            raise RuntimeError(
                "config.yaml: app.maps.api_key 为空（直填或设置 GOOGLE_MAPS_API_KEY 环境变量）"
            )
        # LLM provider 字段在各 adapter 初始化时再校验

    # ------------------------------------------------------------------
    @classmethod
    def from_yaml(cls, path: str | Path) -> "Configuration":
        p = Path(path)
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.exists():
            raise RuntimeError(f"未找到配置文件：{p}（设 CONFIG_PATH 环境变量可指定路径）")
        with p.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        resolved = _resolve_env(raw)
        if not isinstance(resolved, dict):
            raise RuntimeError(f"config.yaml 顶层必须是 dict，实际为 {type(resolved).__name__}")
        if "llm" not in resolved:
            raise RuntimeError("config.yaml 缺少 `llm:` 节")
        return cls(
            llm=LlmSection(**resolved["llm"]),
            app=AppConfig(**(resolved.get("app") or {})),
            config_path=p,
        )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_PATH = "config.yaml"


@lru_cache(maxsize=1)
def get_configuration() -> Configuration:
    """单例配置。``CONFIG_PATH`` 环境变量可指定 yaml 路径。"""
    path = os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)
    return Configuration.from_yaml(path)
