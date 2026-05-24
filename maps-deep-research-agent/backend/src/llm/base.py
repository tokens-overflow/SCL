"""Abstract LLM client interface.

All services depend on this Protocol, not on any concrete implementation.
Swap the underlying model (DeepSeek, OpenAI, Gemini …) by providing a
different class that satisfies the same three methods.
"""

from __future__ import annotations

from typing import AsyncIterator, Iterable, Protocol, runtime_checkable


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
