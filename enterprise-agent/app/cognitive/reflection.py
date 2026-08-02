"""反思与最终回复生成。

两个功能：

1. **Reflection**：执行完成后让模型检查「结果是否达到预期」。
   这拦的是「一本正经的胡说」——结构合法、规则也过了，但结论本身不合理。

2. **最终回复**：把执行结果写成人话。

**关于 Reflection 的一个重要限制**：

它是**质量手段，不是安全手段**。它由同一个（或另一个）模型执行，
同样不可靠。绝不能因为「Reflection 说没问题」就跳过控制层校验，
也不能让 Reflection 有权触发补偿或撤销——
「是否撤销一笔已生效的业务」必须由明确规则或人来决定。

**关于最终回复的一个重要约束**：

金额、折扣率、单据号这类**事实性数字由程序套模板填入**，
不让模型自由生成。模型负责组织语言，不负责陈述事实。
"""

from __future__ import annotations

from typing import Any

from app.cognitive.models import FinalReply, ReflectionResult
from app.context.models import AgentContext
from app.core.errors import AgentError
from app.llm.base import LLMMessage, LLMProvider
from app.operations.logging import get_logger

logger = get_logger(__name__)


class ReflectionEngine:
    """执行结果自检。

    Args:
        provider: LLM Provider。
    """

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    async def reflect(self, context: AgentContext, facts: dict[str, Any]) -> ReflectionResult:
        """对执行结果做一次自检。

        Args:
            context: 上下文（含最近步骤结果）。
            facts: **从业务系统查出来的事实**，不是任务自己记录的输出。
                用任务自己的输出做自检，检的只是「我们有没有正确记录」。

        Returns:
            :class:`ReflectionResult`。模型不可用时返回一个保守的
            「继续」结论——**Reflection 失败不应该让已完成的业务倒退**。
        """
        messages = [
            LLMMessage(role="system", content=context.render_system_prompt()),
            LLMMessage(
                role="system",
                content=(
                    "请检查以下执行结果是否达到了用户的原始诉求。\n"
                    f"业务事实：{facts}\n"
                    "只判断结果是否合理，不要建议执行任何新动作。"
                    "你没有权限触发撤销或补偿。"
                ),
            ),
            LLMMessage(role="user", content=context.user_input),
        ]
        try:
            return await self.provider.generate_structured(messages, ReflectionResult, context)
        except AgentError as exc:
            # Reflection 是锦上添花。它失败了不能影响已经完成的业务。
            logger.warning(
                "reflection_failed",
                task_id=context.task_id,
                trace_id=context.trace_id,
                error=exc.message,
            )
            return ReflectionResult(
                acceptable=True,
                suggested_action="proceed",
                reasoning_summary="反思模块不可用，按已落库的执行结果为准。",
            )


class ReplyComposer:
    """最终回复生成器。

    Args:
        provider: LLM Provider。
    """

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    async def compose(
        self,
        context: AgentContext,
        *,
        outcome: str,
        facts: dict[str, Any],
    ) -> str:
        """生成给用户的最终回复。

        Args:
            context: 上下文。
            outcome: 任务结局（COMPLETED / PARTIAL_SUCCESS / FAILED …）。
            facts: 事实字典。**模板里的数字全部来自这里**，
                模型只负责把它们组织成通顺的句子。

        Returns:
            回复文本。模型不可用时退化为程序生成的模板文本——
            **回复生成失败不能让整个任务失败**，
            用户宁可看到一句朴素的模板话，也不要看到 500。
        """
        # 事实性内容由程序生成，作为「必须包含的要点」交给模型。
        factual_lines = _render_facts(outcome, facts)

        messages = [
            LLMMessage(role="system", content=context.render_system_prompt()),
            LLMMessage(
                role="system",
                content=(
                    "请把以下执行结果写成一段简洁、专业的中文回复。\n"
                    "严格要求：\n"
                    "1. 只能陈述下面给出的事实，不得增加任何未提供的数字或结论；\n"
                    "2. 涉及客户信息时使用代号，不要拼出完整联系方式；\n"
                    "3. 不要承诺任何尚未执行的动作。\n\n"
                    f"执行结果：\n{factual_lines}"
                ),
            ),
        ]
        try:
            reply = await self.provider.generate_structured(messages, FinalReply, context)
            return reply.message
        except AgentError as exc:
            logger.warning(
                "reply_composition_failed",
                task_id=context.task_id,
                error=exc.message,
            )
            return factual_lines


def _render_facts(outcome: str, facts: dict[str, Any]) -> str:
    """用程序把事实渲染成模板文本。

    这个函数是「确定性逻辑不交给 LLM」在输出侧的落点：
    折扣率、单据号这些数字**只可能**来自这里，
    模型没有任何机会把 10% 说成 15%。
    """
    lines: list[str] = []
    header = {
        "COMPLETED": "✅ 处理完成",
        "PARTIAL_SUCCESS": "⚠️ 部分完成",
        "WAITING_APPROVAL": "⏳ 等待审批",
        "FAILED": "❌ 处理失败",
        "MANUAL_REVIEW": "🔍 已转人工处理",
        "CANCELLED": "🚫 已取消",
    }.get(outcome, outcome)
    lines.append(header)

    if facts.get("customer_id"):
        lines.append(f"- 客户：{facts['customer_id']}")
    if facts.get("discount_rate") is not None:
        lines.append(f"- 折扣幅度：{float(facts['discount_rate']):.0%}")
    if facts.get("discount_id"):
        lines.append(f"- 折扣单号：{facts['discount_id']}")
    if facts.get("notification_status"):
        lines.append(f"- 通知状态：{facts['notification_status']}")
    if facts.get("reason"):
        lines.append(f"- 说明：{facts['reason']}")
    return "\n".join(lines)
