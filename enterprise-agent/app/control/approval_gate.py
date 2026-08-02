"""审批闸门。

负责三件事：

1. 创建审批单（把「将要执行的确切动作」快照下来给人看）；
2. 校验审批人资格（含**四眼原则**：审批人不能是发起人）；
3. 把审批结果转换成可以驱动状态机的事件。

一个容易被忽略的设计点：**审批单里必须存动作快照，而不是任务 ID**。

如果只存 task_id，审批人点开看到的是「当前任务状态」——
但任务在等审批期间状态可能已经变了，而且他真正需要看的是
「批准之后到底会执行什么」。存快照才能保证：
**他批准的，就是后来执行的那一个**。
"""

from __future__ import annotations

from typing import Any

from app.control.authorization import AuthorizationService, default_authorization_service
from app.control.models import PolicyDecision
from app.core.config import Settings, get_settings
from app.core.enums import ApprovalStatus, ApprovalType, RiskLevel
from app.core.errors import PermissionDeniedError, ValidationError
from app.runtime.models import ApprovalRequest, TaskStep
from app.state.repositories import ApprovalRepository

#: 审批类型 → 所需角色。集中定义，避免各处硬编码角色名。
APPROVER_ROLE_BY_TYPE: dict[ApprovalType, str] = {
    ApprovalType.MANAGER: "cs_manager",
    ApprovalType.COMPLIANCE: "compliance_officer",
    ApprovalType.SECURITY: "security_officer",
    ApprovalType.NONE: "cs_manager",
}


class ApprovalGate:
    """审批单的创建与决策处理。

    Args:
        repository: 审批仓库。
        authorization: 身份服务。
        settings: 配置对象（提供审批超时时长）。
    """

    def __init__(
        self,
        repository: ApprovalRepository,
        *,
        authorization: AuthorizationService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.repository = repository
        self.authorization = authorization or default_authorization_service
        self.settings = settings or get_settings()

    async def request_approval(
        self,
        *,
        task_id: str,
        step: TaskStep,
        decision: PolicyDecision,
        requester: str,
        tool_name: str,
    ) -> ApprovalRequest:
        """创建审批单（幂等）。

        Args:
            task_id: 任务 ID。
            step: 待审批的步骤。
            decision: 触发审批的策略裁决。
            requester: 发起人。
            tool_name: 将要执行的工具。

        Returns:
            审批单。**如果该步骤已有待审批单，直接返回它**——
            重复创建会把审批人淹没，而审批疲劳会让真正重要的审批被随手点过。

        Note:
            `requested_action` 里存的是 `decision.validated_arguments`，
            也就是**已经通过全部参数校验的最终参数**。
            审批人看到什么，执行的就是什么。
        """
        existing = await self.repository.find_pending_for_step(step.step_id)
        if existing is not None:
            return existing

        approver_role = APPROVER_ROLE_BY_TYPE.get(decision.approval_type, "cs_manager")
        return await self.repository.create(
            task_id=task_id,
            step_id=step.step_id,
            requested_action={
                "tool_name": tool_name,
                "step_name": step.step_name,
                # 存的是校验后的参数，不是模型的原始输出。
                "arguments": decision.validated_arguments,
                "policy_reason_code": decision.reason_code,
                "policy_version": decision.policy_version,
            },
            requester=requester,
            approver_role=approver_role,
            reason=decision.human_readable_reason,
            risk_level=decision.risk_level,
            timeout_seconds=self.settings.approval_timeout_seconds,
        )

    async def decide(
        self,
        approval_id: str,
        *,
        approved: bool,
        approver_id: str,
        comment: str | None = None,
    ) -> ApprovalRequest:
        """记录审批决策。

        Args:
            approval_id: 审批单 ID。
            approved: 是否批准。
            approver_id: 审批人。
            comment: 审批意见。

        Returns:
            更新后的审批单。

        Raises:
            ValidationError: 审批单已过期。**过期的审批不能被追认**——
                否则「超时回收」这条规则就形同虚设，任务会在不确定的时刻
                被一个很久以前的决定重新激活。
            PermissionDeniedError: 审批人无资格，或违反四眼原则。
        """
        approval = await self.repository.get(approval_id)
        if approval is None:
            from app.core.errors import ApprovalNotFoundError

            raise ApprovalNotFoundError("审批单不存在", details={"approval_id": approval_id})

        # 已决策的审批单直接返回：审批回调可能被重复投递（人手抖、消息重投），
        # 第二次不应该产生任何额外效果。
        if approval.status != ApprovalStatus.PENDING:
            return approval

        if approval.is_expired():
            await self.repository.decide(
                approval_id,
                status=ApprovalStatus.EXPIRED,
                approver_id=approver_id,
                comment="审批超时自动回收",
            )
            raise ValidationError(
                "该审批单已超时失效，请重新发起请求",
                details={"approval_id": approval_id, "expires_at": str(approval.expires_at)},
            )

        # 四眼原则：审批人不能是发起人。
        # 这条规则的意义不在于防坏人（他可以找同事互批），
        # 而在于**制度上不允许一个人独自完成一次高风险操作**——
        # 它让「我没注意」这种失误至少要两个人同时犯才会造成后果。
        if approver_id == approval.requester:
            raise PermissionDeniedError(
                "审批人不能是发起人（四眼原则）",
                details={"approval_id": approval_id, "requester": approval.requester},
            )

        if not await self.authorization.is_approver(approver_id, approval.approver_role):
            raise PermissionDeniedError(
                f"当前用户不具备 {approval.approver_role} 审批资格",
                details={"approval_id": approval_id, "approver_id": approver_id},
            )

        return await self.repository.decide(
            approval_id,
            status=ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED,
            approver_id=approver_id,
            comment=comment,
        )

    async def expire_overdue(self) -> list[ApprovalRequest]:
        """回收超时未决的审批单。

        由 Scheduler 周期调用。这是「每个任务最终都落到明确终态」的保证之一：
        没有这一步，等审批的任务会永远悬着。
        """
        return await self.repository.expire_overdue()

    @staticmethod
    def audit_payload(approval: ApprovalRequest, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """生成审批相关的审计载荷。"""
        payload: dict[str, Any] = {
            "approval_id": approval.approval_id,
            "status": str(approval.status),
            "approver_role": approval.approver_role,
            "approver_id": approval.approver_id,
            "requester": approval.requester,
            "risk_level": str(approval.risk_level),
            "requested_action": approval.requested_action,
        }
        if extra:
            payload.update(extra)
        return payload


def approval_risk_summary(decision: PolicyDecision) -> str:
    """生成给审批人看的一句话风险说明。"""
    if decision.risk_level.order >= RiskLevel.CRITICAL.order:
        prefix = "【极高风险】"
    elif decision.risk_level.order >= RiskLevel.HIGH.order:
        prefix = "【高风险】"
    else:
        prefix = ""
    return f"{prefix}{decision.human_readable_reason}"
