"""Backward-compatibility shim.

New code should use ``build_llm_client()`` from ``src.llm.loader``.
``LLMUsage`` is now defined in ``base.py`` and re-exported here so existing
imports continue to work without changes.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterable

from .base import LLMUsage  # noqa: F401 — public re-export
from .adapters.openai_compat import OpenAICompatAdapter


class DeepSeekLLMClient:
    """Deprecated: use build_llm_client() instead."""

    def __init__(self, config: Any, usage: LLMUsage | None = None) -> None:
        provider_cfg: dict[str, Any] = {
            "type": "deepseek",
            "model": getattr(config, "deepseek_model", "deepseek-v4-pro"),
            "api_key": getattr(config, "deepseek_api_key", ""),
            "base_url": getattr(config, "deepseek_base_url", "https://api.deepseek.com"),
            "temperature": getattr(config, "deepseek_temperature", 0.2),
            "timeout": getattr(config, "deepseek_timeout", 60),
        }
        if getattr(config, "reasoning_enabled", False):
            provider_cfg["reasoning_effort"] = getattr(
                config, "deepseek_reasoning_effort", "high"
            )
        self._adapter = OpenAICompatAdapter(provider_cfg, usage=usage or LLMUsage())
        self.usage = self._adapter.usage

    @property
    def model(self) -> str:
        return self._adapter.model

    async def chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return await self._adapter.chat(messages, temperature=temperature, max_tokens=max_tokens)

    async def chat_json(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> dict | list:
        return await self._adapter.chat_json(messages, temperature=temperature)

    async def stream_chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        async for chunk in self._adapter.stream_chat(messages, temperature=temperature):
            yield chunk


DeepSeekClient = DeepSeekLLMClient
