"""OpenAI 兼容协议 adapter —— 同时支持 OpenAI 和 DeepSeek。

DeepSeek pro 系列模型默认自动开启 reasoning/thinking 参数；
若需关闭，可在 LLMConfig.yaml 中显式写 ``reasoning_effort: ""``。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterable

from openai import APIError, AsyncOpenAI, RateLimitError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ...config import RetryPolicy
from ..base import LLMUsage
from ._utils import safe_parse_json


def _make_retry(policy: RetryPolicy) -> AsyncRetrying:
    """按 config.app.retry.llm 构造统一的重试策略：仅对 429 / APIError 触发。"""
    return AsyncRetrying(
        stop=stop_after_attempt(policy.max_attempts),
        wait=wait_exponential(multiplier=policy.backoff_mult, max=policy.backoff_max),
        retry=retry_if_exception_type((RateLimitError, APIError)),
        reraise=True,
    )


class OpenAICompatAdapter:
    """OpenAI 兼容协议 adapter（type: openai | deepseek）。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        usage: LLMUsage,
        retry: RetryPolicy,
    ) -> None:
        self._retry_policy = retry
        api_key: str = cfg.get("api_key", "")
        if not api_key:
            raise RuntimeError(
                f"LLM provider '{cfg.get('type', 'openai')}'：必须提供 api_key"
            )

        client_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": float(cfg.get("timeout", 60)),
        }
        if cfg.get("base_url"):
            client_kwargs["base_url"] = cfg["base_url"]

        self._client = AsyncOpenAI(**client_kwargs)
        self._model: str = cfg["model"]
        self._temperature: float = float(cfg.get("temperature", 0.2))
        self._provider_type: str = cfg.get("type", "openai")
        self.usage = usage

        # DeepSeek pro 默认启用 reasoning；flash 默认关闭；yaml 可显式覆盖
        if self._provider_type == "deepseek":
            default_effort = "high" if "pro" in self._model else ""
            self._reasoning_effort: str = cfg.get("reasoning_effort", default_effort)
        else:
            self._reasoning_effort = ""

    @property
    def model(self) -> str:
        return self._model

    # ------------------------------------------------------------------
    def _reasoning_kwargs(self) -> dict[str, Any]:
        if not self._reasoning_effort:
            return {}
        return {
            "reasoning_effort": self._reasoning_effort,
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    async def _record_usage(self, usage: object) -> None:
        if usage is None:
            return
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        await self.usage.record(prompt, completion)

    def _temp(self, override: float | None) -> float:
        return self._temperature if override is None else override

    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        async for attempt in _make_retry(self._retry_policy):
            with attempt:
                resp = await self._client.chat.completions.create(
                    model=self._model,
                    messages=list(messages),
                    temperature=self._temp(temperature),
                    max_tokens=max_tokens,
                    **self._reasoning_kwargs(),
                )
                await self._record_usage(resp.usage)
                return resp.choices[0].message.content or ""
        return ""  # pragma: no cover

    async def chat_json(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> dict | list:
        async for attempt in _make_retry(self._retry_policy):
            with attempt:
                resp = await self._client.chat.completions.create(
                    model=self._model,
                    messages=list(messages),
                    temperature=temperature,
                    response_format={"type": "json_object"},
                    **self._reasoning_kwargs(),
                )
                await self._record_usage(resp.usage)
                return safe_parse_json(resp.choices[0].message.content or "{}")
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
            temperature=self._temp(temperature),
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
                content = getattr(chunk.choices[0].delta, "content", None)
                if content:
                    yield content
        finally:
            await self.usage.record(prompt_tokens, completion_tokens)
