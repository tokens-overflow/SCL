"""BusinessRulePolicy：业务硬规则。

**这里是折扣示例的业务红线所在，也是本项目最想让人记住的一个位置。**

同样的规则在三个地方出现过：

1. 知识库文档（`app/context/retrieval.py` 的 DEMO_KNOWLEDGE）——给模型看的；
2. 系统提示词——给模型看的；
3. **这个文件**——真正执行的。

前两处的作用是让模型少提无效方案，减少往返。它们改错了、
被提示词注入绕过了、换个模型行为变了，**都不会导致越权**——
因为放行与否只看这个文件里的代码。

反过来说：如果哪天有人把折扣上限的判断从这里挪进 Prompt，
那这个系统就从「企业级」退回「Demo 级」了。这是一条不能退的线。

业务规则（示例）：

* 普通客服最多 5%；
* 5%~15% 需要经理审批；
* 超过 15% 直接拒绝，**不接受任何形式的特批**；
* VIP 客户额外放宽 3 个百分点，但仍然要过控制层；
* 已有生效折扣时不允许重复创建。
"""

from __future__ import annotations

from app.control.models import PolicyEvaluationRequest, PolicyEvaluationResult
from app.core.config import Settings, get_settings
from app.core.enums import ApprovalType, RiskLevel
from app.security.identity import PERM_DISCOUNT_APPLY_HIGH


class BusinessRulePolicy:
    """折扣业务规则。

    Args:
        settings: 配置对象。阈值来自配置而不是硬编码常量——
            业务阈值会变，改一个环境变量不该需要重新发版。
    """

    name = "BusinessRulePolicy"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def evaluate(self, request: PolicyEvaluationRequest) -> PolicyEvaluationResult:
        """评估业务规则。

        Returns:
            * 超过绝对上限 → DENY（**这是硬拒绝，任何审批都不能覆盖**）；
            * 超过自助额度 → REQUIRE_APPROVAL；
            * 已有生效折扣 → DENY；
            * 其余 → ALLOW。
        """
        if request.tool_name != "apply_discount":
            # 非折扣工具不归这条策略管。
            # 保持策略职责单一，而不是写一个什么都判断的巨型规则。
            return PolicyEvaluationResult.allow(self.name)

        # 关键：用 **validated_arguments**，不用 proposal.arguments。
        # 前者经过了 Pydantic 强校验，后者是模型的原始输出。
        args = request.validated_arguments or request.proposal.arguments
        rate = float(args.get("discount_rate") or 0.0)
        customer_id = str(args.get("customer_id") or "")

        facts = request.business_facts
        tier = str(facts.get("customer_tier") or "STANDARD")
        is_vip = tier.upper() in ("VIP", "PLATINUM")

        # —— 规则 1：已有生效折扣，不允许重复创建 ——
        # 放在最前面：它是一个「无论折扣多少都不行」的前置条件。
        active = facts.get("active_discount")
        if active:
            return PolicyEvaluationResult.deny(
                self.name,
                "DUPLICATE_ACTIVE_DISCOUNT",
                (
                    f"客户 {customer_id} 已有生效折扣 "
                    f"{float(active.get('discount_rate', 0)):.0%}，不能重复创建，"
                    "如需调整请先撤销原折扣"
                ),
                risk_level=RiskLevel.MEDIUM,
                metadata={"active_discount": active},
            )

        # —— 规则 2：绝对上限 ——
        # VIP 可以在标准额度上放宽，但**绝对上限本身不因 VIP 而改变**。
        # 这是刻意的：如果连硬上限都能被客户等级突破，那它就不是硬上限了。
        absolute_max = self.settings.discount_manager_approve_max
        if rate > absolute_max:
            return PolicyEvaluationResult.deny(
                self.name,
                "DISCOUNT_EXCEEDS_ABSOLUTE_LIMIT",
                (
                    f"申请折扣 {rate:.0%} 超过公司允许的最高折扣 {absolute_max:.0%}，"
                    "该规则不接受审批例外"
                ),
                risk_level=RiskLevel.CRITICAL,
                metadata={
                    "requested_rate": rate,
                    "absolute_max": absolute_max,
                    "customer_tier": tier,
                    # 明确标注：这是硬拒绝，审批也不能放行。
                    "override_allowed": False,
                },
            )

        # —— 规则 3：自助额度 ——
        # VIP 客户在这一档可以放宽（settings.discount_vip_bonus），
        # 但放宽之后仍然要走下面的判断——**没有「自动放行」这条路**。
        self_service_max = self.settings.discount_auto_approve_max
        if is_vip:
            self_service_max += self.settings.discount_vip_bonus

        if rate <= self_service_max:
            return PolicyEvaluationResult.allow(
                self.name,
                risk_level=RiskLevel.LOW if rate <= 0.05 else RiskLevel.MEDIUM,
                metadata={
                    "requested_rate": rate,
                    "self_service_max": self_service_max,
                    "customer_tier": tier,
                    "vip_bonus_applied": is_vip,
                },
            )

        # —— 规则 4：需要经理审批 ——
        # 注意这里**不检查用户是不是经理然后直接放行**。
        # 即使操作人本身就是经理，也要走一遍审批流程留痕——
        # 「谁批准的」这个问题必须有一条独立的记录来回答，
        # 而不是靠「他当时的角色是经理」来推断。
        return PolicyEvaluationResult.require_approval(
            self.name,
            "DISCOUNT_REQUIRES_MANAGER_APPROVAL",
            (
                f"申请折扣 {rate:.0%} 超过自助额度 {self_service_max:.0%}，"
                f"需要客服经理审批（上限 {absolute_max:.0%}）"
            ),
            ApprovalType.MANAGER,
            risk_level=RiskLevel.HIGH,
            required_permissions={PERM_DISCOUNT_APPLY_HIGH},
            metadata={
                "requested_rate": rate,
                "self_service_max": self_service_max,
                "absolute_max": absolute_max,
                "customer_tier": tier,
            },
        )
