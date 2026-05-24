"""LLM 客户端抽象接口与 token 用量统计。

所有上层 service 仅依赖 ``LLMClient`` Protocol，不依赖任何具体实现；
更换底层模型只需提供一个满足同样三个方法的类即可。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable, Protocol, runtime_checkable


@dataclass
class LLMUsage:
    """单个客户端实例累计的 token 用量（线程安全）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    request_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def record(self, prompt: int, completion: int) -> None:
        async with self.lock:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.request_count += 1

    def snapshot(self) -> dict[str, int]:
        return {
            "llm_prompt_tokens": self.prompt_tokens,
            "llm_completion_tokens": self.completion_tokens,
            "llm_request_count": self.request_count,
        }


@runtime_checkable
class LLMClient(Protocol):
    """所有 LLM 后端必须满足的结构化协议。"""

    async def chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """单次对话补全，返回完整的助手回复文本。"""
        ...

    async def chat_json(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> dict | list:
        """JSON 输出模式，返回已解析的 Python 对象。"""
        ...

    async def stream_chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """流式补全，按 chunk 异步返回字符串。"""
        ...
