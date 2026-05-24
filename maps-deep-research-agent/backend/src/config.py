"""运行时配置，从环境变量加载。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


Language = Literal["zh", "en"]


class Configuration(BaseSettings):
    """应用配置。

    从环境变量（字段名大写）加载，也可在 backend/ 下放 ``.env`` 文件。
    LLM provider 的选择交给 ``LLMConfig.yaml`` （见 ``llm_config_path``）。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----- LLM -----------------------------------------------------------
    llm_config_path: str = Field(
        default="LLMConfig.yaml",
        description="LLMConfig.yaml 路径（绝对路径或相对于启动目录）",
    )

    # ----- Google Maps Platform ------------------------------------------
    google_maps_api_key: str = Field(default="", description="Server-side Google Maps API key")
    google_maps_default_radius: int = Field(default=3000, ge=100, le=50_000)
    google_maps_places_limit: int = Field(default=8, ge=1, le=20)

    # ----- Agent ----------------------------------------------------------
    max_tasks: int = Field(default=5, ge=1, le=10)
    task_concurrency: int = Field(default=3, ge=1, le=8)
    default_language: Language = Field(default="zh")

    # ----- Cache ----------------------------------------------------------
    cache_dir: str = Field(default="./.cache/maps")
    cache_ttl_seconds: int = Field(default=86_400, ge=0)

    # ----- Server ---------------------------------------------------------
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: str = Field(default="http://localhost:5173,http://localhost:3000")
    log_level: str = Field(default="INFO")

    @field_validator("cors_origins")
    @classmethod
    def _normalise_origins(cls, value: str) -> str:
        return ",".join(part.strip() for part in value.split(",") if part.strip())

    # ------------------------------------------------------------------
    @property
    def cors_origin_list(self) -> list[str]:
        return [origin for origin in self.cors_origins.split(",") if origin]

    @property
    def cache_path(self) -> Path:
        return Path(self.cache_dir).expanduser().resolve()

    def with_overrides(self, **overrides: Any) -> "Configuration":
        """返回一个覆盖了指定字段的配置副本（用于请求级覆盖）。"""
        data = self.model_dump()
        for key, value in overrides.items():
            if value is None:
                continue
            data[key] = value
        return Configuration(**data)

    def assert_ready(self) -> None:
        """缺失必要凭证或配置文件时抛出异常。"""
        missing: list[str] = []
        if not self.google_maps_api_key:
            missing.append("GOOGLE_MAPS_API_KEY")

        llm_path = Path(self.llm_config_path)
        if not llm_path.is_absolute():
            llm_path = Path.cwd() / llm_path
        if not llm_path.exists():
            raise RuntimeError(
                f"未找到 LLM 配置文件：{self.llm_config_path}（实际解析为 {llm_path}）"
            )

        if missing:
            raise RuntimeError("缺少必要环境变量：" + ", ".join(missing))


@lru_cache(maxsize=1)
def get_configuration() -> Configuration:
    """单例配置。"""
    return Configuration()
