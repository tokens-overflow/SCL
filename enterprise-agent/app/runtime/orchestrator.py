"""Orchestrator：流程主控。

**为什么 Runtime 才是流程主控，而不是 LLM？**

讲 Agent 的文章大多把篇幅给了 LLM、Context 和 Tools，因为它们最像「AI」。
但真正决定系统能不能上线的是 Runtime——**它是那个在凌晨三点
把挂掉的任务捡起来接着跑的东西。**

具体来说，下面这些决策全部由本模块（程序）做出，一次模型调用都没有：

* 该执行哪一步（从状态表推导，不问模型「上次到哪了」）
* 放行 / 拒绝 / 审批 / 重试（控制层裁决）
* 超时了要不要重试（先对账）
* 失败了要不要补偿（业务规则）
* 任务最终落哪个终态

模型只在两个地方出现：**解析意图**和**把结果写成人话**。
其余全是程序。这不是为了省钱（虽然确实省），
而是因为这些问题都有唯一正确答案，而且出错要有人负责。

主流程：

    用户请求 → 创建 Task → 构建 Context → LLM 解析意图 → 生成计划
      → 登记步骤（PENDING）→ 逐步推进：
          构造 ActionProposal → Pydantic 校验 → 控制层裁决
            ├── DENY            → 拒绝并返回原因
            ├── REQUIRE_APPROVAL → 创建审批单，任务挂起
            ├── MANUAL_REVIEW    → 转人工
            └── ALLOW            → 执行工具 → 落盘结果
                  ├── 成功   → 下一步
                  ├── 超时   → 对账（不重试！）
                  ├── 可重试 → 安排退避重试
                  └── 致命   → 补偿 / 终止
      → 汇总结果 → LLM 生成回复 → 审计 / 日志 / 指标 / Trace
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select

from app.actions.compensation import CompensationManager
from app.actions.executor import ActionExecutor
from app.actions.registry import ToolRegistry
from app.cognitive.context_builder import ContextBuilder
from app.cognitive.intent_parser import IntentParser
from app.cognitive.models import ActionProposal, ExecutionPlan
from app.cognitive.planner import Planner
from app.cognitive.reflection import ReplyComposer
from app.context.models import StepSummary
from app.control.approval_gate import ApprovalGate
from app.control.authorization import AuthorizationService
from app.control.models import PolicyDecision, PolicyEvaluationRequest
from app.control.policy_engine import PolicyEngine
from app.core.config import Settings, get_settings
from app.core.enums import (
    ActorType,
    ApprovalStatus,
    AuditEventType,
    CompensationStatus,
    DecisionType,
    ErrorCode,
    RiskLevel,
    StepEvent,
    StepStatus,
    StepType,
    TaskEvent,
    TaskStatus,
    ToolExecutionStatus,
)
from app.core.errors import AgentError, IllegalStateTransitionError
from app.core.ids import build_idempotency_key, utcnow
from app.llm.base import LLMProvider
from app.operations.audit import AuditService
from app.operations.logging import bind_context, get_logger, reset_context
from app.operations.metrics import metrics
from app.operations.tracing import ensure_trace_id, tracer
from app.runtime.events import (
    APPROVAL_REQUESTED,
    STEP_RETRY_SCHEDULED,
    STEP_SUCCEEDED,
    TASK_COMPLETED,
    TASK_CREATED,
    TASK_FAILED,
    TASK_PARTIAL_SUCCESS,
    TASK_WAITING_APPROVAL,
    DomainEvent,
    EventBus,
    event_bus,
)
from app.runtime.models import AgentTask, TaskStep
from app.runtime.retry import RetryPolicy
from app.runtime.state_machine import step_state_machine, task_state_machine
from app.state.models import CustomerORM, DiscountORM
from app.state.repositories import (
    ApprovalRepository,
    AuditRepository,
    CheckpointRepository,
    TaskRepository,
    ToolExecutionRepository,
)

logger = get_logger(__name__)


class Orchestrator:
    """任务编排器。

    Args:
        session: 数据库会话。**一个 Orchestrator 实例绑定一个会话**，
            所以它是「每次请求新建」的，不是单例。
        registry: 工具注册表。
        policy_engine: 策略引擎。
        llm: LLM Provider。
        settings: 配置对象。
        bus: 事件总线。
    """

    def __init__(
        self,
        *,
        session: Any,
        registry: ToolRegistry,
        policy_engine: PolicyEngine,
        llm: LLMProvider,
        settings: Settings | None = None,
        bus: EventBus | None = None,
        authorization: AuthorizationService | None = None,
    ) -> None:
        self.session = session
        self.registry = registry
        self.policy_engine = policy_engine
        self.llm = llm
        self.settings = settings or get_settings()
        self.bus = bus or event_bus

        self.task_repo = TaskRepository(session)
        self.execution_repo = ToolExecutionRepository(session)
        self.approval_repo = ApprovalRepository(session)
        self.audit = AuditService(AuditRepository(session))
        self.checkpoints = CheckpointRepository(session)

        self.authorization = authorization or AuthorizationService()
        self.executor = ActionExecutor(registry, self.execution_repo)
        self.compensation = CompensationManager(registry, self.task_repo, self.audit)
        self.approval_gate = ApprovalGate(
            self.approval_repo, authorization=self.authorization, settings=self.settings
        )
        self.retry_policy = RetryPolicy(self.settings)
        self.context_builder = ContextBuilder(registry, settings=self.settings)
        self.intent_parser = IntentParser(llm)
        self.planner = Planner(llm, registry)
        self.reply_composer = ReplyComposer(llm)

    # ==================================================================
    # 对外入口
    # ==================================================================
    async def start_task(
        self,
        *,
        user_id: str,
        agent_id: str,
        message: str,
        trace_id: str | None = None,
        task_id: str | None = None,
    ) -> AgentTask:
        """创建并推进一个新任务。

        Args:
            user_id: 发起人。
            agent_id: 目标 Agent。
            message: 用户自然语言输入。
            trace_id: 上游传来的链路 ID（**应该复用**，
                这样 Agent 链路能和业务系统链路串起来）。
            task_id: 可指定任务 ID（测试用）。

        Returns:
            推进之后的任务状态。可能是 COMPLETED / WAITING_APPROVAL /
            FAILED / PARTIAL_SUCCESS / MANUAL_REVIEW 中的任意一个。
        """
        trace_id = ensure_trace_id(trace_id)
        started_at = time.perf_counter()
        previous_ctx = bind_context(user_id=user_id, agent_id=agent_id, trace_id=trace_id)

        try:
            task = await self.task_repo.create_task(
                user_id=user_id,
                agent_id=agent_id,
                original_input=message,
                trace_id=trace_id,
                task_id=task_id,
            )
            bind_context(task_id=task.task_id)

            await self.audit.record(
                AuditEventType.REQUEST_RECEIVED,
                actor_type=ActorType.USER,
                actor_id=user_id,
                task_id=task.task_id,
                trace_id=trace_id,
                # 原始输入会经过 mask 后落审计；原文另存在 tasks 表。
                payload={"message": message, "agent_id": agent_id},
            )
            await self.audit.task_created(task.task_id, user_id, agent_id, trace_id)
            await self.bus.publish(
                DomainEvent(name=TASK_CREATED, task_id=task.task_id, trace_id=trace_id)
            )
            metrics.increment("agent_tasks_started_total", agent=agent_id)

            task = await self._plan_task(task, message)
            if task.is_terminal():
                return task

            result = await self._drive(task)
            metrics.observe(
                "agent_task_duration_seconds", time.perf_counter() - started_at
            )
            return result
        finally:
            reset_context(previous_ctx)

    async def resume_task(self, task_id: str) -> AgentTask:
        """从断点恢复任务。

        **整个方法里没有一次模型调用。**

        「上次执行到哪儿」不需要问大模型——程序从状态表就能确定，
        而且比模型可靠得多。模型的「记忆」是上下文窗口里的文本，
        它会被截断、会过期、会因为一次对话重置而清零；
        状态表不会。

        恢复算法：

        1. 按 task_id 读出任务与全部步骤；
        2. 已 SUCCESS 的步骤**直接跳过**，不重做也不重问模型；
        3. RUNNING 但长时间无更新 → 标记 UNKNOWN，进对账；
        4. TIMEOUT / UNKNOWN → **先对账**，查明真相再落定；
        5. WAITING_APPROVAL → 检查审批结果，没批就继续等；
        6. FAILED 且可重试 → 安排重试；
        7. FAILED 且不可重试 → 补偿 或 FAILED / MANUAL_REVIEW；
        8. 从正确的步骤继续。

        Args:
            task_id: 任务 ID。

        Returns:
            恢复推进后的任务。

        Raises:
            TaskNotFoundError: 任务不存在。
        """
        task = await self.task_repo.require_task(task_id)
        previous_ctx = bind_context(
            task_id=task.task_id,
            trace_id=task.trace_id,
            user_id=task.user_id,
            agent_id=task.agent_id,
        )
        try:
            await self.audit.record(
                AuditEventType.TASK_RESUMED,
                actor_type=ActorType.SYSTEM,
                actor_id="orchestrator",
                task_id=task.task_id,
                trace_id=task.trace_id,
                payload={"from_status": str(task.status), "step_count": len(task.steps)},
            )

            if task.is_terminal():
                logger.info("resume_skipped_terminal", task_id=task_id, status=str(task.status))
                return task

            # —— 第 1 步：处理未决状态（对账 / 审批 / 悬挂）——
            task = await self._settle_unresolved_steps(task)
            task = await self._settle_approvals(task)

            if task.is_terminal():
                return task

            # —— 第 2 步：把任务状态拉回 RUNNING（如果允许）——
            if task.status in (TaskStatus.WAITING_APPROVAL,):
                # 审批还没结果，继续等。**不往下走。**
                return task
            if task.status in (TaskStatus.RETRYING, TaskStatus.CREATED, TaskStatus.PLANNING):
                task = await self._transition_task(task, TaskEvent.RESUME, fallback=TaskStatus.RUNNING)

            metrics.increment("agent_tasks_resumed_total")
            return await self._drive(task)
        finally:
            reset_context(previous_ctx)

    async def cancel_task(self, task_id: str, *, actor_id: str, reason: str = "") -> AgentTask:
        """取消任务。

        Note:
            取消**不会自动撤销已生效的副作用**。
            「是否撤销折扣」必须由明确业务规则或人工决定，
            不能因为一句「取消」就自动回滚一笔已经对客户生效的业务。
            如需撤销，应该显式发起补偿。
        """
        task = await self.task_repo.require_task(task_id)
        if task.is_terminal():
            return task
        task = await self._transition_task(task, TaskEvent.CANCEL)
        await self.audit.record(
            AuditEventType.TASK_CANCELLED,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            task_id=task_id,
            trace_id=task.trace_id,
            payload={
                "reason": reason,
                "note": "取消不自动撤销已生效副作用，如需撤销请显式发起补偿",
                "side_effect_steps": [s.step_name for s in task.completed_side_effect_steps()],
            },
        )
        return task

    # ==================================================================
    # 规划阶段（唯一大量使用 LLM 的地方）
    # ==================================================================
    async def _plan_task(self, task: AgentTask, message: str) -> AgentTask:
        """构建上下文 → 解析意图 → 生成计划 → 登记步骤。"""
        task = await self._transition_task(task, TaskEvent.START_PLANNING)

        identity = await self.authorization.resolve(task.user_id, task.agent_id)
        context = await self.context_builder.build(
            task_id=task.task_id,
            trace_id=task.trace_id,
            identity=identity,
            user_input=message,
            task_status=str(task.status),
            session=self.session,
        )
        await self.audit.record(
            AuditEventType.CONTEXT_BUILT,
            actor_type=ActorType.SYSTEM,
            actor_id="context_builder",
            task_id=task.task_id,
            trace_id=task.trace_id,
            payload=context.prompt_snapshot(),
        )

        with tracer.span("cognitive.plan", trace_id=task.trace_id, task_id=task.task_id):
            await self.audit.llm_call(
                task_id=task.task_id, trace_id=task.trace_id, started=True, provider=self.llm.name
            )
            try:
                intent = await self.intent_parser.parse(context)
                plan = await self.planner.plan(context, intent)
            except AgentError as exc:
                # 模型无法给出可用结构 → 转人工，而不是猜一个动作。
                await self.audit.llm_call(
                    task_id=task.task_id,
                    trace_id=task.trace_id,
                    started=False,
                    provider=self.llm.name,
                    error=exc.message,
                )
                return await self._finish_manual_review(
                    task, reason=f"意图解析失败：{exc.message}", error_code=exc.error_code
                )

            record = self.llm.last_call_record()
            await self.audit.llm_call(
                task_id=task.task_id,
                trace_id=task.trace_id,
                started=False,
                provider=self.llm.name,
                model=record.model if record else "",
                prompt_snapshot=record.prompt_snapshot if record else {},
                usage=record.usage.model_dump() if record else {},
            )
            if record:
                metrics.record_cost(
                    task.task_id,
                    tokens_in=record.usage.prompt_tokens,
                    tokens_out=record.usage.completion_tokens,
                )

        # 信息不足以形成动作 → 转人工追问，不要凭空补默认值。
        if intent.clarification_needed or not plan.steps:
            return await self._finish_manual_review(
                task,
                reason=(
                    "用户输入信息不足以形成明确动作，需人工补充"
                    if intent.clarification_needed
                    else "未能生成任何可执行步骤"
                ),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )

        await self.audit.proposal_generated(
            task_id=task.task_id,
            trace_id=task.trace_id,
            proposals=[
                {
                    "step_name": s.step_name,
                    "tool_name": s.proposal.tool_name,
                    "intent": s.proposal.intent,
                    "arguments": s.proposal.arguments,
                    # 只记录简洁的决策说明，不记录模型的私有思维链。
                    "reasoning_summary": s.proposal.reasoning_summary,
                    "confidence": s.proposal.confidence,
                    "risk_hint": str(s.proposal.risk_hint),
                }
                for s in plan.steps
            ],
        )

        await self._register_steps(task, plan)
        task = await self.task_repo.update_task_status(
            task.task_id,
            TaskStatus.PLANNING,
            current_step=plan.steps[0].step_name,
        )
        task = await self._transition_task(task, TaskEvent.PLAN_READY)
        # 任务类型来自意图解析，用于后续的编排选择与统计。
        return await self.task_repo.require_task(task.task_id)

    async def _register_steps(self, task: AgentTask, plan: ExecutionPlan) -> None:
        """把计划登记成步骤记录。

        **步骤必须在执行前登记。** 如果执行和登记之间进程崩溃，
        这次执行就成了无人知晓的孤儿：外部系统那边已经生效，
        我们的库里查无此事。
        """
        for idx, planned in enumerate(plan.steps):
            tool_name = planned.proposal.tool_name
            step_type = StepType.COMPUTE
            max_retries = self.settings.max_retries
            if self.registry.has(tool_name):
                step_type = self.registry.get(tool_name).step_type

            # 幂等键在**登记时**就算出来并落库。
            # 这样即使这一步还没执行就崩了，恢复时也知道该拿哪个键去对账。
            idem_key = build_idempotency_key(
                task_id=task.task_id,
                step_name=planned.step_name,
                tool_name=tool_name,
                arguments=planned.proposal.arguments,
            )
            await self.task_repo.create_step(
                task_id=task.task_id,
                step_name=planned.step_name,
                sequence=idx,
                step_type=step_type,
                tool_name=tool_name,
                input_payload={
                    "arguments": planned.proposal.arguments,
                    "intent": planned.proposal.intent,
                    "reasoning_summary": planned.proposal.reasoning_summary,
                    "confidence": planned.proposal.confidence,
                    "risk_hint": str(planned.proposal.risk_hint),
                    "requested_by": planned.proposal.requested_by,
                },
                max_retries=max_retries,
                critical=planned.critical,
                depends_on=planned.depends_on,
                idempotency_key=idem_key,
            )

    # ==================================================================
    # 推进循环
    # ==================================================================
    async def _drive(self, task: AgentTask, max_iterations: int = 50) -> AgentTask:
        """逐步推进任务直到终态或需要外部输入。

        Args:
            task: 任务。
            max_iterations: 循环上限。**必须有上限**——
                一个写错的条件分支能让 Agent 无限循环烧钱，
                而这类 Bug 往往只在特定输入下才触发。

        Returns:
            推进之后的任务。
        """
        for _ in range(max_iterations):
            task = await self.task_repo.require_task(task.task_id)
            if task.is_terminal():
                return task

            step = task.next_actionable_step()
            if step is None:
                # 没有可推进的步骤了。但要区分两种情况：
                # * 有步骤卡在审批 / 执行中 → 挂起等待，**不能收尾**；
                # * 确实全部处理完了 → 收尾定终态。
                if task.has_blocking_steps():
                    return task
                return await self._finalize(task)

            outcome = await self._advance_step(task, step)
            if outcome in ("halt", "terminal"):
                return await self.task_repo.require_task(task.task_id)

        # 达到循环上限：不是失败，是「程序判断不了」，转人工。
        logger.error("orchestrator_max_iterations", task_id=task.task_id)
        return await self._finish_manual_review(
            task, reason="推进循环达到上限，可能存在步骤依赖死锁", error_code=ErrorCode.INTERNAL_ERROR
        )

    async def _advance_step(self, task: AgentTask, step: TaskStep) -> str:
        """推进单个步骤。

        Returns:
            ``"continue"`` 继续推进下一步；
            ``"halt"`` 需要外部输入（审批 / 重试等待）；
            ``"terminal"`` 任务已落终态。
        """
        bind_context(step_id=step.step_id, tool_name=step.tool_name)

        # —— 未决状态优先处理：先对账，不能直接执行 ——
        if step.is_unresolved():
            return await self._reconcile_step(task, step)

        # —— 构造 ActionProposal（来自登记时保存的输入快照）——
        proposal = self._proposal_from_step(step)

        # —— 控制层裁决 ——
        decision = await self._evaluate(task, step, proposal)
        await self.audit.policy_decision(
            task_id=task.task_id,
            step_id=step.step_id,
            trace_id=task.trace_id,
            decision_payload=decision.audit_payload(),
        )

        if decision.decision == DecisionType.DENY:
            return await self._handle_denied(task, step, decision)
        if decision.decision == DecisionType.MANUAL_REVIEW:
            await self._fail_step(step, ErrorCode.POLICY_DENIED, decision.human_readable_reason)
            await self._finish_manual_review(
                task, reason=decision.human_readable_reason, error_code=ErrorCode.POLICY_DENIED
            )
            return "terminal"
        if decision.decision == DecisionType.REQUIRE_APPROVAL:
            await self._request_approval(task, step, decision)
            return "halt"

        # —— ALLOW：执行 ——
        return await self._execute_step(task, step, decision)

    async def _execute_step(self, task: AgentTask, step: TaskStep, decision: PolicyDecision) -> str:
        """执行一个已放行的步骤。"""
        # 选择正确的转换事件：重试路径走 RETRY_STARTED，首次执行走 START_EXECUTION。
        # 不能一律用 START_EXECUTION——状态机刻意区分这两者，
        # 因为「第一次跑」和「第 N 次重试」在审计里是不同的事件，
        # 而且 RETRY_SCHEDULED 状态本身就是「等退避窗口」的语义。
        start_event = (
            StepEvent.RETRY_STARTED
            if step.status == StepStatus.RETRY_SCHEDULED
            else StepEvent.START_EXECUTION
        )
        new_status = step_state_machine.transition(step.status, start_event)
        step = await self.task_repo.update_step(
            step.step_id, status=new_status, started_at=utcnow()
        )

        await self.audit.tool_execution(
            task_id=task.task_id,
            step_id=step.step_id,
            trace_id=task.trace_id,
            tool_name=step.tool_name or "",
            started=True,
            payload={"arguments": decision.validated_arguments, "attempt": step.retry_count + 1},
        )

        result = await self.executor.execute(
            task_id=task.task_id,
            step=step,
            tool_name=step.tool_name or "",
            decision=decision,
            session=self.session,
            user_id=task.user_id,
            agent_id=task.agent_id,
            trace_id=task.trace_id,
        )

        await self.audit.tool_execution(
            task_id=task.task_id,
            step_id=step.step_id,
            trace_id=task.trace_id,
            tool_name=step.tool_name or "",
            started=False,
            payload={
                "status": str(result.status),
                "error_code": result.error_code,
                "external_reference_id": result.external_reference_id,
                "idempotency_key": result.idempotency_key,
            },
        )

        # —— 成功 ——
        if result.succeeded:
            await self._succeed_step(task, step, result)
            return "continue"

        # —— 超时 / 未知：**绝不当作失败** ——
        if result.unresolved:
            unresolved_status = step_state_machine.transition(
                StepStatus.RUNNING,
                StepEvent.EXECUTION_TIMEOUT
                if result.status == ToolExecutionStatus.TIMEOUT
                else StepEvent.EXECUTION_UNKNOWN,
            )
            step = await self.task_repo.update_step(
                step.step_id,
                status=unresolved_status,
                error_code=result.error_code,
                error_message=result.error_message,
                idempotency_key=result.idempotency_key,
            )
            logger.warning(
                "step_unresolved",
                task_id=task.task_id,
                step_name=step.step_name,
                status=str(unresolved_status),
                idempotency_key=result.idempotency_key,
            )
            # 立即尝试对账。查不清才挂起等待下一轮恢复。
            return await self._reconcile_step(task, step)

        # —— 明确失败：按错误码分流 ——
        return await self._handle_failure(task, step, result)

    async def _reconcile_step(self, task: AgentTask, step: TaskStep) -> str:
        """对账：查明未决步骤的真相。

        **这是超时处理的唯一正确出路。**
        """
        if not step.tool_name:
            return await self._fail_and_settle(task, step, ErrorCode.INTERNAL_ERROR, "步骤缺少工具名")

        # 只读步骤没有副作用，不需要对账，直接重试即可。
        if step.step_type == StepType.READ:
            reset_status = step_state_machine.transition(step.status, StepEvent.SCHEDULE_RETRY)
            await self.task_repo.update_step(step.step_id, status=reset_status)
            return await self._schedule_retry(task, step, ErrorCode.TIMEOUT, retryable=True)

        result = await self.executor.reconcile(
            task_id=task.task_id,
            step=step,
            tool_name=step.tool_name,
            session=self.session,
            trace_id=task.trace_id,
            user_id=task.user_id,
            agent_id=task.agent_id,
        )

        if result is None:
            # 查无可查 → 转人工。**绝不猜。**
            await self.audit.reconciliation(
                task_id=task.task_id,
                step_id=step.step_id,
                trace_id=task.trace_id,
                idempotency_key=step.idempotency_key or "",
                outcome="UNRESOLVABLE",
            )
            await self._finish_manual_review(
                task,
                reason=f"步骤 {step.step_name} 状态未知且无法对账，需人工核实外部系统",
                error_code=ErrorCode.UNKNOWN_EXECUTION_STATE,
            )
            return "terminal"

        if result.succeeded:
            # 查到已成功 → **只补写状态和凭证，绝不重复执行**。
            await self.audit.reconciliation(
                task_id=task.task_id,
                step_id=step.step_id,
                trace_id=task.trace_id,
                idempotency_key=step.idempotency_key or "",
                outcome="ALREADY_SUCCEEDED",
                external_reference_id=result.external_reference_id,
            )
            final_status = step_state_machine.transition(
                step.status, StepEvent.RECONCILED_SUCCESS
            )
            await self.task_repo.update_step(
                step.step_id,
                status=final_status,
                output_payload=result.result,
                external_reference_id=result.external_reference_id,
                completed_at=utcnow(),
            )
            await self.bus.publish(
                DomainEvent(
                    name=STEP_SUCCEEDED,
                    task_id=task.task_id,
                    trace_id=task.trace_id,
                    payload={"step_name": step.step_name, "reconciled": True},
                )
            )
            return "continue"

        # 查明未发生 → 可以带同一个幂等键安全重试。
        await self.audit.reconciliation(
            task_id=task.task_id,
            step_id=step.step_id,
            trace_id=task.trace_id,
            idempotency_key=step.idempotency_key or "",
            outcome="NOT_EXECUTED",
        )
        retry_status = step_state_machine.transition(step.status, StepEvent.SCHEDULE_RETRY)
        step = await self.task_repo.update_step(step.step_id, status=retry_status)
        return await self._schedule_retry(
            task, step, result.error_code or ErrorCode.UPSTREAM_UNAVAILABLE, retryable=True
        )

    # ==================================================================
    # 结果处理
    # ==================================================================
    async def _succeed_step(self, task: AgentTask, step: TaskStep, result: Any) -> None:
        final_status = step_state_machine.transition(step.status, StepEvent.EXECUTION_SUCCEEDED)
        await self.task_repo.update_step(
            step.step_id,
            status=final_status,
            output_payload=result.result,
            external_reference_id=result.external_reference_id,
            idempotency_key=result.idempotency_key,
            completed_at=utcnow(),
            compensation_status=(
                CompensationStatus.REQUIRED
                if step.step_type == StepType.WRITE
                else CompensationStatus.NOT_REQUIRED
            ),
        )
        await self.task_repo.update_task_status(
            task.task_id, TaskStatus.RUNNING, current_step=step.step_name
        )
        # 检查点：非必需，但让「第 N 步结束时任务长什么样」可回放。
        await self.checkpoints.save(
            task_id=task.task_id,
            step_name=step.step_name,
            label="step_success",
            snapshot={"status": str(final_status), "output": result.result},
        )
        await self.bus.publish(
            DomainEvent(
                name=STEP_SUCCEEDED,
                task_id=task.task_id,
                trace_id=task.trace_id,
                payload={"step_name": step.step_name, "tool_name": step.tool_name},
            )
        )

    async def _handle_failure(self, task: AgentTask, step: TaskStep, result: Any) -> str:
        """明确失败的分流处理。"""
        failed_status = step_state_machine.transition(step.status, StepEvent.EXECUTION_FAILED)
        step = await self.task_repo.update_step(
            step.step_id,
            status=failed_status,
            error_code=result.error_code,
            error_message=result.error_message,
            completed_at=utcnow(),
        )

        decision = self.retry_policy.decide(
            step_status=StepStatus(step.status),
            error_code=result.error_code,
            retryable_hint=result.retryable,
            retry_count=step.retry_count,
            max_retries=step.max_retries,
        )
        if decision.should_retry:
            return await self._schedule_retry(
                task, step, result.error_code, retryable=True, precomputed=decision
            )

        # 不可重试。区分关键与非关键步骤：
        # 非关键步骤（通知）失败 → 跳过，任务可以是 PARTIAL_SUCCESS；
        # 关键步骤失败 → 补偿 / 终止。
        if not step.critical:
            logger.info(
                "non_critical_step_failed",
                task_id=task.task_id,
                step_name=step.step_name,
                error_code=result.error_code,
            )
            # 保持 FAILED 状态（供审计追溯），但不阻塞任务收尾。
            return "continue"

        return await self._fail_and_settle(
            task, step, result.error_code, result.error_message or "", already_failed=True
        )

    async def _schedule_retry(
        self,
        task: AgentTask,
        step: TaskStep,
        error_code: str | None,
        *,
        retryable: bool,
        precomputed: Any = None,
    ) -> str:
        """安排一次重试。

        Note:
            **重试状态必须持久化**（`retry_count` + `next_retry_at`）。
            只在内存里维护重试计划意味着进程一重启就失忆——
            而进程重启正是最需要它的时刻。
        """
        decision = precomputed or self.retry_policy.decide(
            step_status=StepStatus(step.status),
            error_code=error_code,
            retryable_hint=retryable,
            retry_count=step.retry_count,
            max_retries=step.max_retries,
        )
        if not decision.should_retry:
            return await self._fail_and_settle(task, step, error_code, decision.reason)

        retry_status = (
            StepStatus(step.status)
            if step.status == StepStatus.RETRY_SCHEDULED
            else step_state_machine.transition(step.status, StepEvent.SCHEDULE_RETRY)
        )
        await self.task_repo.update_step(
            step.step_id,
            status=retry_status,
            increment_retry=True,
            next_retry_at=decision.next_retry_at,
            error_code=error_code,
            error_message=decision.reason,
        )
        await self.audit.retry_scheduled(
            task_id=task.task_id,
            step_id=step.step_id,
            trace_id=task.trace_id,
            attempt=step.retry_count + 1,
            delay_seconds=decision.delay_seconds,
            error_code=error_code,
        )
        await self.bus.publish(
            DomainEvent(
                name=STEP_RETRY_SCHEDULED,
                task_id=task.task_id,
                trace_id=task.trace_id,
                payload={"step_name": step.step_name, "tool_name": step.tool_name},
            )
        )
        await self.task_repo.update_task_status(task.task_id, TaskStatus.RETRYING)

        # 步骤停留在 RETRY_SCHEDULED，由下一轮 _drive 捞起执行
        # （`_execute_step` 会用 RETRY_STARTED 事件把它推进 RUNNING）。
        #
        # Demo 里退避时间很短，所以下一轮循环立刻就会重试。
        # 生产环境应该由 Scheduler 在 `next_retry_at` 到达时才捞起——
        # 这样退避才是真的退避，而不是阻塞住调用方。
        # 关键是 **`next_retry_at` 已经落库**，所以即使进程此刻崩溃，
        # 重试计划也不会丢。
        task = await self.task_repo.update_task_status(task.task_id, TaskStatus.RUNNING)
        return "continue"

    async def _handle_denied(self, task: AgentTask, step: TaskStep, decision: PolicyDecision) -> str:
        """控制层拒绝：工具**绝不执行**。"""
        await self._fail_step(step, ErrorCode.POLICY_DENIED, decision.human_readable_reason)
        if not step.critical:
            return "continue"
        await self._fail_and_settle(
            task, step, ErrorCode.POLICY_DENIED, decision.human_readable_reason, already_failed=True
        )
        return "terminal"

    async def _fail_step(self, step: TaskStep, error_code: str, message: str) -> TaskStep:
        try:
            status = step_state_machine.transition(step.status, StepEvent.EXECUTION_FAILED)
        except IllegalStateTransitionError:
            status = StepStatus.FAILED
        return await self.task_repo.update_step(
            step.step_id,
            status=status,
            error_code=error_code,
            error_message=message,
            completed_at=utcnow(),
        )

    async def _fail_and_settle(
        self,
        task: AgentTask,
        step: TaskStep,
        error_code: str | None,
        message: str,
        *,
        already_failed: bool = False,
    ) -> str:
        """关键步骤失败后的收场：补偿 → 落终态。"""
        if not already_failed:
            await self._fail_step(step, error_code or ErrorCode.INTERNAL_ERROR, message)

        task = await self.task_repo.require_task(task.task_id)
        side_effects = [s for s in task.completed_side_effect_steps() if s.sequence < step.sequence]

        if side_effects:
            # 有已生效的副作用需要撤销 → 走 Saga 补偿。
            # **注意这不是数据库回滚**：那些副作用已经提交、已经对外生效了。
            task = await self._transition_task(task, TaskEvent.START_COMPENSATION)
            comp_result = await self.compensation.compensate_task(
                task,
                session=self.session,
                upto_sequence=step.sequence,
                reason=f"步骤 {step.step_name} 失败：{message}",
            )
            if comp_result.needs_manual_followup:
                # 补偿没能完全收场 → MANUAL_REVIEW 而不是 FAILED。
                # FAILED 意味着「已经收场了」，但这里还有东西没收拾干净。
                await self._finish_manual_review(
                    task,
                    reason=f"补偿未能完全收场：{comp_result.to_dict()}",
                    error_code=error_code,
                )
                return "terminal"
            task = await self._transition_task(task, TaskEvent.COMPENSATION_DONE)
        else:
            task = await self._transition_task(task, TaskEvent.FATAL_ERROR)

        await self.task_repo.update_task_status(
            task.task_id,
            TaskStatus(task.status),
            error_code=error_code,
            error_message=message,
        )
        await self.audit.record(
            AuditEventType.TASK_FAILED,
            actor_type=ActorType.SYSTEM,
            actor_id="orchestrator",
            task_id=task.task_id,
            trace_id=task.trace_id,
            payload={"error_code": error_code, "message": message, "failed_step": step.step_name},
        )
        await self.bus.publish(
            DomainEvent(
                name=TASK_FAILED,
                task_id=task.task_id,
                trace_id=task.trace_id,
                payload={"error_code": error_code},
            )
        )
        await self._compose_final_reply(task, "FAILED", {"reason": message})
        return "terminal"

    # ==================================================================
    # 审批
    # ==================================================================
    async def _request_approval(
        self, task: AgentTask, step: TaskStep, decision: PolicyDecision
    ) -> None:
        """创建审批单并挂起任务。"""
        approval = await self.approval_gate.request_approval(
            task_id=task.task_id,
            step=step,
            decision=decision,
            requester=task.user_id,
            tool_name=step.tool_name or "",
        )
        step_status = step_state_machine.transition(step.status, StepEvent.NEED_APPROVAL)
        await self.task_repo.update_step(step.step_id, status=step_status)
        await self._transition_task(task, TaskEvent.NEED_APPROVAL)
        await self.task_repo.update_task_status(
            task.task_id,
            TaskStatus.WAITING_APPROVAL,
            current_step=step.step_name,
            risk_level=decision.risk_level,
        )
        await self.audit.approval(
            task_id=task.task_id,
            step_id=step.step_id,
            trace_id=task.trace_id,
            requested=True,
            actor_id="policy_engine",
            payload=self.approval_gate.audit_payload(approval),
        )
        await self.bus.publish(
            DomainEvent(
                name=APPROVAL_REQUESTED,
                task_id=task.task_id,
                trace_id=task.trace_id,
                payload={"approval_id": approval.approval_id, "step_name": step.step_name},
            )
        )
        await self.bus.publish(
            DomainEvent(name=TASK_WAITING_APPROVAL, task_id=task.task_id, trace_id=task.trace_id)
        )
        metrics.increment("agent_approvals_requested_total", tool=step.tool_name or "unknown")

    async def _settle_approvals(self, task: AgentTask) -> AgentTask:
        """处理已决的审批结果。

        审批挂起和崩溃恢复本质上是同一件事：
        把任务冻在某一步，等外部条件满足后再继续。
        所以这段逻辑天然地属于恢复流程。
        """
        if task.status != TaskStatus.WAITING_APPROVAL:
            return task

        for step in task.steps:
            if step.status != StepStatus.WAITING_APPROVAL:
                continue
            approval = await self.approval_repo.find_pending_for_step(step.step_id)
            if approval is not None:
                # 还在等人，不往下走。
                return task

            # 找最近一条已决的审批单。
            approvals = await self.approval_repo.list_approvals(limit=200)
            decided = [a for a in approvals if a.step_id == step.step_id]
            if not decided:
                return task
            latest = max(decided, key=lambda a: a.decided_at or a.created_at)

            if latest.status == ApprovalStatus.APPROVED:
                new_status = step_state_machine.transition(
                    step.status, StepEvent.APPROVAL_GRANTED
                )
                await self.task_repo.update_step(step.step_id, status=new_status)
                task = await self._transition_task(task, TaskEvent.APPROVAL_GRANTED)
                await self.audit.approval(
                    task_id=task.task_id,
                    step_id=step.step_id,
                    trace_id=task.trace_id,
                    requested=False,
                    actor_id=latest.approver_id or "",
                    payload=self.approval_gate.audit_payload(latest, {"outcome": "APPROVED"}),
                )
            elif latest.status in (ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED):
                new_status = step_state_machine.transition(
                    step.status, StepEvent.APPROVAL_REJECTED
                )
                await self.task_repo.update_step(
                    step.step_id,
                    status=new_status,
                    error_code=ErrorCode.APPROVAL_REJECTED,
                    error_message=latest.decision_comment or f"审批{latest.status}",
                )
                task = await self._transition_task(task, TaskEvent.APPROVAL_REJECTED)
                await self.audit.approval(
                    task_id=task.task_id,
                    step_id=step.step_id,
                    trace_id=task.trace_id,
                    requested=False,
                    actor_id=latest.approver_id or "system",
                    payload=self.approval_gate.audit_payload(
                        latest, {"outcome": str(latest.status)}
                    ),
                )
                await self._compose_final_reply(
                    task, "FAILED", {"reason": f"审批未通过：{latest.decision_comment or ''}"}
                )
            return await self.task_repo.require_task(task.task_id)

        return task

    # ==================================================================
    # 恢复辅助
    # ==================================================================
    async def _settle_unresolved_steps(self, task: AgentTask) -> AgentTask:
        """处理悬挂与未决步骤。

        1. RUNNING 且长时间无更新 → 标记 UNKNOWN（**不是 FAILED**）；
        2. TIMEOUT / UNKNOWN → 对账。
        """
        stale_threshold = self.settings.stale_running_seconds
        now = utcnow()

        for step in task.steps:
            if step.status == StepStatus.RUNNING:
                age = (now - step.updated_at).total_seconds()
                if age >= stale_threshold:
                    # 悬挂：进程可能崩在了执行中途。结果未知，不是失败。
                    new_status = step_state_machine.transition(
                        step.status, StepEvent.EXECUTION_UNKNOWN
                    )
                    await self.task_repo.update_step(
                        step.step_id,
                        status=new_status,
                        error_code=ErrorCode.UNKNOWN_EXECUTION_STATE,
                        error_message=f"步骤 RUNNING 超过 {stale_threshold} 秒无更新，结果未知",
                    )
                    logger.warning(
                        "stale_running_step_marked_unknown",
                        task_id=task.task_id,
                        step_name=step.step_name,
                        age_seconds=int(age),
                    )

        task = await self.task_repo.require_task(task.task_id)
        for step in task.steps:
            if step.is_unresolved():
                await self._reconcile_step(task, step)
                task = await self.task_repo.require_task(task.task_id)
                if task.is_terminal():
                    return task
        return task

    # ==================================================================
    # 收尾
    # ==================================================================
    async def _finalize(self, task: AgentTask) -> AgentTask:
        """所有步骤都处理完之后的收尾。

        判定规则：

        * 所有关键步骤成功 + 所有非关键步骤成功 → COMPLETED
        * 所有关键步骤成功 + 有非关键步骤失败   → PARTIAL_SUCCESS
        * 有关键步骤未成功                      → FAILED（理论上不会走到这里）
        """
        task = await self.task_repo.require_task(task.task_id)
        critical_ok = all(
            s.status == StepStatus.SUCCESS for s in task.steps if s.critical
        )
        non_critical_failed = [
            s for s in task.steps if not s.critical and s.status != StepStatus.SUCCESS
        ]

        facts = await self._collect_facts(task)

        if critical_ok and not non_critical_failed:
            task = await self._transition_task(task, TaskEvent.ALL_STEPS_DONE)
            outcome = "COMPLETED"
            event_name = TASK_COMPLETED
        elif critical_ok:
            # **折扣成功但通知失败** 就走这条路。
            # 关键：绝不因为通知失败去撤销折扣——那是两回事。
            # 通知可以单独重试，折扣是否撤销必须由业务规则或人来决定。
            task = await self._transition_task(task, TaskEvent.PARTIALLY_DONE)
            outcome = "PARTIAL_SUCCESS"
            event_name = TASK_PARTIAL_SUCCESS
            facts["notification_status"] = "发送失败，已记录待重试"
        else:  # pragma: no cover - 关键步骤失败会在更早的分支处理
            task = await self._transition_task(task, TaskEvent.FATAL_ERROR)
            outcome = "FAILED"
            event_name = TASK_FAILED

        reply = await self._compose_final_reply(task, outcome, facts)
        task = await self.task_repo.update_task_status(
            task.task_id,
            TaskStatus(task.status),
            result_payload={
                "outcome": outcome,
                "reply": reply,
                "facts": facts,
                "steps": task.summarize_steps(),
                "failed_optional_steps": [s.step_name for s in non_critical_failed],
                "cost": metrics.task_cost(task.task_id),
            },
        )
        await self.audit.record(
            AuditEventType.TASK_COMPLETED,
            actor_type=ActorType.SYSTEM,
            actor_id="orchestrator",
            task_id=task.task_id,
            trace_id=task.trace_id,
            payload={"outcome": outcome, "steps": task.summarize_steps()},
        )
        await self.bus.publish(
            DomainEvent(
                name=event_name,
                task_id=task.task_id,
                trace_id=task.trace_id,
                payload={"outcome": outcome},
            )
        )
        return task

    async def _finish_manual_review(
        self, task: AgentTask, *, reason: str, error_code: str | None = None
    ) -> AgentTask:
        """把任务落到 MANUAL_REVIEW 终态。"""
        task = await self._transition_task(task, TaskEvent.ESCALATE_TO_HUMAN)
        task = await self.task_repo.update_task_status(
            task.task_id,
            TaskStatus.MANUAL_REVIEW,
            error_code=error_code,
            error_message=reason,
            result_payload={"outcome": "MANUAL_REVIEW", "reason": reason},
        )
        await self.audit.manual_intervention(
            task_id=task.task_id, step_id=None, trace_id=task.trace_id, reason=reason
        )
        metrics.increment("agent_manual_reviews_total")
        return task

    async def _compose_final_reply(
        self, task: AgentTask, outcome: str, facts: dict[str, Any]
    ) -> str:
        """调用 LLM 把结果写成人话。

        这是模型在整个流程里的**第二次也是最后一次**出场。
        而且它只负责组织语言——所有数字都由程序通过 `facts` 提供。
        """
        try:
            identity = await self.authorization.resolve(task.user_id, task.agent_id)
            recent = [
                StepSummary(
                    step_name=s.step_name,
                    status=str(s.status),
                    summary=s.summary(),
                    error_code=s.error_code,
                )
                for s in task.steps
            ]
            context = await self.context_builder.build(
                task_id=task.task_id,
                trace_id=task.trace_id,
                identity=identity,
                user_input=task.original_input,
                task_status=str(task.status),
                recent_steps=recent,
                business_facts=facts,
                session=self.session,
                retrieve_limit=0,
            )
            return await self.reply_composer.compose(context, outcome=outcome, facts=facts)
        except Exception as exc:  # noqa: BLE001
            # 回复生成失败不能让整个任务失败。
            # 用户宁可看到一句朴素的模板话，也不要看到 500。
            logger.warning("final_reply_failed", task_id=task.task_id, error=str(exc))
            return f"任务处理结果：{outcome}"

    async def _collect_facts(self, task: AgentTask) -> dict[str, Any]:
        """从**业务系统**收集事实，用于最终回复与验收。

        注意是从业务表查，不是读任务自己记录的输出——
        用任务自己的输出做验收，验的只是「我们有没有正确记录」，
        不是「外部世界是不是真的变成了预期的样子」。
        """
        facts: dict[str, Any] = {}
        for step in task.steps:
            payload = step.input_payload or {}
            args = payload.get("arguments") or {}
            if args.get("customer_id"):
                facts["customer_id"] = args["customer_id"]
            if step.step_name == "apply_discount" and args.get("discount_rate") is not None:
                facts["requested_rate"] = args["discount_rate"]
            if step.status == StepStatus.SUCCESS and step.output_payload:
                if step.step_name == "apply_discount":
                    facts["discount_id"] = step.output_payload.get("discount_id")
                    facts["discount_rate"] = step.output_payload.get("discount_rate")
                if step.step_name == "send_notification":
                    facts["notification_status"] = "已发送"

        customer_id = facts.get("customer_id")
        if customer_id:
            customer = await self.session.get(CustomerORM, customer_id)
            if customer:
                facts["customer_tier"] = customer.tier
            active = await self.session.execute(
                select(DiscountORM)
                .where(DiscountORM.customer_id == customer_id)
                .where(DiscountORM.status == "ACTIVE")
            )
            rows = list(active.scalars().all())
            facts["active_discount_count"] = len(rows)
            if rows:
                facts["actual_rate"] = rows[0].discount_rate
        return facts

    # ==================================================================
    # 策略评估
    # ==================================================================
    async def _evaluate(
        self, task: AgentTask, step: TaskStep, proposal: ActionProposal
    ) -> PolicyDecision:
        """构造策略评估请求并调用引擎。"""
        tool_name = proposal.tool_name
        registered = self.registry.has(tool_name)

        service_id = ""
        risk_level = RiskLevel.HIGH
        required_perms: set[str] = set()
        is_write = False
        idempotent = True
        if registered:
            tool = self.registry.get(tool_name)
            service_id = tool.service_id
            risk_level = tool.risk_level
            required_perms = set(tool.required_permissions)
            is_write = tool.step_type in (StepType.WRITE, StepType.NOTIFY)
            idempotent = tool.idempotent

        identity = await self.authorization.resolve(task.user_id, task.agent_id, service_id or None)
        facts = await self._business_facts_for(proposal)

        context = await self.context_builder.build(
            task_id=task.task_id,
            trace_id=task.trace_id,
            identity=identity,
            user_input=task.original_input,
            task_status=str(task.status),
            current_step=step.step_name,
            business_facts=facts,
            session=self.session,
            retrieve_limit=0,
        )

        # 审批已通过 → 让 ApprovalPolicy 短路。
        # **但其它策略照跑不误**：即使经理批了，超过绝对上限的折扣仍会被拒。
        approval_granted = await self._has_granted_approval(step)

        request = PolicyEvaluationRequest(
            proposal=proposal,
            identity=identity,
            context=context,
            tool_name=tool_name,
            tool_registered=registered,
            tool_risk_level=risk_level,
            tool_required_permissions=required_perms,
            tool_is_write=is_write,
            tool_idempotent=idempotent,
            business_facts=facts,
            attempt=step.retry_count + 1,
            approval_granted=approval_granted,
        )
        return await self.policy_engine.evaluate(request)

    async def _business_facts_for(self, proposal: ActionProposal) -> dict[str, Any]:
        """查询与本次动作相关的业务事实。

        **事实要从系统里查，不能问模型。**
        「这个客户是不是 VIP」「他有没有生效折扣」都有唯一答案，
        去库里查既更可靠也更便宜。
        """
        facts: dict[str, Any] = {}
        customer_id = (proposal.arguments or {}).get("customer_id")
        if not customer_id:
            return facts

        facts["customer_id"] = customer_id
        customer = await self.session.get(CustomerORM, str(customer_id))
        if customer is not None:
            facts.update(
                {
                    "customer_tier": customer.tier,
                    "customer_status": customer.status,
                    "customer_department": customer.department,
                    "customer_name": customer.name,
                    "customer_phone": customer.phone,
                    "customer_email": customer.email,
                }
            )
        result = await self.session.execute(
            select(DiscountORM)
            .where(DiscountORM.customer_id == str(customer_id))
            .where(DiscountORM.status == "ACTIVE")
        )
        active = result.scalars().first()
        if active is not None:
            facts["active_discount"] = {
                "discount_id": active.discount_id,
                "discount_rate": active.discount_rate,
            }
        return facts

    async def _has_granted_approval(self, step: TaskStep) -> bool:
        approvals = await self.approval_repo.list_approvals(limit=200)
        return any(
            a.step_id == step.step_id and a.status == ApprovalStatus.APPROVED for a in approvals
        )

    def _proposal_from_step(self, step: TaskStep) -> ActionProposal:
        """从步骤的输入快照重建 ActionProposal。

        **恢复时靠的是这份快照，不是重新问模型。**
        入参快照是在登记步骤时就落库的，所以无论隔了多久、
        进程重启了多少次，重建出来的动作都和当初一模一样。
        """
        payload = step.input_payload or {}
        risk_hint = payload.get("risk_hint", "LOW")
        try:
            risk = RiskLevel(risk_hint)
        except ValueError:
            risk = RiskLevel.LOW
        return ActionProposal(
            intent=payload.get("intent", step.step_name),
            tool_name=step.tool_name or "",
            arguments=payload.get("arguments") or {},
            reasoning_summary=payload.get("reasoning_summary", ""),
            confidence=float(payload.get("confidence") or 0.9),
            requested_by=payload.get("requested_by", "llm"),
            risk_hint=risk,
        )

    # ==================================================================
    # 状态转换
    # ==================================================================
    async def _transition_task(
        self, task: AgentTask, event: TaskEvent, *, fallback: TaskStatus | None = None
    ) -> AgentTask:
        """执行任务状态转换并落库。

        非法转换会写 ILLEGAL_STATE_TRANSITION 审计后抛出——
        **不静默修正**，因为静默修正会把一个明显的 Bug
        变成一个隐蔽的数据不一致。
        """
        try:
            target = task_state_machine.transition(TaskStatus(task.status), event)
        except IllegalStateTransitionError as exc:
            await self.audit.state_transition(
                task_id=task.task_id,
                step_id=None,
                trace_id=task.trace_id,
                scope="task",
                from_status=str(task.status),
                to_status="",
                event=str(event),
                legal=False,
            )
            if fallback is not None:
                target = fallback
            else:
                raise exc

        updated = await self.task_repo.update_task_status(task.task_id, target)
        await self.audit.state_transition(
            task_id=task.task_id,
            step_id=None,
            trace_id=task.trace_id,
            scope="task",
            from_status=str(task.status),
            to_status=str(target),
            event=str(event),
        )
        return updated
