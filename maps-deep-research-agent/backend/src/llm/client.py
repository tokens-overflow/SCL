"""DeepSeek LLM client — OpenAI-compatible API with optional thinking/reasoning.

Supports two model tiers via config:
  - deepseek-v4-pro  : passes reasoning_effort + extra_body thinking params
  - deepseek-v4-flash: standard completion, no reasoning params

Usage tracker (LLMUsage) is injectable so the orchestrator can report
per-run token cost without coupling services to the agent layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable

from openai import APIError, AsyncOpenAI, RateLimitError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import Configuration
from .base import LLMClient  # noqa: F401 — re-exported for convenience

logger = logging.getLogger(__name__)


@dataclass
class LLMUsage:
    """Aggregate token usage across all calls made by one client instance."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    request_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def record(self, prompt: int, completion: int) -> None:
        async with self.lock:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.request_count += 1

    def snapshot(self) -> dict[str, int]:
        return {
            "llm_prompt_tokens": self.prompt_tokens,
            "llm_completion_tokens": self.completion_tokens,
            "llm_request_count": self.request_count,
        }


class DeepSeekLLMClient:
    """Concrete LLM backend wired to the DeepSeek API.

    Satisfies the LLMClient protocol. Passes thinking/reasoning parameters
    automatically when the model and config support it.
    """

    def __init__(self, config: Configuration, usage: LLMUsage | None = None) -> None:
        if not config.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")

        self._config = config
        self._client = AsyncOpenAI(
            api_key=config.deepseek_api_key,
            base_url=config.deepseek_base_url,
            timeout=config.deepseek_timeout,
        )
        self.usage = usage or LLMUsage()

    @property
    def model(self) -> str:
        return self._config.deepseek_model

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reasoning_kwargs(self) -> dict[str, Any]:
        """Extra kwargs for reasoning/thinking mode (pro model only)."""
        if not self._config.reasoning_enabled:
            return {}
        return {
            "reasoning_effort": self._config.deepseek_reasoning_effort,
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    async def _record_usage(self, response_usage: object) -> None:
        if response_usage is None:
            return
        prompt = int(getattr(response_usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(response_usage, "completion_tokens", 0) or 0)
        await self.usage.record(prompt, completion)

    # ------------------------------------------------------------------
    # Public interface (satisfies LLMClient protocol)
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """One-shot chat completion."""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception_type((RateLimitError, APIError)),
            reraise=True,
        ):
            with attempt:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    messages=list(messages),
                    temperature=temperature
                    if temperature is not None
                    else self._config.deepseek_temperature,
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
        """Chat with JSON output mode — returns parsed Python object.

        DeepSeek JSON mode requires the prompt to mention "json".
        Falls back to best-effort extraction on parse failure so the
        pipeline doesn't crash on a single bad reply.
        """
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception_type((RateLimitError, APIError)),
            reraise=True,
        ):
            with attempt:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    messages=list(messages),
                    temperature=temperature,
                    response_format={"type": "json_object"},
                    **self._reasoning_kwargs(),
                )
                await self._record_usage(response.usage)
                raw = response.choices[0].message.content or "{}"
                return _safe_parse_json(raw)
        return {}  # pragma: no cover

    async def stream_chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """Stream incremental content chunks."""
        prompt_tokens = 0
        completion_tokens = 0

        stream = await self._client.chat.completions.create(
            model=self.model,
            messages=list(messages),
            temperature=temperature
            if temperature is not None
            else self._config.deepseek_temperature,
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


# Backward-compat alias — existing code that imports DeepSeekClient still works.
DeepSeekClient = DeepSeekLLMClient


# ---------------------------------------------------------------------------
def _safe_parse_json(raw: str) -> dict | list:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("LLM JSON parse failed, attempting recovery; raw=%s", raw[:200])

    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = raw.find(start_char)
        end = raw.rfind(end_char)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                continue

    return {}
