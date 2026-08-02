"""ApprovalPolicy：审批门槛。

**为什么高风险操作需要人工审批？**

不是因为模型不够聪明，而是因为**责任需要有人承担**。

一笔 12% 的折扣被批准之后，如果事后被质疑，必须有人能说
「是我批的，理由是这个客户去年贡献了 XX 万」。这个「我」不能是模型——
模型不能出席复盘会，不能承担后果，也不能在下次遇到类似情况时
把这次的教训真正内化成组织记忆。

所以审批的价值不在「多一道校验」，而在**把一个不确定的判断
明确地绑定到一个可问责的人身上**。

第二个技术层面的理由：审批挂起和崩溃恢复在实现上是同一件事——
把任务冻在某一步，等外部条件满足后再继续。所以做好了断点续跑之后，
Human-in-the-Loop 几乎是白送的（见 `app/runtime/recovery.py`）。
"""

from __future__ import annotations

from app.control.models import PolicyEvaluationRequest, PolicyEvaluationResult
from app.core.enums import ApprovalType, RiskLevel


class ApprovalPolicy:
    """按风险等级决定是否需要审批。

    Args:
        approval_threshold: 触发审批的最低风险等级。默认 HIGH。
    """

    name = "ApprovalPolicy"

    def __init__(self, approval_threshold: RiskLevel = RiskLevel.HIGH) -> None:
        self.approval_threshold = approval_threshold

    async def evaluate(self, request: PolicyEvaluationRequest) -> PolicyEvaluationResult:
        """评估是否需要审批。

        Returns:
            需要审批时 REQUIRE_APPROVAL，否则 ALLOW。

        Note:
            如果 `approval_granted=True`（审批已通过后重跑策略），
            这条策略直接放行——否则会陷入「要审批 → 批了 → 又要审批」的死循环。

            但要特别注意：**只有这条策略被审批结果短路，其它策略照跑不误。**
            也就是说，即使经理批准了，如果折扣超过绝对上限，
            BusinessRulePolicy 依然会拒绝。审批能覆盖的是「需要授权的操作」，
            覆盖不了「无论谁都不允许的操作」。
        """
        if request.approval_granted:
            return PolicyEvaluationResult.allow(
                self.name,
                metadata={"note": "审批已通过，本策略放行；其它策略仍然照常评估"},
            )

        # 只读操作不需要审批，无论风险等级怎么算。
        # 读操作没有副作用，让人批准一次「查询」纯粹是浪费审批人的时间——
        # 而审批疲劳会让真正重要的审批也被随手点过。
        if not request.tool_is_write:
            return PolicyEvaluationResult.allow(self.name)

        # 风险等级取三者最高：
        #
        # 1. `assessed_risk` —— **RiskPolicy 按实际参数算出来的真实风险**，
        #    由 PolicyEngine 回填。这是最重要的一项：5% 和 30% 的折扣
        #    调用的是同一个工具，但风险完全不同。
        # 2. `tool_risk_level` —— 工具声明的基线风险。
        # 3. `proposal.risk_hint` —— 模型的提示，只在「往高了说」时被采纳。
        #
        # 正因为 ApprovalPolicy 排在策略链最后，它才能看到前面所有策略的评定结果。
        effective_risk = max(
            request.assessed_risk, request.tool_risk_level, request.proposal.risk_hint
        )

        if effective_risk.order >= self.approval_threshold.order:
            approval_type = (
                ApprovalType.COMPLIANCE
                if effective_risk == RiskLevel.CRITICAL
                else ApprovalType.MANAGER
            )
            return PolicyEvaluationResult.require_approval(
                self.name,
                "HIGH_RISK_REQUIRES_APPROVAL",
                (
                    f"该操作风险等级为 {effective_risk}，"
                    f"达到审批阈值 {self.approval_threshold}，需人工审批"
                ),
                approval_type,
                risk_level=effective_risk,
                metadata={"threshold": str(self.approval_threshold)},
            )

        return PolicyEvaluationResult.allow(self.name, risk_level=effective_risk)
