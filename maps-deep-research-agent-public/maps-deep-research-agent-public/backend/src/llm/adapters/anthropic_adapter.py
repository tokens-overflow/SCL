"""Anthropic SDK adapter —— 同时支持官方 API 与 AWS Bedrock。

按需安装：
  pip install "anthropic>=0.40.0"            # type: anthropic
  pip install "anthropic[bedrock]>=0.40.0"   # type: bedrock
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterable

from ...config import LlmDefaultsConfig
from ..base import LLMUsage
from ._utils import safe_parse_json

_JSON_INSTRUCTION = "请只用合法的 JSON 回答，不要附带任何解释或 markdown 包裹。"


def _split_messages(
    messages: Iterable[dict[str, str]],
) -> tuple[str, list[dict[str, str]]]:
    """把 system 消息从列表中拆出（Anthropic API 的 system 是单独参数）。"""
    system_parts: list[str] = []
    rest: list[dict[str, str]] = []
    for m in messages:
        if m["role"] == "system":
            system_parts.append(m["content"])
        else:
            rest.append(m)
    return "\n\n".join(system_parts), rest


class _AnthropicBase:
    """Anthropic Messages API 的共享实现（官方直连与 Bedrock 通用）。"""

    _client: Any
    _model: str
    _temperature: float
    _max_tokens: int
    usage: LLMUsage

    @property
    def model(self) -> str:
        return self._model

    # ------------------------------------------------------------------
    async def _record_usage(self, usage: Any) -> None:
        if usage is None:
            return
        prompt = int(getattr(usage, "input_tokens", 0) or 0)
        completion = int(getattr(usage, "output_tokens", 0) or 0)
        await self.usage.record(prompt, completion)

    def _build_kwargs(
        self,
        messages: Iterable[dict[str, str]],
        temperature: float | None,
        max_tokens: int | None,
        *,
        extra_system: str = "",
    ) -> dict[str, Any]:
        system, msgs = _split_messages(messages)
        if extra_system:
            system = f"{system}\n\n{extra_system}" if system else extra_system
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens or self._max_tokens,
            "messages": msgs,
            "temperature": self._temperature if temperature is None else temperature,
        }
        if system:
            kwargs["system"] = system
        return kwargs

    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        resp = await self._client.messages.create(
            **self._build_kwargs(messages, temperature, max_tokens)
        )
        await self._record_usage(resp.usage)
        return resp.content[0].text if resp.content else ""

    async def chat_json(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> dict | list:
        resp = await self._client.messages.create(
            **self._build_kwargs(
                messages, temperature, None, extra_system=_JSON_INSTRUCTION
            )
        )
        await self._record_usage(resp.usage)
        return safe_parse_json(resp.content[0].text if resp.content else "{}")

    async def stream_chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        kwargs = self._build_kwargs(messages, temperature, None)
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
                    # 流被取消时拿不到 final message，记 0 即可
                    pass
        finally:
            await self.usage.record(prompt_tokens, completion_tokens)


class AnthropicAdapter(_AnthropicBase):
    """Anthropic 官方 Messages API（type: anthropic）。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        usage: LLMUsage,
        defaults: LlmDefaultsConfig,
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "未安装 anthropic，请执行：pip install anthropic"
            ) from exc

        api_key = cfg.get("api_key", "")
        if not api_key:
            raise RuntimeError("LLM provider 'anthropic'：必须提供 api_key")

        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if cfg.get("base_url"):
            client_kwargs["base_url"] = cfg["base_url"]

        self._client = anthropic.AsyncAnthropic(**client_kwargs)
        self._model = cfg["model"]
        self._temperature = float(cfg.get("temperature", 0.2))
        self._max_tokens = int(cfg.get("max_tokens", defaults.max_tokens))
        self.usage = usage


class BedrockAdapter(_AnthropicBase):
    """AWS Bedrock 上的 Anthropic 模型（type: bedrock）。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        usage: LLMUsage,
        defaults: LlmDefaultsConfig,
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "未安装 anthropic[bedrock]，请执行：pip install 'anthropic[bedrock]'"
            ) from exc

        bedrock_kwargs: dict[str, Any] = {}
        if cfg.get("aws_access_key_id"):
            bedrock_kwargs["aws_access_key"] = cfg["aws_access_key_id"]
        if cfg.get("aws_secret_access_key"):
            bedrock_kwargs["aws_secret_key"] = cfg["aws_secret_access_key"]
        if cfg.get("region_name"):
            bedrock_kwargs["aws_region"] = cfg["region_name"]

        self._client = anthropic.AsyncAnthropicBedrock(**bedrock_kwargs)
        self._model = cfg["model"]
        self._temperature = float(cfg.get("temperature", 0.2))
        self._max_tokens = int(cfg.get("max_tokens", defaults.max_tokens))
        self.usage = usage
