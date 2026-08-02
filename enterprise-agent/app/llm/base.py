"""LLM Provider 抽象层。

**这一层存在的唯一目的：让业务层永远不知道自己在用哪家模型。**

如果业务代码里出现了 `response.choices[0].message.tool_calls[0].function.arguments`，
那就等于把 OpenAI 的数据结构焊死在了业务逻辑里。换 Anthropic、换国产模型、
甚至换成一段规则代码，都要动业务。所以：

* 入参统一为 :class:`LLMMessage`；
* 出参统一为**调用方指定的 Pydantic 模型**；
* 各家 SDK 的差异全部在 Provider 内部消化。

另一条重要设计：`generate_structured` 返回的是**已经通过 Pydantic 校验**的对象。
但请注意——

    结构化输出只是必要条件，不是充分条件。

Pydantic 能保证 `discount_rate` 是 0~1 的浮点数，保证不了「这个客服有没有资格
给这么大的折扣」。格式正确的越权请求依然是越权请求。所以结构校验之后，
**必须**再过控制层。这两件事经常被混为一谈，是很多 Demo 变成事故的起点。
"""

from __future__ import annotations

import abc
import json
import re
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from app.context.models import AgentContext
from app.core.errors import LLMOutputInvalidError

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


class LLMMessage(BaseModel):
    """归一化的对话消息。

    刻意只保留 role + content 两个字段：这是所有主流厂商的最小公共子集。
    厂商特有的字段（如 OpenAI 的 `name`、Anthropic 的 content block 数组）
    在各自的 Provider 里做转换，不污染这个契约。
    """

    role: Literal["system", "user", "assistant"]
    content: str


class LLMUsage(BaseModel):
    """token 用量与成本。

    为什么必须归一化这个字段：**成本要能按任务归因**。
    「这次编排到底花了多少钱」是运营层的基本问题，
    如果每家厂商的用量字段名都不一样，成本统计就写不出来。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    provider: str = ""

    def merge(self, other: LLMUsage) -> LLMUsage:
        """累加两次调用的用量。"""
        return LLMUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            model=other.model or self.model,
            provider=other.provider or self.provider,
        )


class LLMCallRecord(BaseModel):
    """一次 LLM 调用的可审计记录。

    包含 Prompt 快照、耗时、用量和是否重试过。
    **不包含**模型的私有思维链——我们只保存简洁的决策说明（reasoning_summary），
    这既是合规要求，也是因为长篇思维链的审计价值远低于它带来的存储和泄漏风险。
    """

    provider: str
    model: str
    latency_ms: int = 0
    usage: LLMUsage = Field(default_factory=LLMUsage)
    attempts: int = 1
    prompt_snapshot: dict[str, Any] = Field(default_factory=dict)
    response_model: str = ""


class LLMProvider(Protocol):
    """LLM Provider 协议。

    所有实现必须满足：**输入 messages，输出一个通过校验的 Pydantic 对象**。
    """

    name: str

    async def generate_structured(
        self,
        messages: list[LLMMessage],
        response_model: type[ResponseModelT],
        context: AgentContext,
    ) -> ResponseModelT:
        """生成结构化输出。

        Args:
            messages: 归一化消息列表。
            response_model: 期望的 Pydantic 输出模型。
            context: 当前 Agent 上下文，供 Provider 做审计与降级判断。

        Returns:
            通过校验的 `response_model` 实例。

        Raises:
            LLMOutputInvalidError: 模型输出无法解析为目标结构（重试后仍失败）。
            LLMProviderError: 供应商调用失败。
        """
        ...

    async def generate_text(
        self,
        messages: list[LLMMessage],
        context: AgentContext,
    ) -> str:
        """生成自由文本（用于把结果写成给人看的话）。"""
        ...

    def last_call_record(self) -> LLMCallRecord | None:
        """返回最近一次调用的审计记录。"""
        ...


class BaseLLMProvider(abc.ABC):
    """Provider 基类，收敛各家实现的公共逻辑。

    公共逻辑包括：
    * JSON 提取与 Pydantic 校验（含一次「带错误信息重试」）；
    * 调用记录的维护；
    * schema 提示词的生成。

    子类只需要实现 `_complete()`——把归一化消息发出去、把文本拿回来。
    """

    name: str = "base"

    def __init__(self) -> None:
        self._last_record: LLMCallRecord | None = None

    # ------------------------------------------------------------------ 子类实现
    @abc.abstractmethod
    async def _complete(
        self,
        messages: list[LLMMessage],
        context: AgentContext,
        *,
        json_mode: bool = False,
    ) -> tuple[str, LLMUsage]:
        """向厂商发起一次补全调用。

        Args:
            messages: 归一化消息。
            context: Agent 上下文。
            json_mode: 是否要求返回 JSON。支持原生 JSON mode 的厂商应该打开它——
                这能显著降低「模型在 JSON 外面裹了一段寒暄」的概率。

        Returns:
            ``(原始文本, 用量)``。
        """

    # ------------------------------------------------------------------ 公共实现
    async def generate_structured(
        self,
        messages: list[LLMMessage],
        response_model: type[ResponseModelT],
        context: AgentContext,
    ) -> ResponseModelT:
        """生成并校验结构化输出。

        实现里有一个重要细节：**第一次校验失败时，把校验错误原文回灌给模型再试一次。**
        这比「换个 Prompt 重来」有效得多，因为模型拿到的是精确的失败原因。
        但只重试一次——如果模型两次都给不出合法结构，说明是 Prompt 或 schema 设计问题，
        再试下去只是烧钱。
        """
        schema_hint = _build_schema_hint(response_model)
        working = list(messages) + [LLMMessage(role="system", content=schema_hint)]

        last_error: Exception | None = None
        for attempt in range(1, 3):
            raw, usage = await self._complete(working, context, json_mode=True)
            self._record(context, usage, attempt, response_model)
            try:
                payload = _extract_json(raw)
                return response_model.model_validate(payload)
            except (PydanticValidationError, ValueError) as exc:
                last_error = exc
                # 把失败原因作为新的一轮输入。注意这里用 assistant + system 两条消息，
                # 让模型清楚看到「你上次给的是这个，它错在哪」。
                working = working + [
                    LLMMessage(role="assistant", content=raw[:2000]),
                    LLMMessage(
                        role="system",
                        content=(
                            "上一次输出不符合要求，错误如下，请只输出修正后的 JSON，"
                            f"不要包含任何解释文字：\n{exc}"
                        ),
                    ),
                ]

        raise LLMOutputInvalidError(
            f"模型连续 2 次未能输出合法的 {response_model.__name__} 结构",
            details={"response_model": response_model.__name__, "last_error": str(last_error)},
        )

    async def generate_text(
        self,
        messages: list[LLMMessage],
        context: AgentContext,
    ) -> str:
        """生成自由文本。"""
        raw, usage = await self._complete(messages, context, json_mode=False)
        self._record(context, usage, 1, None)
        return raw.strip()

    def last_call_record(self) -> LLMCallRecord | None:
        """返回最近一次调用的审计记录。"""
        return self._last_record

    def _record(
        self,
        context: AgentContext,
        usage: LLMUsage,
        attempts: int,
        response_model: type[BaseModel] | None,
    ) -> None:
        self._last_record = LLMCallRecord(
            provider=self.name,
            model=usage.model,
            usage=usage,
            attempts=attempts,
            prompt_snapshot=context.prompt_snapshot(),
            response_model=response_model.__name__ if response_model else "text",
        )


def _build_schema_hint(response_model: type[BaseModel]) -> str:
    """根据 Pydantic 模型生成 schema 提示词。

    直接把 JSON Schema 塞给模型，比人手写「请返回一个包含 xxx 字段的 JSON」
    可靠得多，而且 schema 变了提示词自动跟着变，不会漏改。
    """
    schema = response_model.model_json_schema()
    return (
        "你必须只输出一个 JSON 对象，不要输出 Markdown 代码块标记，"
        "不要输出任何解释文字。JSON 必须符合以下 JSON Schema：\n"
        + json.dumps(schema, ensure_ascii=False, indent=2)
    )


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _extract_json(raw: str) -> Any:
    """从模型输出里稳健地提取 JSON。

    模型经常做三件让人头疼的事：裹上 ```json 代码块、在 JSON 前后加寒暄、
    输出多个 JSON。这个函数按「代码块 → 整体解析 → 首个平衡括号块」的顺序尝试。

    Raises:
        ValueError: 完全找不到可解析的 JSON。此时应该走「回灌错误重试」路径，
            而**不是**返回一个猜出来的默认值——把明显的错误变成隐蔽的错误，
            是这一层最容易犯的设计错误。
    """
    text = raw.strip()

    block = _JSON_BLOCK.search(text)
    if block:
        text = block.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 扫描第一个括号平衡的 JSON 对象。
    start = text.find("{")
    if start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : idx + 1]
                    return json.loads(candidate)

    raise ValueError("模型输出中未找到可解析的 JSON 对象")
