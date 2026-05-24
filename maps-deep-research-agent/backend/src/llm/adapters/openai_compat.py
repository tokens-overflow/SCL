"""OpenAI-compatible LLM adapter — handles both OpenAI and DeepSeek providers.

DeepSeek uses the same OpenAI SDK with a custom base_url.  For DeepSeek pro
models, reasoning/thinking parameters are injected automatically unless the
provider config explicitly sets ``reasoning_effort: ""``.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Iterable

from openai import APIError, AsyncOpenAI, RateLimitError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..base import LLMUsage
from ._utils import safe_parse_json

logger = logging.getLogger(__name__)

_DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"


class OpenAICompatAdapter:
    """Adapter for OpenAI-compatible APIs (type: openai | deepseek)."""

    def __init__(self, cfg: dict[str, Any], *, usage: LLMUsage) -> None:
        api_key: str = cfg.get("api_key", "")
        if not api_key:
            raise RuntimeError(
                f"LLM provider '{cfg.get('type', 'openai')}': api_key is required"
            )

        client_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": float(cfg.get("timeout", 60)),
        }
        base_url: str = cfg.get("base_url", "") or ""
        if base_url:
            client_kwargs["base_url"] = base_url

        self._client = AsyncOpenAI(**client_kwargs)
        self._model: str = cfg["model"]
        self._temperature: float = float(cfg.get("temperature", 0.2))
        self._provider_type: str = cfg.get("type", "openai")
        self.usage = usage

        # DeepSeek reasoning: enabled for pro models by default, disabled for flash
        if self._provider_type == "deepseek":
            default_effort = "high" if "pro" in self._model else ""
            self._reasoning_effort: str = cfg.get("reasoning_effort", default_effort)
        else:
            self._reasoning_effort = ""

    @property
    def model(self) -> str:
        return self._model

    def _reasoning_kwargs(self) -> dict[str, Any]:
        if not self._reasoning_effort:
            return {}
        return {
            "reasoning_effort": self._reasoning_effort,
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    async def _record_usage(self, response_usage: object) -> None:
        if response_usage is None:
            return
        prompt = int(getattr(response_usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(response_usage, "completion_tokens", 0) or 0)
        await self.usage.record(prompt, completion)

    async def chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception_type((RateLimitError, APIError)),
            reraise=True,
        ):
            with attempt:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=list(messages),
                    temperature=temperature if temperature is not None else self._temperature,
                    max_tokens=max_tokens,
                    **self._reasoning_kwargs(),
                )
                await self._record_usage(response.usage)
                return response.choices[0].message.content or ""
        return ""  # pragma: no cover

    async def chat_json(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> dict | list:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception_type((RateLimitError, APIError)),
            reraise=True,
        ):
            with attempt:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=list(messages),
                    temperature=temperature,
                    response_format={"type": "json_object"},
                    **self._reasoning_kwargs(),
                )
                await self._record_usage(response.usage)
                raw = response.choices[0].message.content or "{}"
                return safe_parse_json(raw)
        return {}  # pragma: no cover

    async def stream_chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        prompt_tokens = 0
        completion_tokens = 0

        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=list(messages),
            temperature=temperature if temperature is not None else self._temperature,
            stream=True,
            stream_options={"include_usage": True},
            **self._reasoning_kwargs(),
        )

        try:
            async for chunk in stream:
                if chunk.usage is not None:
                    prompt_tokens = int(chunk.usage.prompt_tokens or 0)
                    completion_tokens = int(chunk.usage.completion_tokens or 0)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None)
                if content:
                    yield content
        finally:
            await self.usage.record(prompt_tokens, completion_tokens)
