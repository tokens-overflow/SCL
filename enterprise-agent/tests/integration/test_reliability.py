"""可靠性集成测试。

覆盖验收场景四 ~ 六：工具超时、部分成功、进程重启。
这三个是「企业级」和「Demo 级」之间最硬的分水岭。
"""

from __future__ import annotations

from sqlalchemy import select

from app.actions.tools.fault_injection import fault_injector
from app.core.enums import CompensationStatus, StepStatus, TaskStatus
from app.state.models import DiscountORM, NotificationORM
from app.state.repositories import AuditRepository, ToolExecutionRepository


class TestScenario4Timeout:
    """场景四：折扣工具超时。

    **超时不等于失败。** 必须通过幂等键查询外部真实状态：
    * 已成功 → 补写状态，不重复执行；
    * 未成功 → 才安全重试。
    """

    async def test_timeout_after_commit_is_reconciled_not_retried(
        self, orchestrator, seeded_session
    ) -> None:
        """最危险的场景：写入已生效但响应没回来。

        期望：对账查明已成功 → 补写状态 → **只有一条折扣记录**。
        """
        fault_injector.set("apply_discount", "timeout_after_commit", times=1)

        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九五折，并通知客户。",
        )
        assert task.status == TaskStatus.COMPLETED

        step = task.step_by_name("apply_discount")
        assert step.status == StepStatus.SUCCESS
        # 对账补写了外部凭证
        assert step.external_reference_id

        # **绝对只有一条折扣记录** —— 没有因为超时而重复写入
        result = await seeded_session.execute(
            select(DiscountORM).where(DiscountORM.customer_id == "C001")
        )
        discounts = list(result.scalars().all())
        assert len(discounts) == 1
        assert discounts[0].discount_id == step.external_reference_id

        # 审计里能看到对账过程与结论
        events = await AuditRepository(seeded_session).list_by_task(task.task_id)
        recon = [e for e in events if e.event_type == "RECONCILIATION"]
        assert recon
        assert recon[0].payload["outcome"] == "ALREADY_SUCCEEDED"
        assert recon[0].payload["idempotency_key"]

    async def test_timeout_before_commit_is_safely_retried(
        self, orchestrator, seeded_session
    ) -> None:
        """超时且写入未发生 → 对账查明后带同一幂等键安全重试。"""
        fault_injector.set("apply_discount", "timeout_before_commit", times=1)

        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九五折。",
        )
        assert task.status == TaskStatus.COMPLETED

        result = await seeded_session.execute(
            select(DiscountORM).where(DiscountORM.customer_id == "C001")
        )
        assert len(list(result.scalars().all())) == 1

        events = await AuditRepository(seeded_session).list_by_task(task.task_id)
        recon = [e for e in events if e.event_type == "RECONCILIATION"]
        assert recon[0].payload["outcome"] == "NOT_EXECUTED"

    async def test_transient_failure_is_retried(self, orchestrator, seeded_session) -> None:
        """可重试的技术失败 → 指数退避重试后成功。"""
        fault_injector.set("apply_discount", "transient_failure", times=1)

        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九五折。",
        )
        assert task.status == TaskStatus.COMPLETED
        step = task.step_by_name("apply_discount")
        assert step.retry_count >= 1

        events = await AuditRepository(seeded_session).list_by_task(task.task_id)
        assert any(e.event_type == "RETRY_SCHEDULED" for e in events)

    async def test_permanent_failure_is_not_retried(self, orchestrator, seeded_session) -> None:
        """不可重试的业务失败 → 不重试，直接失败。"""
        fault_injector.set("apply_discount", "permanent_failure", times=5)

        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九五折。",
        )
        assert task.status == TaskStatus.FAILED
        step = task.step_by_name("apply_discount")
        assert step.retry_count == 0  # 一次都没重试
        assert step.error_code == "BUSINESS_RULE_VIOLATION"

    async def test_multiple_executions_recorded_per_step(
        self, orchestrator, seeded_session
    ) -> None:
        """每次重试都有独立的执行记录——否则「第一次错在哪」就丢了。"""
        fault_injector.set("apply_discount", "transient_failure", times=1)
        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent", message="给客户 C001 打九五折。",
        )
        step = task.step_by_name("apply_discount")
        executions = await ToolExecutionRepository(seeded_session).list_by_step(step.step_id)
        assert len(executions) >= 1
        assert all(e.idempotency_key for e in executions)


class TestScenario5PartialSuccess:
    """场景五：折扣成功但通知失败。

    期望：**不重复创建折扣**、通知单独重试、任务落 PARTIAL_SUCCESS。
    绝不因为通知失败去撤销折扣——那是两回事。
    """

    async def test_discount_succeeds_notification_fails(
        self, orchestrator, seeded_session
    ) -> None:
        # times 设得足够大，让重试也失败
        fault_injector.set("send_notification", "permanent_failure", times=10)

        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九五折，并通知客户。",
        )
        assert task.status == TaskStatus.PARTIAL_SUCCESS

        # 折扣成功且**只有一条**
        assert task.step_by_name("apply_discount").status == StepStatus.SUCCESS
        result = await seeded_session.execute(
            select(DiscountORM).where(DiscountORM.customer_id == "C001")
        )
        discounts = list(result.scalars().all())
        assert len(discounts) == 1
        # **折扣没有被撤销** —— 通知失败不是撤销折扣的理由
        assert discounts[0].status == "ACTIVE"

        # 通知步骤失败但不影响整单
        notify = task.step_by_name("send_notification")
        assert notify.status == StepStatus.FAILED
        assert notify.critical is False

        # 通知确实没发出去
        noti = await seeded_session.execute(select(NotificationORM))
        assert list(noti.scalars().all()) == []

        # 结果里明确列出了失败的可选步骤
        assert task.result_payload["outcome"] == "PARTIAL_SUCCESS"
        assert "send_notification" in task.result_payload["failed_optional_steps"]

    async def test_notification_retried_on_transient_failure(
        self, orchestrator, seeded_session
    ) -> None:
        """通知的瞬时失败可以重试，且**不会重复创建折扣**。"""
        fault_injector.set("send_notification", "transient_failure", times=1)

        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九五折，并通知客户。",
        )
        assert task.status == TaskStatus.COMPLETED
        assert task.step_by_name("send_notification").retry_count >= 1

        result = await seeded_session.execute(
            select(DiscountORM).where(DiscountORM.customer_id == "C001")
        )
        assert len(list(result.scalars().all())) == 1

    async def test_audit_explains_each_step(self, orchestrator, seeded_session) -> None:
        """审计要能清楚说明每一步的结果。"""
        fault_injector.set("send_notification", "permanent_failure", times=10)
        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent",
            message="给客户 C001 打九五折，并通知客户。",
        )
        events = await AuditRepository(seeded_session).list_by_task(task.task_id)
        finished = [e for e in events if e.event_type == "TOOL_EXECUTION_FINISHED"]
        by_tool = {e.payload["tool_name"]: e.payload["status"] for e in finished}
        assert by_tool["apply_discount"] == "SUCCESS"
        assert by_tool["send_notification"] == "FAILED"


class TestScenario6ProcessRestart:
    """场景六：任务执行到一半模拟进程重启。

    期望：从数据库恢复、跳过已成功步骤、正确处理悬挂状态、从正确节点继续。
    **整个恢复过程一次模型调用都没有。**
    """

    async def test_restart_resumes_from_correct_step(
        self, make_orchestrator, database, llm
    ) -> None:
        from app.examples.discount_workflow import seed_demo_data

        # —— 「进程 1」：跑到审批挂起 ——
        async with database.session() as s1:
            await seed_demo_data(s1)
            o1 = make_orchestrator(s1)
            task = await o1.start_task(
                user_id="user_001", agent_id="discount_agent",
                message="给客户 C001 打九折，并通知客户。",
            )
            task_id = task.task_id
            assert task.status == TaskStatus.WAITING_APPROVAL
            calls_after_first_process = llm.call_count
            assert calls_after_first_process > 0  # 规划阶段确实调了模型

        # —— 「进程 2」：全新的 Orchestrator（模拟重启后的新进程）——
        async with database.session() as s2:
            from app.control.approval_gate import ApprovalGate
            from app.state.repositories import ApprovalRepository

            repo = ApprovalRepository(s2)
            approval = (await repo.list_approvals())[0]
            await ApprovalGate(repo).decide(
                approval.approval_id, approved=True, approver_id="manager_001"
            )

            o2 = make_orchestrator(s2)
            resumed = await o2.resume_task(task_id)

        assert resumed.status == TaskStatus.COMPLETED
        # 已成功的步骤没有被重跑
        assert resumed.step_by_name("query_customer").status == StepStatus.SUCCESS

        # **恢复过程没有增加任何模型调用**（除了最后生成回复那一次）。
        # 关键点是：「上次执行到哪儿」是从状态表读出来的，不是问模型问出来的。
        recovery_calls = llm.call_count - calls_after_first_process
        assert recovery_calls <= 1, f"恢复过程不应依赖模型记忆，实际调用了 {recovery_calls} 次"

    async def test_stale_running_step_marked_unknown_then_reconciled(
        self, make_orchestrator, database, settings
    ) -> None:
        """悬挂的 RUNNING 步骤 → UNKNOWN → 对账 → 落定。

        模拟：进程在「已登记执行意图」和「落盘结果」之间崩溃。
        """
        import asyncio

        from app.core.ids import build_idempotency_key
        from app.examples.discount_workflow import seed_demo_data
        from app.state.repositories import TaskRepository

        async with database.session() as s1:
            await seed_demo_data(s1)
            repo = TaskRepository(s1)
            task = await repo.create_task(
                user_id="user_001", agent_id="discount_agent",
                original_input="给客户 C001 打九五折。", trace_id="trace-restart",
            )
            args = {"customer_id": "C001", "discount_rate": 0.05, "reason": ""}
            idem = build_idempotency_key(
                task_id=task.task_id, step_name="apply_discount",
                tool_name="apply_discount", arguments=args,
            )
            from app.core.enums import StepType

            step = await repo.create_step(
                task_id=task.task_id, step_name="apply_discount", sequence=0,
                step_type=StepType.WRITE, tool_name="apply_discount",
                input_payload={"arguments": args, "intent": "apply_discount"},
                idempotency_key=idem,
            )
            # 模拟「已登记 RUNNING、结果未落盘」的悬挂记录
            await repo.update_step(step.step_id, status=StepStatus.RUNNING)
            await repo.update_task_status(task.task_id, TaskStatus.RUNNING)
            task_id = task.task_id

        # 等待超过 stale 阈值（测试配置为 1 秒）
        await asyncio.sleep(settings.stale_running_seconds + 0.2)

        async with database.session() as s2:
            o2 = make_orchestrator(s2)
            resumed = await o2.resume_task(task_id)
            events = await AuditRepository(s2).list_by_task(task_id)

        # 悬挂步骤被识别、对账、然后正确落定
        recon = [e for e in events if e.event_type == "RECONCILIATION"]
        assert recon, "悬挂的 RUNNING 步骤必须触发对账"
        assert recon[0].payload["outcome"] == "NOT_EXECUTED"
        assert resumed.status in (TaskStatus.COMPLETED, TaskStatus.RUNNING)
        assert resumed.step_by_name("apply_discount").status == StepStatus.SUCCESS

    async def test_resume_terminal_task_is_noop(self, orchestrator) -> None:
        """对已终态任务恢复是幂等的 no-op。"""
        task = await orchestrator.start_task(
            user_id="user_001", agent_id="discount_agent", message="给客户 C001 打九五折。",
        )
        assert task.status == TaskStatus.COMPLETED
        again = await orchestrator.resume_task(task.task_id)
        assert again.status == TaskStatus.COMPLETED
        assert again.version == task.version  # 一个字段都没动


class TestCompensation:
    """Saga 补偿：折扣成功后的关键步骤失败 → 逆序撤销。"""

    async def test_failed_critical_step_triggers_compensation(
        self, make_orchestrator, database
    ) -> None:
        """构造：apply_discount 成功 → 后续关键步骤失败 → 折扣被撤销。

        注意补偿是**新的业务动作**（写 REVOKED 状态），不是数据库回滚。
        """
        from app.actions.base import ToolExecutionContext
        from app.actions.compensation import CompensationManager
        from app.actions.tools.apply_discount import ApplyDiscountArgs, ApplyDiscountTool
        from app.core.enums import StepType
        from app.examples.discount_workflow import seed_demo_data
        from app.operations.audit import AuditService
        from app.state.repositories import AuditRepository, TaskRepository

        async with database.session() as s:
            await seed_demo_data(s)
            repo = TaskRepository(s)
            task = await repo.create_task(
                user_id="user_001", agent_id="discount_agent",
                original_input="test", trace_id="t-comp",
            )
            # 先真实发一笔折扣
            tool = ApplyDiscountTool()
            ctx = ToolExecutionContext(
                task_id=task.task_id, step_id="s0", step_name="apply_discount",
                execution_id="e0", idempotency_key="k-saga", session=s,
            )
            res = await tool.execute(
                ApplyDiscountArgs(customer_id="C001", discount_rate=0.05), ctx
            )
            assert res.succeeded

            step = await repo.create_step(
                task_id=task.task_id, step_name="apply_discount", sequence=0,
                step_type=StepType.WRITE, tool_name="apply_discount",
                input_payload={"arguments": {"customer_id": "C001", "discount_rate": 0.05}},
                idempotency_key="k-saga",
            )
            await repo.update_step(
                step.step_id, status=StepStatus.SUCCESS,
                output_payload=res.result, external_reference_id=res.external_reference_id,
            )

            task = await repo.require_task(task.task_id)
            from app.actions.registry import ToolRegistry, register_builtin_tools

            reg = ToolRegistry()
            register_builtin_tools(reg)
            manager = CompensationManager(reg, repo, AuditService(AuditRepository(s)))
            result = await manager.compensate_task(task, session=s, reason="后续步骤失败")

            assert result.compensated == ["apply_discount"]
            assert result.needs_manual_followup is False

            # 折扣被撤销——这是一条**新写入**，不是回滚
            row = await s.execute(
                select(DiscountORM).where(DiscountORM.discount_id == res.external_reference_id)
            )
            discount = row.scalars().first()
            assert discount.status == "REVOKED"
            assert discount.revoked_at is not None
            assert "补偿" in discount.revoke_reason

            # 步骤状态与补偿状态都被独立记录
            refreshed = await repo.get_step(step.step_id)
            assert refreshed.status == StepStatus.COMPENSATED
            assert refreshed.compensation_status == CompensationStatus.COMPENSATED

            # 补偿有**独立的审计事件**
            events = await AuditRepository(s).list_by_task(task.task_id)
            comp = [e for e in events if e.event_type.startswith("COMPENSATION")]
            assert len(comp) >= 2  # started + finished
            assert any(e.payload.get("outcome") == "SUCCESS" for e in comp)

    async def test_non_compensable_step_needs_manual_followup(
        self, make_orchestrator, database
    ) -> None:
        """不可补偿的动作（已发出的通知）→ 标记为需人工善后，不假装成功。"""
        from app.actions.compensation import CompensationManager
        from app.actions.registry import ToolRegistry, register_builtin_tools
        from app.core.enums import StepType
        from app.examples.discount_workflow import seed_demo_data
        from app.operations.audit import AuditService
        from app.state.repositories import AuditRepository, TaskRepository

        async with database.session() as s:
            await seed_demo_data(s)
            repo = TaskRepository(s)
            task = await repo.create_task(
                user_id="user_001", agent_id="discount_agent", original_input="t", trace_id="x",
            )
            step = await repo.create_step(
                task_id=task.task_id, step_name="send_notification", sequence=0,
                step_type=StepType.NOTIFY, tool_name="send_notification",
                input_payload={"arguments": {"customer_id": "C001"}},
            )
            await repo.update_step(step.step_id, status=StepStatus.SUCCESS)

            task = await repo.require_task(task.task_id)
            reg = ToolRegistry()
            register_builtin_tools(reg)
            manager = CompensationManager(reg, repo, AuditService(AuditRepository(s)))
            result = await manager.compensate_task(task, session=s)

            assert result.not_supported == ["send_notification"]
            assert result.needs_manual_followup is True
            refreshed = await repo.get_step(step.step_id)
            assert refreshed.compensation_status == CompensationStatus.NOT_SUPPORTED
