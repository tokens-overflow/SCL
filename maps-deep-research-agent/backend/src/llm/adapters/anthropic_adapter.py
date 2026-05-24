"""Anthropic SDK adapter — handles both direct Anthropic API and AWS Bedrock.

Install extras before use:
  pip install "anthropic>=0.40.0"           # type: anthropic
  pip install "anthropic[bedrock]>=0.40.0"  # type: bedrock
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Iterable

from ..base import LLMUsage
from ._utils import safe_parse_json

logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS = 8192


def _split_messages(
    messages: Iterable[dict[str, str]],
) -> tuple[str, list[dict[str, str]]]:
    """Extract system prompt; return (system_str, non_system_messages)."""
    system_parts: list[str] = []
    human_msgs: list[dict[str, str]] = []
    for m in messages:
        if m["role"] == "system":
            system_parts.append(m["content"])
        else:
            human_msgs.append(m)
    return "\n\n".join(system_parts), human_msgs


class _AnthropicBase:
    """Shared implementation for Anthropic Messages API (direct + Bedrock)."""

    _client: Any
    _model: str
    _temperature: float
    _max_tokens: int
    usage: LLMUsage

    @property
    def model(self) -> str:
        return self._model

    async def _record_usage(self, response_usage: Any) -> None:
        if response_usage is None:
            return
        prompt = int(getattr(response_usage, "input_tokens", 0) or 0)
        completion = int(getattr(response_usage, "output_tokens", 0) or 0)
        await self.usage.record(prompt, completion)

    async def chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        system, msgs = _split_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens or self._max_tokens,
            "messages": msgs,
            "temperature": temperature if temperature is not None else self._temperature,
        }
        if system:
            kwargs["system"] = system
        response = await self._client.messages.create(**kwargs)
        await self._record_usage(response.usage)
        return response.content[0].text if response.content else ""

    async def chat_json(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> dict | list:
        system, msgs = _split_messages(messages)
        json_instruction = "Respond only with valid JSON. No explanation or markdown."
        combined_system = f"{system}\n\n{json_instruction}" if system else json_instruction
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": msgs,
            "temperature": temperature,
            "system": combined_system,
        }
        response = await self._client.messages.create(**kwargs)
        await self._record_usage(response.usage)
        raw = response.content[0].text if response.content else "{}"
        return safe_parse_json(raw)

    async def stream_chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        system, msgs = _split_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": msgs,
            "temperature": temperature if temperature is not None else self._temperature,
        }
        if system:
            kwargs["system"] = system

        prompt_tokens = 0
        completion_tokens = 0

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
                try:
                    final = stream.get_final_message()
                    if final and final.usage:
                        prompt_tokens = int(final.usage.input_tokens or 0)
                        completion_tokens = int(final.usage.output_tokens or 0)
                except Exception:
                    pass
        finally:
            await self.usage.record(prompt_tokens, completion_tokens)


class AnthropicAdapter(_AnthropicBase):
    """Adapter for the direct Anthropic Messages API (type: anthropic)."""

    def __init__(self, cfg: dict[str, Any], *, usage: LLMUsage) -> None:
        try:
            import anthropic as _anthropic
        except ImportError as exc:
            raise ImportError(
                "Install the 'anthropic' package: pip install anthropic"
            ) from exc

        api_key: str = cfg.get("api_key", "")
        if not api_key:
            raise RuntimeError("LLM provider 'anthropic': api_key is required")

        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if cfg.get("base_url"):
            client_kwargs["base_url"] = cfg["base_url"]

        self._client = _anthropic.AsyncAnthropic(**client_kwargs)
        self._model = cfg["model"]
        self._temperature = float(cfg.get("temperature", 0.2))
        self._max_tokens = int(cfg.get("max_tokens", _DEFAULT_MAX_TOKENS))
        self.usage = usage


class BedrockAdapter(_AnthropicBase):
    """Adapter for Anthropic models via AWS Bedrock (type: bedrock)."""

    def __init__(self, cfg: dict[str, Any], *, usage: LLMUsage) -> None:
        try:
            import anthropic as _anthropic
        except ImportError as exc:
            raise ImportError(
                "Install the 'anthropic[bedrock]' package: pip install 'anthropic[bedrock]'"
            ) from exc

        bedrock_kwargs: dict[str, Any] = {}
        if cfg.get("aws_access_key_id"):
            bedrock_kwargs["aws_access_key"] = cfg["aws_access_key_id"]
        if cfg.get("aws_secret_access_key"):
            bedrock_kwargs["aws_secret_key"] = cfg["aws_secret_access_key"]
        if cfg.get("region_name"):
            bedrock_kwargs["aws_region"] = cfg["region_name"]

        self._client = _anthropic.AsyncAnthropicBedrock(**bedrock_kwargs)
        self._model = cfg["model"]
        self._temperature = float(cfg.get("temperature", 0.2))
        self._max_tokens = int(cfg.get("max_tokens", _DEFAULT_MAX_TOKENS))
        self.usage = usage
