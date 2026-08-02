"""RiskPolicy：风险评估。

**模型给的 `risk_hint` 只是提示，真实风险等级由这条策略判定。**

为什么不能信模型的自评：

1. 模型可能真诚地低估——它不知道这个客户上个月刚投诉过；
2. 模型可能被诱导——提示词注入的一个典型目标就是让模型把
   高风险动作标成低风险，从而绕过审批；
3. 模型换一个版本，标注的口径就变了，而风险阈值是要写进制度的。

风险评分基于**系统能验证的客观因素**：工具本身的风险等级、
金额大小、是否写操作、是否可补偿、重试次数、输入是否可疑。
模型的 `risk_hint` 只在一个方向上被采纳：**它说高，我们就至少按高算**
（取 max）。反过来它说低，我们不理会。这是一个刻意的不对称设计。
"""

from __future__ import annotations

from app.control.models import PolicyEvaluationRequest, PolicyEvaluationResult
from app.core.config import Settings, get_settings
from app.core.enums import RiskLevel


class RiskPolicy:
    """基于客观因素的风险评分。

    Args:
        settings: 配置对象。
    """

    name = "RiskPolicy"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def evaluate(self, request: PolicyEvaluationRequest) -> PolicyEvaluationResult:
        """评估风险等级。

        Returns:
            通常返回 ALLOW（携带评定的 risk_level，供 ApprovalPolicy 使用）；
            只有在**不可补偿的高风险写操作**这种组合下才主动 MANUAL_REVIEW。
        """
        factors: list[str] = []
        # 起点是工具自己声明的风险等级——这是最可靠的客观事实。
        level = request.tool_risk_level

        if request.tool_is_write:
            factors.append("write_operation")
            level = max(level, RiskLevel.MEDIUM)

        # 不幂等的写操作风险极高：重试就意味着重复副作用。
        # 注册表其实已经禁止注册这种工具，这里是纵深防御。
        if request.tool_is_write and not request.tool_idempotent:
            factors.append("non_idempotent_write")
            level = max(level, RiskLevel.CRITICAL)

        # 折扣金额越大风险越高。这是可验证的客观事实，不需要问模型。
        args = request.validated_arguments or request.proposal.arguments
        rate = args.get("discount_rate")
        if isinstance(rate, (int, float)):
            if rate > self.settings.discount_auto_approve_max:
                factors.append("above_self_service_limit")
                level = max(level, RiskLevel.HIGH)
            elif rate > 0:
                factors.append("monetary_impact")
                level = max(level, RiskLevel.MEDIUM)

        # 反复重试说明系统正处在不确定状态，风险要往上抬。
        if request.attempt > 2:
            factors.append("repeated_attempts")
            level = max(level, RiskLevel.MEDIUM)

        # 输入被标记为疑似提示词注入 → 加权。
        # 注意是**加权不是拒绝**：净化器的规则不可能穷尽，
        # 用它做二元判断会造成大量误伤，而作为风险信号则很有价值。
        if request.context.extra.get("input_suspicious"):
            factors.append("suspicious_input")
            level = max(level, RiskLevel.HIGH)

        # 模型的提示只在「往高了说」的方向被采纳。
        hint = request.proposal.risk_hint
        if hint.order > level.order:
            factors.append("model_hint_escalation")
            level = hint

        # 置信度过低不是拒绝的理由，但值得让人看一眼。
        low_confidence = request.proposal.confidence < 0.5

        metadata = {
            "risk_factors": factors,
            "model_risk_hint": str(hint),
            "model_confidence": request.proposal.confidence,
            "assessed_risk": str(level),
        }

        # 高风险 + 不可补偿 = 做错了就收不回来。这种组合必须让人看一眼。
        if (
            level.order >= RiskLevel.CRITICAL.order
            and request.tool_is_write
        ):
            return PolicyEvaluationResult.manual_review(
                self.name,
                "CRITICAL_RISK_REQUIRES_REVIEW",
                f"该动作被评估为 {level} 风险，需人工确认后方可继续",
                risk_level=level,
                metadata=metadata,
            )

        if low_confidence and request.tool_is_write:
            return PolicyEvaluationResult.manual_review(
                self.name,
                "LOW_CONFIDENCE_WRITE",
                (
                    f"模型对该写操作的置信度仅 {request.proposal.confidence:.0%}，"
                    "为避免误操作转人工确认"
                ),
                risk_level=max(level, RiskLevel.HIGH),
                metadata=metadata,
            )

        return PolicyEvaluationResult.allow(self.name, risk_level=level, metadata=metadata)
