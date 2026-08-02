"""仓库层：状态读写的唯一入口。

**为什么要有 Repository 而不是让业务代码直接用 Session？**

1. 状态转换必须集中管理。如果任何模块都能 `task.status = "..."`，
   状态机就形同虚设。Repository + StateMachine 是这条纪律的物理保证。
2. 数据库可替换。业务代码只依赖 Repository 的方法签名，
   SQLite → PostgreSQL 的切换不会渗透到业务层。
3. 幂等与乐观锁这类横切逻辑有唯一实现点，不会出现「A 处做了、B 处忘了」。

所有方法都接收一个 `AsyncSession`，由调用方决定事务边界——
这很重要：**「登记步骤状态」和「写审计」必须在同一个事务里**，
否则会出现「状态变了但审计没写」这种事后无法解释的空洞。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    ApprovalStatus,
    CompensationStatus,
    RiskLevel,
    StepStatus,
    StepType,
    TaskStatus,
    ToolExecutionStatus,
)
from app.core.errors import IdempotencyConflictError, TaskNotFoundError
from app.core.ids import (
    new_approval_id,
    new_event_id,
    new_id,
    new_step_id,
    new_task_id,
    utcnow,
)
from app.runtime.models import AgentTask, ApprovalRequest, AuditEvent, TaskStep
from app.state.models import (
    ApprovalORM,
    AuditEventORM,
    CheckpointORM,
    StepORM,
    TaskORM,
    ToolExecutionORM,
)


class TaskRepository:
    """任务与步骤的读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ 任务
    async def create_task(
        self,
        *,
        user_id: str,
        agent_id: str,
        original_input: str,
        task_type: str = "generic",
        trace_id: str = "",
        task_id: str | None = None,
    ) -> AgentTask:
        """创建任务。

        Args:
            user_id: 发起人。
            agent_id: 目标 Agent。
            original_input: 用户原始输入（**存原文**，用于审计追溯）。
            task_type: 任务类型。
            trace_id: 全链路追踪 ID。
            task_id: 可指定的任务 ID，用于测试可重现。

        Returns:
            新建的任务领域模型。
        """
        orm = TaskORM(
            task_id=task_id or new_task_id(),
            user_id=user_id,
            agent_id=agent_id,
            task_type=task_type,
            original_input=original_input,
            status=TaskStatus.CREATED,
            risk_level=RiskLevel.LOW,
            trace_id=trace_id,
        )
        self.session.add(orm)
        await self.session.flush()
        return AgentTask.model_validate(_task_to_dict(orm, []))

    async def get_task(self, task_id: str, *, with_steps: bool = True) -> AgentTask | None:
        """按 ID 读取任务。"""
        orm = await self.session.get(TaskORM, task_id)
        if orm is None:
            return None
        steps: list[StepORM] = []
        if with_steps:
            result = await self.session.execute(
                select(StepORM).where(StepORM.task_id == task_id).order_by(StepORM.sequence)
            )
            steps = list(result.scalars().all())
        return AgentTask.model_validate(_task_to_dict(orm, steps))

    async def require_task(self, task_id: str) -> AgentTask:
        """按 ID 读取任务，不存在则抛异常。

        Raises:
            TaskNotFoundError: 任务不存在。
        """
        task = await self.get_task(task_id)
        if task is None:
            raise TaskNotFoundError("任务不存在", details={"task_id": task_id})
        return task

    async def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        expected_version: int | None = None,
        current_step: str | None = None,
        risk_level: RiskLevel | None = None,
        result_payload: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> AgentTask:
        """更新任务状态（带乐观锁）。

        Args:
            expected_version: 期望的版本号。传入时会做 CAS 更新，
                版本不匹配说明有另一个 worker 同时在推进这个任务——
                此时应该放弃而不是覆盖，否则两个 worker 会互相回退对方的进度。

        Raises:
            TaskNotFoundError: 任务不存在或版本冲突。
        """
        values: dict[str, Any] = {
            "status": status,
            "updated_at": utcnow(),
            "version": TaskORM.version + 1,
        }
        if current_step is not None:
            values["current_step"] = current_step
        if risk_level is not None:
            values["risk_level"] = risk_level
        if result_payload is not None:
            values["result_payload"] = result_payload
        if error_code is not None:
            values["error_code"] = error_code
        if error_message is not None:
            values["error_message"] = error_message

        stmt = update(TaskORM).where(TaskORM.task_id == task_id)
        if expected_version is not None:
            stmt = stmt.where(TaskORM.version == expected_version)
        result = await self.session.execute(stmt.values(**values))

        if result.rowcount == 0:
            raise TaskNotFoundError(
                "任务不存在或版本冲突（可能有另一个 worker 正在推进）",
                details={"task_id": task_id, "expected_version": expected_version},
            )
        await self.session.flush()
        return await self.require_task(task_id)

    async def list_tasks(
        self,
        *,
        user_id: str | None = None,
        status: TaskStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentTask]:
        """分页列出任务。"""
        stmt = select(TaskORM).order_by(TaskORM.created_at.desc()).limit(limit).offset(offset)
        if user_id:
            stmt = stmt.where(TaskORM.user_id == user_id)
        if status:
            stmt = stmt.where(TaskORM.status == status)
        result = await self.session.execute(stmt)
        return [AgentTask.model_validate(_task_to_dict(orm, [])) for orm in result.scalars().all()]

    async def list_resumable_tasks(self, limit: int = 100) -> list[AgentTask]:
        """列出所有**非终态**任务，供恢复扫描使用。

        这是「进程重启后知道该干什么」的入口。注意它只依赖数据库，
        **一次模型调用都没有**——「上次执行到哪儿」不需要问大模型，
        程序从状态表就能确定，而且比模型可靠得多。
        """
        terminal = [
            TaskStatus.COMPLETED,
            TaskStatus.PARTIAL_SUCCESS,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.MANUAL_REVIEW,
        ]
        result = await self.session.execute(
            select(TaskORM)
            .where(TaskORM.status.notin_([str(s) for s in terminal]))
            .order_by(TaskORM.updated_at.asc())
            .limit(limit)
        )
        return [AgentTask.model_validate(_task_to_dict(orm, [])) for orm in result.scalars().all()]

    # ------------------------------------------------------------------ 步骤
    async def create_step(
        self,
        *,
        task_id: str,
        step_name: str,
        sequence: int,
        step_type: StepType,
        tool_name: str | None = None,
        input_payload: dict[str, Any] | None = None,
        max_retries: int = 3,
        critical: bool = True,
        depends_on: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> TaskStep:
        """登记一个步骤。

        **步骤必须在执行前登记**（状态 PENDING/RUNNING），而不是执行完再补。
        如果执行和登记之间进程崩溃，这次执行就成了无人知晓的孤儿：
        外部系统那边已经生效，你的库里查无此事。
        """
        orm = StepORM(
            step_id=new_step_id(),
            task_id=task_id,
            step_name=step_name,
            sequence=sequence,
            step_type=step_type,
            status=StepStatus.PENDING,
            tool_name=tool_name,
            input_payload=input_payload,
            max_retries=max_retries,
            critical=critical,
            depends_on=depends_on or [],
            idempotency_key=idempotency_key,
        )
        self.session.add(orm)
        await self.session.flush()
        return TaskStep.model_validate(_step_to_dict(orm))

    async def get_step(self, step_id: str) -> TaskStep | None:
        """按 ID 读取步骤。"""
        orm = await self.session.get(StepORM, step_id)
        return TaskStep.model_validate(_step_to_dict(orm)) if orm else None

    async def list_steps(self, task_id: str) -> list[TaskStep]:
        """列出任务的全部步骤（按 sequence 升序）。"""
        result = await self.session.execute(
            select(StepORM).where(StepORM.task_id == task_id).order_by(StepORM.sequence)
        )
        return [TaskStep.model_validate(_step_to_dict(o)) for o in result.scalars().all()]

    async def update_step(
        self,
        step_id: str,
        *,
        status: StepStatus | None = None,
        output_payload: dict[str, Any] | None = None,
        input_payload: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        idempotency_key: str | None = None,
        external_reference_id: str | None = None,
        compensation_status: CompensationStatus | None = None,
        increment_retry: bool = False,
        next_retry_at: datetime | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> TaskStep:
        """更新步骤字段。

        Note:
            这个方法**不做状态合法性校验**——校验在
            :class:`~app.runtime.state_machine.StepStateMachine` 里，
            由 Orchestrator 在调用本方法前完成。
            分开的原因：仓库层不应该知道业务状态机的规则，
            否则测试状态机就必须连数据库。
        """
        orm = await self.session.get(StepORM, step_id)
        if orm is None:
            raise TaskNotFoundError("步骤不存在", details={"step_id": step_id})

        if status is not None:
            orm.status = status
        if output_payload is not None:
            orm.output_payload = output_payload
        if input_payload is not None:
            orm.input_payload = input_payload
        # 允许显式清空错误信息：重试成功后必须把上一次的错误抹掉，
        # 否则一个 SUCCESS 的步骤上挂着错误码，事后排查会被严重误导。
        orm.error_code = error_code
        orm.error_message = error_message
        if idempotency_key is not None:
            orm.idempotency_key = idempotency_key
        if external_reference_id is not None:
            orm.external_reference_id = external_reference_id
        if compensation_status is not None:
            orm.compensation_status = compensation_status
        if increment_retry:
            orm.retry_count += 1
        orm.next_retry_at = next_retry_at
        if started_at is not None:
            orm.started_at = started_at
        if completed_at is not None:
            orm.completed_at = completed_at
        orm.updated_at = utcnow()
        orm.version += 1
        await self.session.flush()
        return TaskStep.model_validate(_step_to_dict(orm))

    async def find_stale_running_steps(self, older_than_seconds: int) -> list[TaskStep]:
        """找出「悬挂」的 RUNNING 步骤。

        判定依据：状态是 RUNNING，但 `updated_at` 已经超过阈值没动过。
        这类步骤的真相是**未知**，不是失败——恢复时必须标记为 UNKNOWN
        并进入对账流程。
        """
        threshold = utcnow() - timedelta(seconds=older_than_seconds)
        result = await self.session.execute(
            select(StepORM)
            .where(StepORM.status == StepStatus.RUNNING)
            .where(StepORM.updated_at < threshold)
        )
        return [TaskStep.model_validate(_step_to_dict(o)) for o in result.scalars().all()]


class ToolExecutionRepository:
    """工具执行记录 + 幂等去重表。

    **顺序很重要：先占位，再执行。**

    很多实现把「写去重记录」放在执行成功之后，这留了一个致命窗口：
    如果在「执行成功」和「写记录」之间进程崩了，去重记录就丢了，
    重试必然产生第二笔。正确顺序是执行前先插一条 IN_FLIGHT，成功后改成 SUCCESS。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def find_by_idempotency_key(self, key: str) -> ToolExecutionORM | None:
        """按幂等键查历史执行记录。"""
        result = await self.session.execute(
            select(ToolExecutionORM).where(ToolExecutionORM.idempotency_key == key)
        )
        return result.scalar_one_or_none()

    async def reserve(
        self,
        *,
        task_id: str,
        step_id: str,
        tool_name: str,
        idempotency_key: str,
        arguments: dict[str, Any],
        arguments_hash: str,
        attempt: int = 1,
        trace_id: str = "",
    ) -> tuple[ToolExecutionORM, bool]:
        """占位（执行前登记意图）。

        Returns:
            ``(记录, 是否为新建)``。``is_new=False`` 表示幂等键已存在，
            调用方必须根据已有记录的状态决定：

            * SUCCESS → 直接返回历史结果，**不重复执行**；
            * IN_FLIGHT → 说明有另一次执行正在进行或已崩溃，返回当前状态；
            * FAILED 且可重试 → 允许重试。

        Raises:
            IdempotencyConflictError: 同一幂等键但参数指纹不同。
                这种情况必须显式拒绝——如果返回旧结果，
                第二笔业务就会被静默吞掉，而没有任何人知道。
        """
        existing = await self.find_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.arguments_hash and existing.arguments_hash != arguments_hash:
                raise IdempotencyConflictError(
                    "同一幂等键对应的参数不一致，拒绝执行",
                    details={
                        "idempotency_key": idempotency_key,
                        "existing_hash": existing.arguments_hash,
                        "incoming_hash": arguments_hash,
                    },
                )
            return existing, False

        orm = ToolExecutionORM(
            execution_id=new_id("exec"),
            task_id=task_id,
            step_id=step_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            arguments=arguments,
            arguments_hash=arguments_hash,
            status=ToolExecutionStatus.IN_FLIGHT,
            attempt=attempt,
            trace_id=trace_id,
        )
        self.session.add(orm)
        try:
            await self.session.flush()
        except IntegrityError:
            # 并发下另一个进程抢先插入了同一个键。
            # 这正是**数据库唯一约束**存在的意义：应用层的「先查再写」挡不住并发。
            await self.session.rollback()
            existing = await self.find_by_idempotency_key(idempotency_key)
            if existing is None:  # pragma: no cover - 理论上不可达
                raise
            return existing, False
        return orm, True

    async def complete(
        self,
        execution_id: str,
        *,
        status: ToolExecutionStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool = False,
        external_reference_id: str | None = None,
    ) -> ToolExecutionORM:
        """落盘执行结果。"""
        orm = await self.session.get(ToolExecutionORM, execution_id)
        if orm is None:
            raise TaskNotFoundError("执行记录不存在", details={"execution_id": execution_id})
        orm.status = status
        orm.result = result
        orm.error_code = error_code
        orm.error_message = error_message
        orm.retryable = retryable
        if external_reference_id is not None:
            orm.external_reference_id = external_reference_id
        orm.completed_at = utcnow()
        await self.session.flush()
        return orm

    async def list_by_step(self, step_id: str) -> list[ToolExecutionORM]:
        """列出某步骤的全部执行记录（含每一次重试）。"""
        result = await self.session.execute(
            select(ToolExecutionORM)
            .where(ToolExecutionORM.step_id == step_id)
            .order_by(ToolExecutionORM.started_at)
        )
        return list(result.scalars().all())

    async def list_by_task(self, task_id: str) -> list[ToolExecutionORM]:
        """列出某任务的全部执行记录。"""
        result = await self.session.execute(
            select(ToolExecutionORM)
            .where(ToolExecutionORM.task_id == task_id)
            .order_by(ToolExecutionORM.started_at)
        )
        return list(result.scalars().all())


class ApprovalRepository:
    """审批单读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        task_id: str,
        step_id: str,
        requested_action: dict[str, Any],
        requester: str,
        approver_role: str,
        reason: str,
        risk_level: RiskLevel,
        timeout_seconds: int,
    ) -> ApprovalRequest:
        """创建审批单。

        `expires_at` 是必填的：**没有超时回收的审批会让任务永远悬着**。
        """
        orm = ApprovalORM(
            approval_id=new_approval_id(),
            task_id=task_id,
            step_id=step_id,
            requested_action=requested_action,
            requester=requester,
            approver_role=approver_role,
            reason=reason,
            risk_level=risk_level,
            status=ApprovalStatus.PENDING,
            expires_at=utcnow() + timedelta(seconds=timeout_seconds),
        )
        self.session.add(orm)
        await self.session.flush()
        return ApprovalRequest.model_validate(orm)

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        """按 ID 读取审批单。"""
        orm = await self.session.get(ApprovalORM, approval_id)
        return ApprovalRequest.model_validate(orm) if orm else None

    async def find_pending_for_step(self, step_id: str) -> ApprovalRequest | None:
        """查某步骤是否已有待审批单（防止重复创建把审批人淹没）。"""
        result = await self.session.execute(
            select(ApprovalORM)
            .where(ApprovalORM.step_id == step_id)
            .where(ApprovalORM.status == ApprovalStatus.PENDING)
        )
        orm = result.scalars().first()
        return ApprovalRequest.model_validate(orm) if orm else None

    async def list_approvals(
        self,
        *,
        status: ApprovalStatus | None = None,
        approver_role: str | None = None,
        limit: int = 50,
    ) -> list[ApprovalRequest]:
        """列出审批单。"""
        stmt = select(ApprovalORM).order_by(ApprovalORM.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(ApprovalORM.status == status)
        if approver_role:
            stmt = stmt.where(ApprovalORM.approver_role == approver_role)
        result = await self.session.execute(stmt)
        return [ApprovalRequest.model_validate(o) for o in result.scalars().all()]

    async def decide(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        approver_id: str,
        comment: str | None = None,
    ) -> ApprovalRequest:
        """记录审批决策。

        **幂等设计**：如果审批单已经不是 PENDING，直接返回现状而不是报错。
        原因：审批回调可能被重复投递（人手抖点了两次、消息队列重投），
        第二次点击不应该产生任何额外效果。
        """
        orm = await self.session.get(ApprovalORM, approval_id)
        if orm is None:
            from app.core.errors import ApprovalNotFoundError

            raise ApprovalNotFoundError("审批单不存在", details={"approval_id": approval_id})
        if orm.status != ApprovalStatus.PENDING:
            return ApprovalRequest.model_validate(orm)
        orm.status = status
        orm.approver_id = approver_id
        orm.decision_comment = comment
        orm.decided_at = utcnow()
        await self.session.flush()
        return ApprovalRequest.model_validate(orm)

    async def expire_overdue(self, now: datetime | None = None) -> list[ApprovalRequest]:
        """把超时未决的审批单标记为 EXPIRED。

        由 Scheduler 周期调用。这是「每个任务最终都落到明确终态」的保证之一。
        """
        now = now or utcnow()
        result = await self.session.execute(
            select(ApprovalORM)
            .where(ApprovalORM.status == ApprovalStatus.PENDING)
            .where(ApprovalORM.expires_at.is_not(None))
            .where(ApprovalORM.expires_at <= now)
        )
        expired: list[ApprovalRequest] = []
        for orm in result.scalars().all():
            orm.status = ApprovalStatus.EXPIRED
            orm.decided_at = now
            expired.append(ApprovalRequest.model_validate(orm))
        await self.session.flush()
        return expired


class AuditRepository:
    """审计事件写入与查询。

    审计是**只追加**的：没有 update，没有 delete。
    可修改的审计日志等于没有审计。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(
        self,
        *,
        event_type: str,
        actor_type: str,
        actor_id: str = "",
        task_id: str | None = None,
        step_id: str | None = None,
        payload: dict[str, Any] | None = None,
        trace_id: str = "",
    ) -> AuditEvent:
        """追加一条审计事件。

        Note:
            调用方应确保 `payload` 已经过脱敏。
            :class:`~app.operations.audit.AuditService` 会统一处理，
            直接用本仓库的调用方需要自己负责。
        """
        orm = AuditEventORM(
            event_id=new_event_id(),
            task_id=task_id,
            step_id=step_id,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=payload or {},
            trace_id=trace_id,
        )
        self.session.add(orm)
        await self.session.flush()
        return AuditEvent.model_validate(orm)

    async def list_by_task(self, task_id: str, limit: int = 500) -> list[AuditEvent]:
        """按任务列出审计事件（时间升序，便于回放）。"""
        result = await self.session.execute(
            select(AuditEventORM)
            .where(AuditEventORM.task_id == task_id)
            .order_by(AuditEventORM.created_at.asc())
            .limit(limit)
        )
        return [AuditEvent.model_validate(o) for o in result.scalars().all()]

    async def count_by_type(self, task_id: str) -> dict[str, int]:
        """统计任务下各类事件数量（运营视图用）。"""
        result = await self.session.execute(
            select(AuditEventORM.event_type, func.count())
            .where(AuditEventORM.task_id == task_id)
            .group_by(AuditEventORM.event_type)
        )
        return {row[0]: row[1] for row in result.all()}


class CheckpointRepository:
    """检查点快照读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(
        self,
        *,
        task_id: str,
        snapshot: dict[str, Any],
        step_name: str | None = None,
        label: str = "",
    ) -> str:
        """保存一个检查点，返回 checkpoint_id。"""
        orm = CheckpointORM(
            checkpoint_id=new_id("ckpt"),
            task_id=task_id,
            step_name=step_name,
            label=label,
            snapshot=snapshot,
        )
        self.session.add(orm)
        await self.session.flush()
        return orm.checkpoint_id

    async def list_by_task(self, task_id: str) -> Sequence[CheckpointORM]:
        """列出任务的全部检查点。"""
        result = await self.session.execute(
            select(CheckpointORM)
            .where(CheckpointORM.task_id == task_id)
            .order_by(CheckpointORM.created_at)
        )
        return list(result.scalars().all())

    async def latest(self, task_id: str) -> CheckpointORM | None:
        """取最近一个检查点。"""
        result = await self.session.execute(
            select(CheckpointORM)
            .where(CheckpointORM.task_id == task_id)
            .order_by(CheckpointORM.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()


# --------------------------------------------------------------------------------------
# ORM → dict 的转换。刻意不用 `from_attributes` 直接映射 ORM 对象，
# 因为 ORM 对象上的 relationship 会在 Pydantic 遍历时触发 lazy load，
# 在异步会话里会直接抛 MissingGreenlet。显式转换虽然啰嗦，但行为可预测。
# --------------------------------------------------------------------------------------
def _task_to_dict(orm: TaskORM, steps: Sequence[StepORM]) -> dict[str, Any]:
    return {
        "task_id": orm.task_id,
        "user_id": orm.user_id,
        "agent_id": orm.agent_id,
        "task_type": orm.task_type,
        "original_input": orm.original_input,
        "status": orm.status,
        "risk_level": orm.risk_level,
        "current_step": orm.current_step,
        "trace_id": orm.trace_id,
        "result_payload": orm.result_payload,
        "error_code": orm.error_code,
        "error_message": orm.error_message,
        "created_at": orm.created_at,
        "updated_at": orm.updated_at,
        "version": orm.version,
        "steps": [_step_to_dict(s) for s in steps],
    }


def _step_to_dict(orm: StepORM) -> dict[str, Any]:
    return {
        "step_id": orm.step_id,
        "task_id": orm.task_id,
        "step_name": orm.step_name,
        "step_type": orm.step_type,
        "sequence": orm.sequence,
        "status": orm.status,
        "tool_name": orm.tool_name,
        "input_payload": orm.input_payload,
        "output_payload": orm.output_payload,
        "error_code": orm.error_code,
        "error_message": orm.error_message,
        "retry_count": orm.retry_count,
        "max_retries": orm.max_retries,
        "idempotency_key": orm.idempotency_key,
        "external_reference_id": orm.external_reference_id,
        "compensation_status": orm.compensation_status,
        "critical": orm.critical,
        "depends_on": orm.depends_on or [],
        "next_retry_at": orm.next_retry_at,
        "started_at": orm.started_at,
        "completed_at": orm.completed_at,
        "created_at": orm.created_at,
        "updated_at": orm.updated_at,
        "version": orm.version,
    }
