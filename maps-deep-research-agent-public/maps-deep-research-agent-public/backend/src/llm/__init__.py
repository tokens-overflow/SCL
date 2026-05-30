"""LLM 客户端包 —— 通过 config.yaml 的 ``llm:`` 节实现多 provider 即插即换。"""

from .base import LLMClient, LLMUsage
from .loader import build_llm_client, get_active_provider_info

__all__ = [
    "LLMClient",
    "LLMUsage",
    "build_llm_client",
    "get_active_provider_info",
]
