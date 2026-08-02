"""LLM Provider 工厂。

这个文件是「换厂商只改一个环境变量」这个承诺的兑现点。

**重要的降级策略**：如果配置指定了 openai / anthropic 但没有对应的 API Key，
我们**不静默降级到 Mock**。原因：生产环境里静默降级是灾难——
你以为在用真模型，实际上在跑一套规则，而且没有任何告警。
正确做法是明确报错，让人去修配置。

只有一个例外：`allow_fallback=True` 时（测试或本地演示）才允许回退，
并且会打一条明确的告警日志。
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.errors import LLMProviderError
from app.llm.base import LLMProvider
from app.llm.mock_provider import MockLLMProvider
from app.operations.logging import get_logger

logger = get_logger(__name__)


def create_llm_provider(
    settings: Settings | None = None,
    *,
    allow_fallback: bool = False,
) -> LLMProvider:
    """按配置创建 LLM Provider。

    Args:
        settings: 配置对象，缺省取全局配置。
        allow_fallback: 缺少密钥时是否允许回退到 Mock。
            **生产环境必须为 False。**

    Returns:
        实现了 :class:`~app.llm.base.LLMProvider` 协议的实例。

    Raises:
        LLMProviderError: 配置了真实 Provider 但缺少必要密钥，且不允许回退。
    """
    settings = settings or get_settings()
    provider_name = settings.llm_provider

    if provider_name == "mock":
        logger.info("llm_provider_selected", provider="mock")
        return MockLLMProvider()

    try:
        if provider_name == "openai":
            from app.llm.openai_provider import OpenAIProvider

            logger.info("llm_provider_selected", provider="openai", model=settings.openai_model)
            return OpenAIProvider(settings)

        if provider_name == "anthropic":
            from app.llm.anthropic_provider import AnthropicProvider

            logger.info(
                "llm_provider_selected", provider="anthropic", model=settings.anthropic_model
            )
            return AnthropicProvider(settings)
    except LLMProviderError:
        if not allow_fallback:
            raise
        logger.warning(
            "llm_provider_fallback_to_mock",
            requested=provider_name,
            reason="missing_credentials",
        )
        return MockLLMProvider()

    raise LLMProviderError(
        f"未知的 LLM Provider: {provider_name}",
        details={"provider": provider_name, "supported": ["mock", "openai", "anthropic"]},
    )
