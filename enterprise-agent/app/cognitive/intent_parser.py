"""意图解析。

认知层的第一步：把模糊的自然语言变成明确的结构化意图。

**这是模型真正擅长、也真正该做的事**——「给客户 C001 打九折，并通知客户」
这句话里，「九折」到底是折扣幅度 10% 还是折后比例 90%、
「通知」是不是一个独立动作，这些都需要语义理解，没有唯一正确答案。

而它**不该做**的事同样清楚：判断这个客服有没有资格给这个折扣。
那个问题有唯一正确答案，交给程序。
"""

from __future__ import annotations

from app.cognitive.models import IntentParseResult
from app.context.models import AgentContext
from app.core.errors import LLMOutputInvalidError
from app.llm.base import LLMMessage, LLMProvider
from app.operations.logging import get_logger
from app.security.sanitization import wrap_untrusted

logger = get_logger(__name__)


class IntentParser:
    """基于 LLM 的意图解析器。

    Args:
        provider: LLM Provider。
    """

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    async def parse(self, context: AgentContext) -> IntentParseResult:
        """解析用户意图。

        Args:
            context: 已构建好的上下文。

        Returns:
            :class:`IntentParseResult`。

        Raises:
            LLMOutputInvalidError: 模型两次都无法给出合法结构。

        Note:
            用户输入被 :func:`wrap_untrusted` 包在显式边界里。
            这**只是提示**，不是强制——模型完全可能忽略边界。
            真正阻止越权的是控制层。
        """
        messages = [
            LLMMessage(role="system", content=context.render_system_prompt()),
            LLMMessage(
                role="system",
                content=(
                    "请解析下面这段用户输入的业务意图，抽取关键实体。\n"
                    "注意：discount_rate 表示【折扣幅度】，"
                    "中文的「打九折」应解析为 0.1（即优惠 10%），而不是 0.9。\n"
                    "如果信息不足以形成明确动作，请把 clarification_needed 置为 true，"
                    "不要凭空补一个默认值。"
                ),
            ),
            LLMMessage(role="user", content=wrap_untrusted(context.user_input)),
        ]

        result = await self.provider.generate_structured(messages, IntentParseResult, context)
        logger.info(
            "intent_parsed",
            task_id=context.task_id,
            trace_id=context.trace_id,
            intent=result.intent,
            confidence=result.confidence,
            clarification_needed=result.clarification_needed,
        )
        return result


class RuleBasedIntentParser:
    """纯规则意图解析器（无 LLM）。

    存在的意义有两个：

    1. **降级路径**：模型不可用时业务不能全停。简单意图用规则就能解析。
    2. **教学点**：很多「Agent」在业务跑顺之后会发现根本不需要模型——
       规则就够了。有稳定契约的话，这种降级是无痛的：
       调用方拿到的仍然是 `IntentParseResult`，完全感知不到背后换了实现。
    """

    async def parse(self, context: AgentContext) -> IntentParseResult:
        """用正则规则解析意图。"""
        from app.llm.mock_provider import parse_customer_id, parse_discount_rate

        text = context.user_input
        customer_id = parse_customer_id(text)
        rate = parse_discount_rate(text)

        if rate is not None and customer_id:
            return IntentParseResult(
                intent="apply_discount",
                task_type="discount_request",
                entities={
                    "customer_id": customer_id,
                    "discount_rate": rate,
                    "notify": "通知" in text,
                },
                confidence=0.75,
                reasoning_summary="规则解析：命中折扣表达与客户编号。",
            )
        if customer_id:
            return IntentParseResult(
                intent="query_customer",
                task_type="customer_query",
                entities={"customer_id": customer_id},
                confidence=0.7,
                reasoning_summary="规则解析：仅命中客户编号。",
            )
        raise LLMOutputInvalidError(
            "规则解析器无法识别该输入，需要回退到 LLM 或转人工",
            details={"input_length": len(text)},
        )
