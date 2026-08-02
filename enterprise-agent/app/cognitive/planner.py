"""任务规划。

把意图变成一串**结构化动作建议**（ActionProposal）。

三条设计纪律：

1. **计划只是建议。** 每一步在执行前都要单独过控制层——
   不存在「计划整体被批准了，后面几步就放行」这种事。
   因为业务事实会变：第一步查出来客户已经有生效折扣，
   第二步就必须被拒绝，哪怕计划里写着要执行。

2. **不可撤回的动作排在最后。** 通知类步骤永远是最后一步。
   这条纪律由本模块和 BusinessRulePolicy 共同保证，**不依赖模型自觉**。

3. **步骤名必须稳定。** 因为它参与幂等键生成。
   如果模型这次叫 `apply_discount`、下次叫 `do_discount`，
   同一个动作就会被算成两笔，幂等失效。
   所以本模块会对模型给的步骤名做规范化。
"""

from __future__ import annotations

from app.cognitive.models import ExecutionPlan, IntentParseResult, PlannedStep
from app.context.models import AgentContext
from app.core.enums import StepType
from app.llm.base import LLMMessage, LLMProvider
from app.operations.logging import get_logger
from app.security.sanitization import wrap_untrusted

logger = get_logger(__name__)

#: 工具名 → 步骤类型。用于判断哪些步骤需要幂等键与对账。
#: 由注册表的工具元数据推导，这里作为兜底。
_NOTIFY_TOOLS = {"send_notification"}


class Planner:
    """基于 LLM 的任务规划器。

    Args:
        provider: LLM Provider。
        registry: 工具注册表，用于推导步骤类型与顺序校验。
    """

    def __init__(self, provider: LLMProvider, registry) -> None:  # noqa: ANN001
        self.provider = provider
        self.registry = registry

    async def plan(self, context: AgentContext, intent: IntentParseResult) -> ExecutionPlan:
        """生成执行计划。

        Args:
            context: 上下文。
            intent: 已解析的意图。

        Returns:
            :class:`ExecutionPlan`，步骤顺序已经过规范化。
        """
        messages = [
            LLMMessage(role="system", content=context.render_system_prompt()),
            LLMMessage(
                role="system",
                content=(
                    f"已解析的用户意图：{intent.intent}，实体：{intent.entities}。\n"
                    "请规划完成该意图所需的步骤序列。要求：\n"
                    "1. 只能使用【可用工具】清单中的工具；\n"
                    "2. 先查询后写入，写入前必须有信息核对步骤；\n"
                    "3. 通知类动作不可撤回，必须放在最后一步；\n"
                    "4. 通知步骤的 critical 置为 false（通知失败不应导致整单失败）；\n"
                    "5. step_name 使用与工具名一致的稳定标识。"
                ),
            ),
            LLMMessage(role="user", content=wrap_untrusted(context.user_input)),
        ]

        plan = await self.provider.generate_structured(messages, ExecutionPlan, context)
        normalized = self._normalize(plan)

        logger.info(
            "plan_generated",
            task_id=context.task_id,
            trace_id=context.trace_id,
            step_count=len(normalized.steps),
            steps=[s.step_name for s in normalized.steps],
        )
        return normalized

    def _normalize(self, plan: ExecutionPlan) -> ExecutionPlan:
        """规范化计划：稳定步骤名 + 强制不可撤回动作排在最后。

        **这里不信任模型的排序。** 即使提示词里写清楚了「通知放最后」，
        模型也可能排错。而排错的代价是：折扣失败时短信已经发出去了，
        收不回来。所以由程序强制重排——
        这是「确定性逻辑不交给 LLM」的一个具体例子。
        """
        steps = list(plan.steps)

        # 步骤名规范化：统一用工具名，保证幂等键稳定。
        for step in steps:
            if not step.step_name or step.step_name != step.proposal.tool_name:
                step.step_name = step.proposal.tool_name

        # 强制重排：不可撤回的动作（通知）沉底。
        def sort_key(item: tuple[int, PlannedStep]) -> tuple[int, int]:
            idx, step = item
            is_notify = step.proposal.tool_name in _NOTIFY_TOOLS or (
                self.registry.has(step.proposal.tool_name)
                and self.registry.get(step.proposal.tool_name).step_type == StepType.NOTIFY
            )
            return (1 if is_notify else 0, idx)

        reordered = [step for _, step in sorted(enumerate(steps), key=sort_key)]

        # 通知步骤强制标记为非关键：
        # 折扣成功但通知失败应该是 PARTIAL_SUCCESS，不是整单失败，
        # 更不该触发对已生效折扣的补偿。
        for step in reordered:
            if step.proposal.tool_name in _NOTIFY_TOOLS:
                step.critical = False

        return plan.model_copy(update={"steps": reordered})
