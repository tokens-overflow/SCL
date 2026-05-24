"""Thin async client around the DeepSeek OpenAI-compatible API.

We use the official ``openai`` SDK pointed at DeepSeek's base URL. Compared to
chapter 14 (which goes through ``hello_agents``), this gives us:

* direct access to JSON mode (``response_format``) for the planner;
* native streaming with explicit token accounting;
* an injectable usage tracker so the orchestrator can report per-run cost.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable

from openai import APIError, AsyncOpenAI, RateLimitError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import Configuration

logger = logging.getLogger(__name__)


@dataclass
class LLMUsage:
    """Aggregate token usage across all calls made by a client instance."""

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


class DeepSeekClient:
    """High-level wrapper exposing chat / JSON / streaming completions."""

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

    # ------------------------------------------------------------------
    @property
    def model(self) -> str:
        return self._config.deepseek_model

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
        """One-shot chat completion. Returns the assistant message content."""
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
        """Chat with ``response_format=json_object`` and parse the result.

        DeepSeek's JSON mode requires that the prompt mention "json" - our
        planner prompt does. If parsing fails we fall back to a best-effort
        extraction so the whole pipeline doesn't crash on a single bad reply.
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


def _safe_parse_json(raw: str) -> dict | list:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("LLM JSON parse failed, attempting recovery; raw=%s", raw[:200])

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            pass

    start = raw.find("[")
    end = raw.rfind("]")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            pass

    return {}
