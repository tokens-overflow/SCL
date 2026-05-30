"""LLM Provider 工厂 —— 根据 ``Configuration.llm`` 节实例化 adapter。

设计要点：
- 由 ``config.py`` 统一加载 ``config.yaml``、展开 ``${VAR}`` 占位符
- 本模块只负责按 ``active`` provider 选择 adapter 并注入运行时所需的 retry / llm_defaults
"""

from __future__ import annotations

from typing import Any

from ..config import Configuration, LlmSection
from .base import LLMClient, LLMUsage


def build_llm_client(
    cfg: Configuration,
    *,
    usage: LLMUsage | None = None,
) -> LLMClient:
    """根据 ``cfg.llm.active`` 实例化对应的 adapter。

    支持的 provider type：
      - ``openai``    OpenAI 官方 API
      - ``deepseek``  DeepSeek（OpenAI 兼容协议）
      - ``anthropic`` Anthropic Messages API （需 ``pip install anthropic``）
      - ``bedrock``   AWS Bedrock 上的 Anthropic（需 ``pip install 'anthropic[bedrock]'``）
    """
    provider_cfg = cfg.llm.active_provider()
    provider_type = provider_cfg.get("type", cfg.llm.active)
    _usage = usage or LLMUsage()

    if provider_type in ("openai", "deepseek"):
        from .adapters.openai_compat import OpenAICompatAdapter
        return OpenAICompatAdapter(
            provider_cfg,
            usage=_usage,
            retry=cfg.app.retry.llm,
        )

    if provider_type == "anthropic":
        from .adapters.anthropic_adapter import AnthropicAdapter
        return AnthropicAdapter(
            provider_cfg,
            usage=_usage,
            defaults=cfg.app.llm_defaults,
        )

    if provider_type == "bedrock":
        from .adapters.anthropic_adapter import BedrockAdapter
        return BedrockAdapter(
            provider_cfg,
            usage=_usage,
            defaults=cfg.app.llm_defaults,
        )

    raise RuntimeError(
        f"config.yaml: 未知 provider type '{provider_type}'，"
        "可选值为 openai / deepseek / anthropic / bedrock"
    )


def get_active_provider_info(llm: LlmSection) -> dict[str, str]:
    """返回当前激活 provider 的简要信息 ``{active, type, model}``。"""
    provider_cfg: dict[str, Any] = llm.providers.get(llm.active, {})
    return {
        "active": llm.active,
        "type": provider_cfg.get("type", llm.active),
        "model": provider_cfg.get("model", ""),
    }
