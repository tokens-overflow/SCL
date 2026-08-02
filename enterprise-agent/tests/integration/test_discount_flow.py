"""折扣业务流程集成测试。

覆盖验收场景一 ~ 三：允许执行、需要审批、直接拒绝。
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.enums import ApprovalStatus, StepStatus, TaskStatus
from app.state.models import DiscountORM
from app.state.repositories import ApprovalRepository, AuditRepository


class TestScenario1Allow:
    """场景一：用户申请 5% 折扣 → 放行 → COMPLETED。"""

    async def test_five_percent_completes(self, orchestrator, seeded_session) -> None:
        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九五折，并通知客户。",
        )
        assert task.status == TaskStatus.COMPLETED, task.error_message

        # 步骤全部成功
        assert [s.step_name for s in task.steps] == [
            "query_customer", "apply_discount", "send_notification",
        ]
        assert all(s.status == StepStatus.SUCCESS for s in task.steps)

        # 折扣真的写进了业务系统（**从业务表查，不是读任务自己的输出**）
        result = await seeded_session.execute(
            select(DiscountORM).where(DiscountORM.customer_id == "C001")
        )
        discount = result.scalars().first()
        assert discount is not None
        assert abs(discount.discount_rate - 0.05) < 1e-9
        assert discount.status == "ACTIVE"

        # 每个写步骤都有幂等键和外部凭证
        write_step = task.step_by_name("apply_discount")
        assert write_step.idempotency_key
        assert write_step.external_reference_id == discount.discount_id

    async def test_audit_trail_is_complete(self, orchestrator, seeded_session) -> None:
        """审计必须覆盖全链路的关键动作。"""
        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九五折，并通知客户。",
        )
        events = await AuditRepository(seeded_session).list_by_task(task.task_id)
        types = {e.event_type for e in events}

        for required in (
            "REQUEST_RECEIVED", "TASK_CREATED", "CONTEXT_BUILT",
            "LLM_CALL_STARTED", "LLM_CALL_FINISHED", "PROPOSAL_GENERATED",
            "POLICY_DECISION", "TOOL_EXECUTION_STARTED", "TOOL_EXECUTION_FINISHED",
            "STATE_TRANSITION", "TASK_COMPLETED",
        ):
            assert required in types, f"缺少审计事件：{required}"

        # 所有事件都能被 trace_id 串起来
        assert all(e.trace_id == task.trace_id for e in events)

    async def test_no_pii_in_audit(self, orchestrator, seeded_session) -> None:
        """审计里不允许出现未脱敏的个人信息。"""
        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九五折，并通知客户。",
        )
        events = await AuditRepository(seeded_session).list_by_task(task.task_id)
        dumped = str([e.payload for e in events])
        assert "13812345678" not in dumped
        assert "zhangsan@example.com" not in dumped


class TestScenario2RequiresApproval:
    """场景二：10% 折扣 → REQUIRE_APPROVAL → 审批后断点续跑。"""

    async def test_ten_percent_waits_for_approval(self, orchestrator, seeded_session) -> None:
        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九折，并通知客户。",
        )
        assert task.status == TaskStatus.WAITING_APPROVAL

        # 前置的查询步骤**已经成功**，不会被重跑
        assert task.step_by_name("query_customer").status == StepStatus.SUCCESS
        assert task.step_by_name("apply_discount").status == StepStatus.WAITING_APPROVAL

        # 折扣绝对没有被执行
        result = await seeded_session.execute(
            select(DiscountORM).where(DiscountORM.customer_id == "C001")
        )
        assert result.scalars().first() is None

        # 审批单存的是**将要执行的确切参数**
        approvals = await ApprovalRepository(seeded_session).list_approvals()
        assert len(approvals) == 1
        action = approvals[0].requested_action
        assert action["tool_name"] == "apply_discount"
        assert abs(action["arguments"]["discount_rate"] - 0.1) < 1e-9
        assert approvals[0].approver_role == "cs_manager"

    async def test_resume_after_approval_skips_completed_steps(
        self, orchestrator, seeded_session
    ) -> None:
        """**审批后不重跑已成功的前置步骤** —— 断点续跑的核心价值。"""
        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九折，并通知客户。",
        )
        query_step = task.step_by_name("query_customer")
        completed_at_before = query_step.completed_at

        repo = ApprovalRepository(seeded_session)
        approval = (await repo.list_approvals())[0]
        from app.control.approval_gate import ApprovalGate

        await ApprovalGate(repo).decide(
            approval.approval_id, approved=True, approver_id="manager_001", comment="客户价值高，同意"
        )

        resumed = await orchestrator.resume_task(task.task_id)
        assert resumed.status == TaskStatus.COMPLETED

        # 查询步骤的完成时间没变 → 它没有被重新执行
        assert resumed.step_by_name("query_customer").completed_at == completed_at_before
        assert resumed.step_by_name("apply_discount").status == StepStatus.SUCCESS

        result = await seeded_session.execute(
            select(DiscountORM).where(DiscountORM.customer_id == "C001")
        )
        discount = result.scalars().first()
        assert discount is not None and abs(discount.discount_rate - 0.1) < 1e-9

    async def test_rejection_fails_task_without_side_effect(
        self, orchestrator, seeded_session
    ) -> None:
        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九折，并通知客户。",
        )
        repo = ApprovalRepository(seeded_session)
        approval = (await repo.list_approvals())[0]
        from app.control.approval_gate import ApprovalGate

        await ApprovalGate(repo).decide(
            approval.approval_id, approved=False, approver_id="manager_001", comment="折扣过高"
        )
        resumed = await orchestrator.resume_task(task.task_id)
        assert resumed.status == TaskStatus.FAILED

        result = await seeded_session.execute(
            select(DiscountORM).where(DiscountORM.customer_id == "C001")
        )
        assert result.scalars().first() is None

    async def test_four_eyes_principle(self, orchestrator, seeded_session) -> None:
        """审批人不能是发起人。"""
        import pytest

        from app.control.approval_gate import ApprovalGate
        from app.core.errors import PermissionDeniedError

        await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九折。",
        )
        repo = ApprovalRepository(seeded_session)
        approval = (await repo.list_approvals())[0]
        with pytest.raises(PermissionDeniedError, match="四眼原则"):
            await ApprovalGate(repo).decide(
                approval.approval_id, approved=True, approver_id="user_001"
            )

    async def test_non_manager_cannot_approve(self, orchestrator, seeded_session) -> None:
        import pytest

        from app.control.approval_gate import ApprovalGate
        from app.core.errors import PermissionDeniedError

        await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九折。",
        )
        repo = ApprovalRepository(seeded_session)
        approval = (await repo.list_approvals())[0]
        # user_002 不存在 → 零权限 → 无审批资格
        with pytest.raises(PermissionDeniedError):
            await ApprovalGate(repo).decide(
                approval.approval_id, approved=True, approver_id="user_002"
            )

    async def test_approval_decision_is_idempotent(self, orchestrator, seeded_session) -> None:
        """重复投递的审批回调不应产生额外效果。"""
        from app.control.approval_gate import ApprovalGate

        await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九折。",
        )
        repo = ApprovalRepository(seeded_session)
        gate = ApprovalGate(repo)
        approval = (await repo.list_approvals())[0]
        first = await gate.decide(approval.approval_id, approved=True, approver_id="manager_001")
        second = await gate.decide(approval.approval_id, approved=False, approver_id="manager_001")
        assert first.status == ApprovalStatus.APPROVED
        assert second.status == ApprovalStatus.APPROVED  # 第二次不改变结果


class TestScenario3Deny:
    """场景三：30% 折扣 → DENY → 工具绝不执行。"""

    async def test_thirty_percent_denied(self, orchestrator, seeded_session) -> None:
        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打七折。",
        )
        assert task.status == TaskStatus.FAILED
        assert task.error_code == "POLICY_DENIED"

        # 明确的拒绝原因
        assert "超过" in task.error_message

        # **工具没有被执行**
        result = await seeded_session.execute(
            select(DiscountORM).where(DiscountORM.customer_id == "C001")
        )
        assert result.scalars().first() is None

        # 审计里能看到 DENY 的具体原因和是哪条策略拒的
        events = await AuditRepository(seeded_session).list_by_task(task.task_id)
        decisions = [e for e in events if e.event_type == "POLICY_DECISION"]
        deny = [d for d in decisions if d.payload["decision"] == "DENY"]
        assert deny
        assert deny[0].payload["reason_code"] == "DISCOUNT_EXCEEDS_ABSOLUTE_LIMIT"
        assert any(
            ev["policy"] == "BusinessRulePolicy" and ev["decision"] == "DENY"
            for ev in deny[0].payload["evaluations"]
        )

    async def test_policy_version_recorded(self, orchestrator, seeded_session, settings) -> None:
        """每条裁决都要记录策略版本，否则半年后无法解释当时为什么这么判。"""
        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent", message="给客户 C001 打七折。",
        )
        events = await AuditRepository(seeded_session).list_by_task(task.task_id)
        decisions = [e for e in events if e.event_type == "POLICY_DECISION"]
        assert all(d.payload["policy_version"] == settings.policy_version for d in decisions)


class TestScenario7InsufficientPermission:
    """场景七：模型提出当前 Agent 无权使用的工具。"""

    async def test_refund_request_is_denied(self, orchestrator, seeded_session) -> None:
        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="请给客户 C001 办理退款。",
        )
        assert task.status == TaskStatus.FAILED
        assert task.error_code == "POLICY_DENIED"

        events = await AuditRepository(seeded_session).list_by_task(task.task_id)
        decisions = [
            e for e in events
            if e.event_type == "POLICY_DECISION" and e.payload["decision"] == "DENY"
        ]
        assert decisions
        # 未注册工具在 AgentPermissionPolicy 就被挡下
        assert decisions[0].payload["reason_code"] in (
            "TOOL_NOT_REGISTERED", "TOOL_NOT_IN_AGENT_WHITELIST",
        )

    async def test_readonly_agent_cannot_apply_discount(self, orchestrator, seeded_session) -> None:
        """即使用管理员身份，只读 Agent 也不能发折扣。"""
        task = await orchestrator.start_task(
            user_id="admin_001", agent_id="readonly_agent",
            message="给客户 C001 打九五折。",
        )
        assert task.status == TaskStatus.FAILED
        result = await seeded_session.execute(
            select(DiscountORM).where(DiscountORM.customer_id == "C001")
        )
        assert result.scalars().first() is None

    async def test_cross_department_data_denied(self, orchestrator, seeded_session) -> None:
        """C003 属于 cs_south，user_001 属于 cs_north。"""
        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C003 打九五折。",
        )
        assert task.status == TaskStatus.FAILED
        result = await seeded_session.execute(
            select(DiscountORM).where(DiscountORM.customer_id == "C003")
        )
        assert result.scalars().first() is None
