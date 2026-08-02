"""OpenAI 适配层。

**这个文件里不应该出现任何业务逻辑。** 它唯一的职责是：
把归一化的 :class:`LLMMessage` 翻译成 OpenAI 的请求格式，
再把 OpenAI 的响应翻译回归一化的 `(text, usage)`。

两条纪律：

1. **不写死密钥**：API Key 只从配置（环境变量）读取。
2. **SDK 是可选依赖**：没装 `openai` 包时，导入本模块不会报错，
   只有真正实例化 Provider 时才会给出清晰的提示。
   这样「没装 SDK 也能用 Mock Provider 跑通全流程」这个承诺才成立。

实现上刻意用 `httpx` 直调 HTTP 接口而不是依赖官方 SDK：
少一个依赖，也让读者能直接看到请求长什么样。需要 SDK 特性时替换本文件即可。
"""

from __future__ import annotations

import time
from typing import Any

from app.context.models import AgentContext
from app.core.config import Settings, get_settings
from app.core.errors import LLMProviderError
from app.llm.base import BaseLLMProvider, LLMMessage, LLMUsage


class OpenAIProvider(BaseLLMProvider):
    """OpenAI Chat Completions 适配器。

    Args:
        settings: 配置对象，缺省取全局配置。
        client: 可注入的 httpx.AsyncClient，便于测试时替换为 Mock Transport。

    Raises:
        LLMProviderError: 未配置 ``OPENAI_API_KEY`` 时实例化即失败——
            **早失败好过在第一次真实请求时才失败**。
    """

    name = "openai"

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        super().__init__()
        self.settings = settings or get_settings()
        if not self.settings.openai_api_key:
            raise LLMProviderError(
                "未配置 OPENAI_API_KEY。请设置环境变量，或把 LLM_PROVIDER 切回 mock。",
                details={"provider": "openai"},
            )
        self._client = client
        self._base_url = (self.settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")

    async def _get_client(self) -> Any:
        if self._client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - 依赖缺失路径
                raise LLMProviderError("使用 OpenAIProvider 需要安装 httpx", details={}) from exc
            self._client = httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds)
        return self._client

    async def _complete(
        self,
        messages: list[LLMMessage],
        context: AgentContext,
        *,
        json_mode: bool = False,
    ) -> tuple[str, LLMUsage]:
        """调用 Chat Completions 并归一化返回值。

        Returns:
            ``(文本内容, 归一化用量)``。

        Note:
            `response_format={"type": "json_object"}` 能显著降低
            「模型在 JSON 外面裹一段寒暄」的概率，但**不能替代 Pydantic 校验**——
            它保证的是「是合法 JSON」，不是「符合我们的 schema」。
        """
        client = await self._get_client()
        payload: dict[str, Any] = {
            "model": self.settings.openai_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": 0,  # 结构化输出场景一律用 0：我们要的是可复现，不是创意。
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            resp = await client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers={
                    # 密钥只在这里出现一次，且来自配置。绝不落日志。
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
            )
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一归一化
            raise LLMProviderError(
                "调用 OpenAI 失败（网络层）",
                details={"provider": "openai", "error": type(exc).__name__},
                retryable=True,
            ) from exc

        if resp.status_code == 429:
            raise LLMProviderError(
                "OpenAI 限流", details={"status": 429}, retryable=True
            )
        if resp.status_code >= 500:
            raise LLMProviderError(
                "OpenAI 服务端错误", details={"status": resp.status_code}, retryable=True
            )
        if resp.status_code >= 400:
            # 4xx 一般是参数或鉴权问题：重试一万次也是同一个结果。
            raise LLMProviderError(
                "OpenAI 请求被拒绝", details={"status": resp.status_code}, retryable=False
            )

        data = resp.json()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(
                "OpenAI 返回结构异常", details={"provider": "openai"}
            ) from exc

        raw_usage = data.get("usage") or {}
        usage = LLMUsage(
            prompt_tokens=int(raw_usage.get("prompt_tokens", 0)),
            completion_tokens=int(raw_usage.get("completion_tokens", 0)),
            total_tokens=int(raw_usage.get("total_tokens", 0)),
            model=data.get("model", self.settings.openai_model),
            provider=self.name,
        )
        context.extra["llm_latency_ms"] = elapsed_ms
        return text, usage

    async def aclose(self) -> None:
        """关闭底层 HTTP 客户端。"""
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
