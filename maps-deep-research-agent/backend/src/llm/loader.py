"""LLM provider factory — loads LLMConfig.yaml and instantiates the active adapter.

Environment variable substitution: values like ``${DEEPSEEK_API_KEY}`` in the
YAML file are expanded at load time from ``os.environ``.  Missing variables
expand to an empty string (the adapter will raise a clear error if required).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .base import LLMClient, LLMUsage


# ---------------------------------------------------------------------------
# YAML loading + env-var expansion
# ---------------------------------------------------------------------------

def _expand_env(value: str) -> str:
    return re.sub(
        r"\$\{([^}]+)\}",
        lambda m: os.environ.get(m.group(1), ""),
        value,
    )


def _resolve(obj: Any) -> Any:
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {k: _resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve(item) for item in obj]
    return obj


def load_llm_config(path: str | Path) -> dict[str, Any]:
    """Parse LLMConfig.yaml and expand ``${ENV_VAR}`` placeholders."""
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    with p.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return _resolve(raw)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_llm_client(
    yaml_path: str | Path = "LLMConfig.yaml",
    *,
    usage: LLMUsage | None = None,
) -> LLMClient:
    """Instantiate the active LLM adapter declared in LLMConfig.yaml.

    Supported types:
      - ``openai``    — OpenAI API
      - ``deepseek``  — DeepSeek API (OpenAI-compatible)
      - ``anthropic`` — Anthropic Messages API  (requires: pip install anthropic)
      - ``bedrock``   — Anthropic via AWS Bedrock (requires: pip install anthropic[bedrock])
    """
    cfg = load_llm_config(yaml_path)
    active: str = cfg["active"]
    providers: dict[str, Any] = cfg.get("providers", {})

    if active not in providers:
        raise RuntimeError(
            f"LLMConfig.yaml: active provider '{active}' not found in providers section"
        )

    provider_cfg: dict[str, Any] = providers[active]
    provider_type: str = provider_cfg.get("type", active)
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
        f"LLMConfig.yaml: unknown provider type '{provider_type}' "
        f"(supported: openai, deepseek, anthropic, bedrock)"
    )


def get_active_provider_info(yaml_path: str | Path = "LLMConfig.yaml") -> dict[str, str]:
    """Return ``{active, type, model}`` for the currently selected provider."""
    cfg = load_llm_config(yaml_path)
    active: str = cfg["active"]
    provider_cfg: dict[str, Any] = cfg.get("providers", {}).get(active, {})
    return {
        "active": active,
        "type": provider_cfg.get("type", active),
        "model": provider_cfg.get("model", ""),
    }
