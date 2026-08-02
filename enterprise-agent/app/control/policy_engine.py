"""Policy Engine：按顺序执行多条策略并聚合裁决。

**聚合规则的优先级（这是整个控制层的核心逻辑）：**

    1. 明确禁止（DENY）优先
    2. 权限不足优先
    3. 高风险审批（REQUIRE_APPROVAL / MANUAL_REVIEW）优先
    4. 所有规则都通过，才能 ALLOW

换句话说：**ALLOW 是最弱的裁决**，任何一条策略给出更强的裁决都会覆盖它。
这个方向不能反——如果实现成「有一条 ALLOW 就放行」，
那么加一条新策略反而可能让系统变得更宽松，这是灾难性的设计。

**执行顺序也有讲究**：先跑便宜的、能提前短路的（身份、权限），
再跑贵的（参数校验要构造 Pydantic 对象、业务规则要查数据库事实）。
但注意——**短路只是性能优化，不能改变结论**。所以默认配置下
引擎会跑完全部策略（`fail_fast=False`），把每条的结论都记进审计。
只有在明确的 DENY 之后才会短路，因为那时结论已经不可能改变了。
"""

from __future__ import annotations

from app.control.models import (
    PolicyDecision,
    PolicyEvaluationRequest,
    PolicyEvaluationResult,
)
from app.control.policies import Policy
from app.core.config import Settings, get_settings
from app.core.enums import ApprovalType, DecisionType, RiskLevel
from app.operations.logging import get_logger

logger = get_logger(__name__)

#: 裁决优先级。数值越大越「强」，聚合时取最强的那个。
DECISION_PRIORITY: dict[DecisionType, int] = {
    DecisionType.ALLOW: 0,
    DecisionType.RETRY: 1,
    DecisionType.REQUIRE_APPROVAL: 2,
    DecisionType.MANUAL_REVIEW: 3,
    DecisionType.DENY: 4,
}


class PolicyEngine:
    """策略引擎。

    Args:
        policies: 策略列表，**按执行顺序排列**。
        settings: 配置对象（提供 policy_version）。
        fail_fast: 遇到 DENY 是否立即短路。
            默认 True——DENY 之后结论已经不可能改变，继续跑只是浪费。
            但**非 DENY 的情况一定会跑完全部策略**，
            因为「需要审批」和「参数不合法」需要同时被记录下来。
    """

    def __init__(
        self,
        policies: list[Policy],
        *,
        settings: Settings | None = None,
        fail_fast: bool = True,
    ) -> None:
        self.policies = policies
        self.settings = settings or get_settings()
        self.fail_fast = fail_fast

    async def evaluate(self, request: PolicyEvaluationRequest) -> PolicyDecision:
        """依次执行全部策略并聚合裁决。

        Args:
            request: 策略评估请求。

        Returns:
            :class:`PolicyDecision`。**这是控制层交给 Runtime 的唯一产物**，
            Runtime 只看它，不需要知道有几条策略。

        Note:
            有一个细节很重要：`ParameterPolicy` 产出的 `validated_arguments`
            会被**回填到 request 上**，供后续策略使用。
            这样 BusinessRulePolicy 拿到的就是强类型校验过的参数，
            而不是模型的原始输出。这个回填是策略之间唯一的数据流动，
            刻意做得很窄——策略之间不应该互相依赖太多。
        """
        evaluations: list[PolicyEvaluationResult] = []
        strongest = PolicyEvaluationResult.allow("PolicyEngine")
        highest_risk = RiskLevel.NONE
        required: set[str] = set()
        missing: set[str] = set()
        approval_type = ApprovalType.NONE

        for policy in self.policies:
            result = await policy.evaluate(request)
            result.policy_name = result.policy_name or getattr(policy, "name", type(policy).__name__)

            # ----------------------------------------------------------------
            # 审批已通过时，把 REQUIRE_APPROVAL **降级**为 ALLOW。
            #
            # 这条规则必须放在引擎里，而不是让每条策略自己判断 `approval_granted`：
            # 任何一条策略都可能要求审批（BusinessRulePolicy 因为金额、
            # ApprovalPolicy 因为风险等级），漏掉任何一条都会导致
            # 「批了之后又要批」的死循环——而且这个 Bug 只在审批路径上出现，
            # 平时的自助额度流程完全测不到。
            #
            # **关键边界：只降级 REQUIRE_APPROVAL，绝不降级 DENY。**
            # 审批能覆盖的是「需要授权才能做的事」，
            # 覆盖不了「无论谁批都不允许的事」——
            # 30% 的折扣即使经理点了同意，BusinessRulePolicy 照样拒绝。
            # ----------------------------------------------------------------
            if request.approval_granted and result.decision == DecisionType.REQUIRE_APPROVAL:
                logger.info(
                    "approval_satisfied_downgrade",
                    policy=result.policy_name,
                    reason_code=result.reason_code,
                    tool_name=request.tool_name,
                )
                result = result.model_copy(
                    update={
                        "decision": DecisionType.ALLOW,
                        "reason_code": f"{result.reason_code}__APPROVED",
                        "human_readable_reason": (
                            f"{result.human_readable_reason}（审批已通过）"
                        ),
                        "approval_type": ApprovalType.NONE,
                        "metadata": {
                            **result.metadata,
                            "original_decision": str(DecisionType.REQUIRE_APPROVAL),
                            "downgraded_by": "approval_granted",
                        },
                    }
                )

            evaluations.append(result)

            # 参数校验结果回填：后续策略必须基于校验后的参数做判断。
            if result.validated_arguments is not None:
                request = request.model_copy(
                    update={"validated_arguments": result.validated_arguments}
                )

            highest_risk = max(highest_risk, result.risk_level)
            # 把累计风险回填给后续策略。ApprovalPolicy 依赖它——
            # 只有拿到 RiskPolicy 按参数算出的真实风险，
            # 「要不要审批」才可能判断正确。
            if highest_risk != request.assessed_risk:
                request = request.model_copy(update={"assessed_risk": highest_risk})
            required |= result.required_permissions
            missing |= result.missing_permissions
            if result.approval_type != ApprovalType.NONE:
                approval_type = result.approval_type

            # 聚合：取最强裁决。
            if DECISION_PRIORITY[result.decision] > DECISION_PRIORITY[strongest.decision]:
                strongest = result

            if self.fail_fast and result.decision == DecisionType.DENY:
                # 已经是最强裁决，后面跑什么都不会改变结论。
                logger.info(
                    "policy_engine_short_circuit",
                    policy=result.policy_name,
                    reason_code=result.reason_code,
                    tool_name=request.tool_name,
                )
                break

        decision = PolicyDecision(
            decision=strongest.decision,
            reason_code=strongest.reason_code,
            human_readable_reason=strongest.human_readable_reason
            or _default_reason(strongest.decision),
            validated_arguments=request.validated_arguments,
            required_permissions=required,
            missing_permissions=missing,
            risk_level=highest_risk if highest_risk != RiskLevel.NONE else RiskLevel.LOW,
            approval_type=(
                approval_type
                if strongest.decision == DecisionType.REQUIRE_APPROVAL
                else ApprovalType.NONE
            ),
            policy_version=self.settings.policy_version,
            evaluations=evaluations,
        )

        logger.info(
            "policy_decision",
            tool_name=request.tool_name,
            decision=str(decision.decision),
            reason_code=decision.reason_code,
            risk_level=str(decision.risk_level),
            policy_count=len(evaluations),
        )
        return decision


def _default_reason(decision: DecisionType) -> str:
    return {
        DecisionType.ALLOW: "所有策略均已通过",
        DecisionType.DENY: "策略拒绝执行",
        DecisionType.REQUIRE_APPROVAL: "该操作需要人工审批",
        DecisionType.MANUAL_REVIEW: "该操作需要人工确认",
        DecisionType.RETRY: "建议稍后重试",
    }.get(decision, "")


def build_default_policy_engine(
    registry,  # noqa: ANN001 - 避免与 registry 模块循环导入
    *,
    settings: Settings | None = None,
    rate_limiter: object | None = None,
) -> PolicyEngine:
    """构造默认策略链。

    **顺序即语义**，从上到下依次是：

    1. IdentityPolicy         —— 你是谁？身份本身站得住吗？
    2. AgentPermissionPolicy  —— 这个 Agent 允许碰这个工具吗？（含未注册拦截）
    3. ToolPermissionPolicy   —— 三方权限交集够吗？
    4. ParameterPolicy        —— 参数强校验（产出 validated_arguments）
    5. DataAccessPolicy       —— 能碰**这一条**数据吗？
    6. SensitiveDataPolicy    —— 参数里有不该出现的敏感信息吗？
    7. BusinessRulePolicy     —— 业务硬规则（折扣上限等）
    8. RiskPolicy             —— 客观风险评分
    9. RateLimitPolicy        —— 频率与熔断
    10. ApprovalPolicy        —— 是否需要人工审批（放最后：
        它要基于前面所有策略认定的风险来判断）

    Args:
        registry: 工具注册表。
        settings: 配置对象。
        rate_limiter: 可选的 RateLimitPolicy 实例（需要跨请求共享计数器）。

    Returns:
        配置好的 :class:`PolicyEngine`。
    """
    from app.control.policies import (
        AgentPermissionPolicy,
        ApprovalPolicy,
        BusinessRulePolicy,
        DataAccessPolicy,
        IdentityPolicy,
        ParameterPolicy,
        RateLimitPolicy,
        RiskPolicy,
        SensitiveDataPolicy,
        ToolPermissionPolicy,
    )

    settings = settings or get_settings()
    limiter = rate_limiter if rate_limiter is not None else RateLimitPolicy()

    return PolicyEngine(
        [
            IdentityPolicy(),
            AgentPermissionPolicy(),
            ToolPermissionPolicy(),
            ParameterPolicy(registry),
            DataAccessPolicy(),
            SensitiveDataPolicy(),
            BusinessRulePolicy(settings),
            RiskPolicy(settings),
            limiter,  # type: ignore[list-item]
            ApprovalPolicy(),
        ],
        settings=settings,
    )
