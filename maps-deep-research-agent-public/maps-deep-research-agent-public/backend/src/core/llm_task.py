"""单个 LLM 用例的复用基类。

每个 :class:`LLMTask` 解决一个聚焦问题：
    接收类型化输入 → 构造 chat 消息（prompt）→ 调 LLM → 解析返回 → 给出类型化输出。

一个类把 **Prompt、调用、解析** 这三件事打包进同一个文件 / 同一处类型边界。

为什么要有这一层
----------------
没有它的话，每个 service 都会同时做：拼 prompt、选 LLM 调用形态
（chat / chat_json / stream_chat）、解析返回。所有 service 互相重复。

有了它，每个 LLM 用例只活在一个文件里，Stage 调用时就一行::

    >>> tasks = await PlanTask(llm).run(PlanInput(topic=...))

三种 LLM 调用形态各自对应一个具体基类：

* :class:`TextLLMTask`      —— 调 ``chat()``、返回纯文本
* :class:`JsonLLMTask`      —— 调 ``chat_json()``、返回 dict/list
* :class:`StreamingLLMTask` —— 调 ``stream_chat()``，把每个 chunk 回调出去
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Generic, TypeVar

from ..llm import LLMClient

TInput = TypeVar("TInput")
TOutput = TypeVar("TOutput")


class LLMTaskBase(ABC, Generic[TInput, TOutput]):
    """所有 LLM 用例的共享骨架。

    子类只需要实现 :meth:`build_messages` 和 :meth:`parse`；
    具体的 LLM 调用形态由选择 Text / Json / Streaming 子类决定。
    """

    def __init__(self, llm: LLMClient, *, temperature: float | None = None) -> None:
        self._llm = llm
        self._temperature = temperature

    # ------------------------------------------------------------------ 待子类实现
    @abstractmethod
    def build_messages(self, input: TInput) -> list[dict[str, str]]:
        """根据 input 拼出 chat 消息（system + user）。"""

    @abstractmethod
    def parse(self, raw: Any, input: TInput) -> TOutput:
        """把 LLM 原始返回转成类型化输出。

        把 ``input`` 一并传进来，因为有些用例 parse 时需要参考原始请求字段
        （例如 PlanTask 校验时会用到 ``input.max_tasks``）。
        """

    # ------------------------------------------------------------------ 内部小工具
    def _temp_kwargs(self) -> dict[str, Any]:
        """如果指定了温度就拼成 kwargs，否则空 dict —— 不传 temperature 给 LLM。"""
        return {"temperature": self._temperature} if self._temperature is not None else {}


class TextLLMTask(LLMTaskBase[TInput, TOutput]):
    """走 ``chat()`` 的用例（返回纯文本）。"""

    async def run(self, input: TInput) -> TOutput:
        raw = await self._llm.chat(self.build_messages(input), **self._temp_kwargs())
        return self.parse(raw, input)


class JsonLLMTask(LLMTaskBase[TInput, TOutput]):
    """走 ``chat_json()`` 的用例（强制 JSON 模式返回）。"""

    async def run(self, input: TInput) -> TOutput:
        raw = await self._llm.chat_json(self.build_messages(input), **self._temp_kwargs())
        return self.parse(raw, input)


class StreamingLLMTask(LLMTaskBase[TInput, TOutput]):
    """走 ``stream_chat()`` 的用例（按 chunk 流式返回）。

    调用方传入 ``on_chunk`` 回调，每来一个 chunk 就 forward 出去（例如塞进 SSE）。
    全部 chunk 拼成的完整字符串再交给 :meth:`parse`。
    """

    async def stream(
        self,
        input: TInput,
        on_chunk: Callable[[str], None] | None = None,
    ) -> TOutput:
        chunks: list[str] = []
        async for chunk in self._llm.stream_chat(
            self.build_messages(input), **self._temp_kwargs()
        ):
            if not chunk:
                continue
            chunks.append(chunk)
            if on_chunk is not None:
                on_chunk(chunk)
        return self.parse("".join(chunks), input)
