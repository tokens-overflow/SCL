"""Anthropic 适配层。

和 :mod:`app.llm.openai_provider` 一样，这里只做格式翻译，不含业务逻辑。

Anthropic Messages API 与 OpenAI 有两个显著差异，**正是这两个差异说明了
为什么必须有 Provider 抽象层**：

1. system 提示是**顶层参数**，不是 messages 里的一条；
2. 返回的 content 是一个 **block 数组**，不是单一字符串。

如果业务层直接对接 SDK，这两个差异就会渗透到编排代码里，
换厂商时就得改业务。归一化之后，业务层只看到 `list[LLMMessage] -> str`。
"""

from __future__ import annotations

import time
from typing import Any

from app.context.models import AgentContext
from app.core.config import Settings, get_settings
from app.core.errors import LLMProviderError
from app.llm.base import BaseLLMProvider, LLMMessage, LLMUsage


class AnthropicProvider(BaseLLMProvider):
    """Anthropic Messages API 适配器。

    Args:
        settings: 配置对象，缺省取全局配置。
        client: 可注入的 httpx.AsyncClient，便于测试。

    Raises:
        LLMProviderError: 未配置 ``ANTHROPIC_API_KEY`` 时实例化即失败。
    """

    name = "anthropic"
    #: Anthropic 要求显式声明 API 版本。写成常量便于统一升级。
    api_version = "2023-06-01"

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        super().__init__()
        self.settings = settings or get_settings()
        if not self.settings.anthropic_api_key:
            raise LLMProviderError(
                "未配置 ANTHROPIC_API_KEY。请设置环境变量，或把 LLM_PROVIDER 切回 mock。",
                details={"provider": "anthropic"},
            )
        self._client = client
        self._base_url = (
            self.settings.anthropic_base_url or "https://api.anthropic.com/v1"
        ).rstrip("/")

    async def _get_client(self) -> Any:
        if self._client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover
                raise LLMProviderError("使用 AnthropicProvider 需要安装 httpx", details={}) from exc
            self._client = httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds)
        return self._client

    @staticmethod
    def _split_system(messages: list[LLMMessage]) -> tuple[str, list[dict[str, str]]]:
        """把 system 消息抽出来，其余转成 Anthropic 的 messages 数组。

        这就是「厂商差异在 Provider 层被消化」的具体样子：
        调用方仍然按统一格式传 system 消息，转换在这里发生。
        """
        system_parts: list[str] = []
        turns: list[dict[str, str]] = []
        for msg in messages:
            if msg.role == "system":
                system_parts.append(msg.content)
            else:
                turns.append({"role": msg.role, "content": msg.content})
        # Anthropic 要求 messages 非空且首条为 user。
        if not turns:
            turns = [{"role": "user", "content": "请按系统指令输出。"}]
        elif turns[0]["role"] != "user":
            turns.insert(0, {"role": "user", "content": "请按系统指令输出。"})
        return "\n\n".join(system_parts), turns

    async def _complete(
        self,
        messages: list[LLMMessage],
        context: AgentContext,
        *,
        json_mode: bool = False,
    ) -> tuple[str, LLMUsage]:
        """调用 Messages API 并归一化返回值。"""
        client = await self._get_client()
        system_prompt, turns = self._split_system(messages)

        if json_mode:
            # Anthropic 没有 response_format 开关，用提示词约束 + 后置校验兜底。
            # 这正说明了为什么**结构校验不能省**：不同厂商的「保证」强度并不一致。
            system_prompt = (
                system_prompt
                + "\n\n只输出一个合法的 JSON 对象，不要输出任何其它文字或代码块标记。"
            )

        payload: dict[str, Any] = {
            "model": self.settings.anthropic_model,
            "max_tokens": self.settings.anthropic_max_tokens,
            "temperature": 0,
            "messages": turns,
        }
        if system_prompt:
            payload["system"] = system_prompt

        started = time.perf_counter()
        try:
            resp = await client.post(
                f"{self._base_url}/messages",
                json=payload,
                headers={
                    "x-api-key": self.settings.anthropic_api_key or "",
                    "anthropic-version": self.api_version,
                    "Content-Type": "application/json",
                },
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMProviderError(
                "调用 Anthropic 失败（网络层）",
                details={"provider": "anthropic", "error": type(exc).__name__},
                retryable=True,
            ) from exc

        if resp.status_code == 429:
            raise LLMProviderError("Anthropic 限流", details={"status": 429}, retryable=True)
        if resp.status_code >= 500:
            raise LLMProviderError(
                "Anthropic 服务端错误", details={"status": resp.status_code}, retryable=True
            )
        if resp.status_code >= 400:
            raise LLMProviderError(
                "Anthropic 请求被拒绝", details={"status": resp.status_code}, retryable=False
            )

        data = resp.json()
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        # content 是 block 数组，需要把所有 text block 拼起来——这是第二个厂商差异点。
        blocks = data.get("content") or []
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if not text:
            raise LLMProviderError("Anthropic 返回内容为空", details={"provider": "anthropic"})

        raw_usage = data.get("usage") or {}
        input_tokens = int(raw_usage.get("input_tokens", 0))
        output_tokens = int(raw_usage.get("output_tokens", 0))
        usage = LLMUsage(
            # 字段名归一化：input/output → prompt/completion。
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            model=data.get("model", self.settings.anthropic_model),
            provider=self.name,
        )
        context.extra["llm_latency_ms"] = elapsed_ms
        return text, usage

    async def aclose(self) -> None:
        """关闭底层 HTTP 客户端。"""
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
