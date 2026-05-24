"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


Language = Literal["zh", "en"]


class Configuration(BaseSettings):
    """Application configuration.

    Values are loaded from environment variables (uppercase field names) and
    optionally from a ``.env`` file in the backend folder.

    LLM provider selection is handled by ``LLMConfig.yaml`` (see ``llm_config_path``).
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
        description="Path to LLMConfig.yaml (absolute or relative to cwd)",
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
        """Return a *copy* with selected fields overridden (per-request)."""
        data = self.model_dump()
        for key, value in overrides.items():
            if value is None:
                continue
            data[key] = value
        return Configuration(**data)

    def assert_ready(self) -> None:
        """Raise if mandatory credentials or config files are missing."""
        missing: list[str] = []
        if not self.google_maps_api_key:
            missing.append("GOOGLE_MAPS_API_KEY")

        llm_path = Path(self.llm_config_path)
        if not llm_path.is_absolute():
            llm_path = Path.cwd() / llm_path
        if not llm_path.exists():
            raise RuntimeError(
                f"LLM config file not found: {self.llm_config_path} "
                f"(resolved to {llm_path})"
            )

        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )


@lru_cache(maxsize=1)
def get_configuration() -> Configuration:
    """Cached singleton configuration."""
    return Configuration()
