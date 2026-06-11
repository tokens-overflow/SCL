"""根据 llm.yaml 里启用的 provider 构建对应的 LLMClient。"""
from backend.app.llm.base import LLMClient, ProviderConfig
from backend.app.llm.config import load_active_provider

# type -> 适配器，懒加载（用到哪个才 import 哪个 SDK）
_BUILDERS = {"anthropic", "openai"}


def build_client(config: ProviderConfig) -> LLMClient:
    if config.type == "anthropic":
        from backend.app.llm.anthropic_client import AnthropicClient

        return AnthropicClient(config)
    if config.type == "openai":
        from backend.app.llm.openai_client import OpenAIClient

        return OpenAIClient(config)
    raise ValueError(
        f"未知的 provider type：{config.type}（provider={config.name}）。支持：{sorted(_BUILDERS)}"
    )


def create_llm_client() -> LLMClient:
    """读取配置 + 构建当前启用 provider 的客户端。"""
    return build_client(load_active_provider())
