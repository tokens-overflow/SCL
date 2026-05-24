"""LLM Provider 工厂 —— 加载 LLMConfig.yaml 并实例化当前激活的 adapter。

YAML 中形如 ``${API_KEY}`` 的占位符在加载阶段自动从 ``os.environ`` 展开；
缺失的变量替换为空串，由具体 adapter 在初始化时报清晰的错误。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .base import LLMClient, LLMUsage

# 匹配 ${VAR} 形式的环境变量占位符
_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand_env(value: str) -> str:
    """将字符串中所有 ${VAR} 替换为对应的环境变量值。"""
    return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _resolve(obj: Any) -> Any:
    """递归展开 dict / list / str 中的环境变量。"""
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {k: _resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve(v) for v in obj]
    return obj


def load_llm_config(path: str | Path) -> dict[str, Any]:
    """加载并解析 LLMConfig.yaml（自动展开 ${ENV_VAR} 占位符）。"""
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    with p.open(encoding="utf-8") as f:
        return _resolve(yaml.safe_load(f))


def build_llm_client(
    yaml_path: str | Path = "LLMConfig.yaml",
    *,
    usage: LLMUsage | None = None,
) -> LLMClient:
    """根据 LLMConfig.yaml 中 ``active`` 字段实例化对应的 adapter。

    支持的 provider type：
      - ``openai``    OpenAI 官方 API
      - ``deepseek``  DeepSeek（OpenAI 兼容协议）
      - ``anthropic`` Anthropic Messages API （需 ``pip install anthropic``）
      - ``bedrock``   AWS Bedrock 上的 Anthropic（需 ``pip install 'anthropic[bedrock]'``）
    """
    cfg = load_llm_config(yaml_path)
    active: str = cfg["active"]
    providers: dict[str, Any] = cfg.get("providers", {})

    if active not in providers:
        raise RuntimeError(
            f"LLMConfig.yaml：active provider '{active}' 未在 providers 节中定义"
        )

    provider_cfg = providers[active]
    provider_type = provider_cfg.get("type", active)
    _usage = usage or LLMUsage()

    if provider_type in ("openai", "deepseek"):
        from .adapters.openai_compat import OpenAICompatAdapter
        return OpenAICompatAdapter(provider_cfg, usage=_usage)

    if provider_type == "anthropic":
        from .adapters.anthropic_adapter import AnthropicAdapter
        return AnthropicAdapter(provider_cfg, usage=_usage)

    if provider_type == "bedrock":
        from .adapters.anthropic_adapter import BedrockAdapter
        return BedrockAdapter(provider_cfg, usage=_usage)

    raise RuntimeError(
        f"LLMConfig.yaml：未知 provider type '{provider_type}'，"
        "可选值为 openai / deepseek / anthropic / bedrock"
    )


def get_active_provider_info(yaml_path: str | Path = "LLMConfig.yaml") -> dict[str, str]:
    """返回当前激活 provider 的简要信息 ``{active, type, model}``。"""
    cfg = load_llm_config(yaml_path)
    active = cfg["active"]
    provider_cfg = cfg.get("providers", {}).get(active, {})
    return {
        "active": active,
        "type": provider_cfg.get("type", active),
        "model": provider_cfg.get("model", ""),
    }
