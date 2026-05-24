"""Abstract LLM client interface and shared usage tracker.

All services depend on LLMClient protocol, not on any concrete implementation.
Swap the underlying model (DeepSeek, OpenAI, Anthropic, Bedrock …) by providing a
different class that satisfies the same three methods.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable, Protocol, runtime_checkable


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


@runtime_checkable
class LLMClient(Protocol):
    """Structural protocol that every LLM backend must satisfy."""

    async def chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """One-shot completion — returns the full assistant reply."""
        ...

    async def chat_json(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> dict | list:
        """Completion with JSON output — returns parsed Python object."""
        ...

    async def stream_chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """Stream completion chunks as an async iterator of strings."""
        ...
