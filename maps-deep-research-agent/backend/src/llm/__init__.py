"""LLM client package — multi-provider support via LLMConfig.yaml."""

from .base import LLMClient, LLMUsage
from .client import DeepSeekClient, DeepSeekLLMClient
from .loader import build_llm_client, get_active_provider_info

__all__ = [
    "LLMClient",
    "LLMUsage",
    "build_llm_client",
    "get_active_provider_info",
    # Deprecated aliases kept for backward compatibility
    "DeepSeekLLMClient",
    "DeepSeekClient",
]
