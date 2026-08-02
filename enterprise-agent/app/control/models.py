"""控制层数据模型。

控制层是整个架构里**失效代价最高**的一层：它的失效通常不报错，
只是安静地造成损失——越权读到别人的数据、绕过审批直接执行。

所以这一层的数据模型有一个贯穿始终的设计原则：
**决策必须自带理由，而且理由要分成两份。**

* `reason_code`：机器可读。上层据此分流（重试 / 补偿 / 转人工）。
* `human_readable_reason`：给人看。出现在审批单和用户回复里。

只有 message 没有 code，事后就只能靠字符串匹配来分流，一改文案全线崩溃；
只有 code 没有 message，审批人看到「POLICY_DENIED_007」根本没法做判断。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.cognitive.models import ActionProposal
from app.context.models import AgentContext
from app.core.enums import ApprovalType, DecisionType, RiskLevel
from app.security.identity import ResolvedIdentity


class PolicyEvaluationRequest(BaseModel):
    """策略评估的输入。

    这个对象把「一次动作请求的全部判断依据」打包在一起：
    谁（identity）、想做什么（proposal）、在什么任务里（context）、
    工具本身是什么性质（tool_*）。

    刻意做成一个完整对象而不是一堆参数，是为了让每条策略拿到的信息一致——
    否则会出现「A 策略能看到客户等级、B 策略看不到」这种隐性差异，
    导致规则之间行为不一致且极难排查。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    proposal: ActionProposal
    identity: ResolvedIdentity
    context: AgentContext

    # ---- 工具的静态契约（来自注册表，不是模型说的）----
    tool_name: str
    tool_registered: bool = False
    tool_risk_level: RiskLevel = RiskLevel.LOW
    tool_required_permissions: set[str] = Field(default_factory=set)
    tool_is_write: bool = False
    tool_idempotent: bool = True

    #: 已经通过工具 `args_model` 校验的参数。ParameterPolicy 会填这里。
    #: **后续策略必须用这份，而不是 `proposal.arguments`**——
    #: 前者经过了强类型校验，后者是模型的原始输出。
    validated_arguments: dict[str, Any] = Field(default_factory=dict)

    #: 业务事实（如客户等级、现有折扣）。由 Orchestrator 从状态层查出来填入。
    #: **绝不从模型的输出里取这些事实**——事实要从系统里查，不能问模型。
    business_facts: dict[str, Any] = Field(default_factory=dict)

    #: 前序策略累计评定的风险等级。
    #:
    #: 由 PolicyEngine 在每条策略执行后回填。这是策略之间**唯一**的风险信息流动：
    #: RiskPolicy 根据参数算出真实风险后，ApprovalPolicy 才能基于它决定要不要审批。
    #: 如果 ApprovalPolicy 只看工具的静态风险等级，就会出现
    #: 「1% 的折扣和 30% 的折扣走同一条审批路径」这种既不安全也不好用的结果。
    assessed_risk: RiskLevel = RiskLevel.NONE

    #: 本步骤的重试次数，供 RateLimit / Risk 策略参考。
    attempt: int = 1
    #: 是否已经拿到审批。审批通过后重跑策略时置 True，避免无限循环要审批。
    approval_granted: bool = False


class PolicyEvaluationResult(BaseModel):
    """单条策略的评估结果。

    Attributes:
        decision: 这条策略的裁决。
        reason_code: 机器可读原因码。
        human_readable_reason: 给人看的原因。
        policy_name: 哪条策略给出的（审计必需——
            「为什么被拒了」要能定位到具体规则）。
        risk_level: 这条策略认定的风险等级。
        required_permissions / missing_permissions: 权限详情。
        approval_type: 需要哪一类审批。
        validated_arguments: 校验/规范化后的参数（ParameterPolicy 会填）。
        metadata: 附加信息，写入审计。
    """

    model_config = ConfigDict(extra="forbid")

    decision: DecisionType = DecisionType.ALLOW
    reason_code: str = "OK"
    human_readable_reason: str = ""
    policy_name: str = ""
    risk_level: RiskLevel = RiskLevel.NONE
    required_permissions: set[str] = Field(default_factory=set)
    missing_permissions: set[str] = Field(default_factory=set)
    approval_type: ApprovalType = ApprovalType.NONE
    validated_arguments: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def allow(cls, policy_name: str, **kwargs: Any) -> PolicyEvaluationResult:
        """构造一个放行结果。"""
        return cls(decision=DecisionType.ALLOW, policy_name=policy_name, **kwargs)

    @classmethod
    def deny(
        cls,
        policy_name: str,
        reason_code: str,
        reason: str,
        **kwargs: Any,
    ) -> PolicyEvaluationResult:
        """构造一个拒绝结果。"""
        return cls(
            decision=DecisionType.DENY,
            policy_name=policy_name,
            reason_code=reason_code,
            human_readable_reason=reason,
            **kwargs,
        )

    @classmethod
    def require_approval(
        cls,
        policy_name: str,
        reason_code: str,
        reason: str,
        approval_type: ApprovalType,
        **kwargs: Any,
    ) -> PolicyEvaluationResult:
        """构造一个「需要审批」结果。"""
        return cls(
            decision=DecisionType.REQUIRE_APPROVAL,
            policy_name=policy_name,
            reason_code=reason_code,
            human_readable_reason=reason,
            approval_type=approval_type,
            **kwargs,
        )

    @classmethod
    def manual_review(
        cls,
        policy_name: str,
        reason_code: str,
        reason: str,
        **kwargs: Any,
    ) -> PolicyEvaluationResult:
        """构造一个「转人工」结果。

        与 REQUIRE_APPROVAL 的区别：审批是「这件事做不做，你拍板」，
        转人工是「这件事程序判断不了，你来看看」。
        前者有明确的待办动作，后者需要人先搞清楚状况。
        """
        return cls(
            decision=DecisionType.MANUAL_REVIEW,
            policy_name=policy_name,
            reason_code=reason_code,
            human_readable_reason=reason,
            **kwargs,
        )


class PolicyDecision(BaseModel):
    """控制层的最终裁决（多条策略聚合后的结果）。

    这是控制层交给 Runtime 的唯一产物。Runtime 只看这个对象，
    不需要知道有几条策略、每条说了什么——那些细节在 `evaluations` 里，
    供审计与排查使用。

    Attributes:
        decision: 最终裁决。
        reason_code / human_readable_reason: 决定性理由（来自优先级最高的那条策略）。
        validated_arguments: **执行时必须使用这份参数**，
            而不是 `proposal.arguments`。这是「工具不能绕过控制层」的关键：
            执行器只接受 PolicyDecision 里的参数。
        required_permissions / missing_permissions: 权限详情。
        risk_level: 聚合后的最高风险等级。
        approval_type: 需要的审批类型。
        policy_version: 策略版本。**事后回放时要能回答「当时用的是哪版规则」**——
            没有这个字段，半年后你无法解释一笔当时被放行的操作。
        evaluations: 全部策略的评估明细。
    """

    model_config = ConfigDict(extra="forbid")

    decision: DecisionType
    reason_code: str = "OK"
    human_readable_reason: str = ""
    validated_arguments: dict[str, Any] = Field(default_factory=dict)
    required_permissions: set[str] = Field(default_factory=set)
    missing_permissions: set[str] = Field(default_factory=set)
    risk_level: RiskLevel = RiskLevel.LOW
    approval_type: ApprovalType = ApprovalType.NONE
    policy_version: str = ""
    evaluations: list[PolicyEvaluationResult] = Field(default_factory=list)

    @property
    def allowed(self) -> bool:
        """是否放行执行。

        只有 ALLOW 才返回 True。REQUIRE_APPROVAL 不是「有条件放行」，
        它是「现在不许执行」——必须先真的拿到审批，重新走一遍策略评估。
        """
        return self.decision == DecisionType.ALLOW

    def audit_payload(self) -> dict[str, Any]:
        """生成写入审计的载荷。

        包含每条策略的裁决，这样事后能回答
        「为什么这一步被拒了 / 为什么放行了」，
        而不只是看到一个孤零零的结论。
        """
        return {
            "decision": str(self.decision),
            "reason_code": self.reason_code,
            "human_readable_reason": self.human_readable_reason,
            "risk_level": str(self.risk_level),
            "approval_type": str(self.approval_type),
            "policy_version": self.policy_version,
            "missing_permissions": sorted(self.missing_permissions),
            "evaluations": [
                {
                    "policy": e.policy_name,
                    "decision": str(e.decision),
                    "reason_code": e.reason_code,
                    "risk_level": str(e.risk_level),
                }
                for e in self.evaluations
            ],
        }
