"""SQLAlchemy 持久化模型（状态层）。

字段设计的每一条都对应一个「不这么设计就会出事」的场景：

* ``idempotency_key`` + **数据库唯一约束**：重试的安全前提，也是对账的钥匙。
  只在应用层查一遍再写是不够的——并发下两个进程会同时查到「没有」然后同时写入。
  唯一约束是最后也是唯一可靠的一道闸。
* ``external_reference_id``：外部系统返回的凭证（支付流水号、单据号）。
  没有它，一条 UNKNOWN 记录就永远查不清了。
* ``input_payload`` / ``output_payload`` 快照：恢复时不用重新推导入参，
  后续步骤也能直接用上一步的产出。
* ``version``：乐观锁。多个 worker 同时恢复同一个任务时，靠它避免状态互相覆盖。
* ``updated_at``：用于发现悬挂任务（RUNNING 超过 N 分钟 = 可疑）。

关于 JSON 字段：这里用 SQLAlchemy 的通用 ``JSON`` 类型，
SQLite 存 TEXT、PostgreSQL 存 JSON，**业务代码不需要知道差别**。
如果想在 PG 上用 JSONB 加索引，可以在 Alembic 迁移里单独处理，ORM 定义不用动。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    ApprovalStatus,
    CompensationStatus,
    RiskLevel,
    StepStatus,
    StepType,
    TaskStatus,
    ToolExecutionStatus,
)
from app.core.ids import utcnow
from app.state.database import Base


def _now_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TaskORM(Base):
    """任务表。一次用户请求对应一行。"""

    __tablename__ = "agent_tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False, default="generic")
    #: 用户原始输入。**这里存原文**（用于审计与追溯），
    #: 但送进 LLM 的是脱敏后的版本——两者刻意分开存放。
    original_input: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TaskStatus.CREATED, index=True
    )
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default=RiskLevel.LOW)
    #: 当前进行到的步骤名。恢复时用于快速定位，真正的判定仍以步骤表为准。
    current_step: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    #: 最终结果摘要（含给用户的回复、关键业务字段）。
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    #: 乐观锁版本号。每次状态变更 +1，防止并发恢复互相覆盖。
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    steps: Mapped[list[StepORM]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="StepORM.sequence",
        lazy="selectin",
    )

    __table_args__ = (
        # 恢复扫描的主查询：按状态 + 更新时间捞非终态任务。
        Index("ix_task_status_updated", "status", "updated_at"),
    )


class StepORM(Base):
    """步骤表。断点续跑的地基。

    **一条原则：可记录的粒度，就是可恢复的粒度。**
    你希望能从哪里重来，就必须在哪里留下记录。
    工程上不该出现「执行到 50%」这种状态——50% 无法恢复，
    因为你不知道那 50% 是哪一半。
    """

    __tablename__ = "task_steps"

    step_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_tasks.task_id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_name: Mapped[str] = mapped_column(String(128), nullable=False)
    step_type: Mapped[str] = mapped_column(String(32), nullable=False, default=StepType.COMPUTE)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StepStatus.PENDING, index=True
    )
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 入参快照。恢复时直接用，不需要重新问模型「上次参数是什么」。
    input_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    #: 出参快照。后续步骤要用。
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    #: 幂等键。写操作必须有，读操作可以为空。
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    #: 外部系统凭证。UNKNOWN 状态下的对账靠它。
    external_reference_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    compensation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CompensationStatus.NOT_REQUIRED
    )
    #: 是否关键步骤。非关键步骤失败 → PARTIAL_SUCCESS，而不是整单失败。
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 依赖的前置步骤名（JSON 数组）。用于 READY 判定与并行调度。
    depends_on: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    #: 下次允许重试的时间。退避策略把它写进来，调度器按它决定何时捞起。
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    task: Mapped[TaskORM] = relationship(back_populates="steps")

    __table_args__ = (
        # 同一任务内步骤名唯一：这保证了幂等键（含 step_name）的稳定性，
        # 也防止恢复时重复插入同名步骤。
        UniqueConstraint("task_id", "step_name", name="uq_step_task_name"),
        Index("ix_step_status_retry", "status", "next_retry_at"),
    )


class ToolExecutionORM(Base):
    """单次工具执行记录。

    为什么和步骤分表：**一个步骤可能包含多次执行**（重试）。
    合并成一张表意味着重试会覆盖上一次的错误信息，
    于是「第一次错在哪」这个最有价值的信息就丢了。

    这张表还承担了**幂等去重表**的职责，见 `idempotency_key` 上的唯一约束。
    """

    __tablename__ = "tool_executions"

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    step_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 幂等键 + 唯一约束 = 真正的去重保证。
    #: 应用层的「先查再写」在并发下是不够的，必须靠数据库约束兜底。
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 参数指纹。用于检测「同一幂等键但参数不同」——这种情况必须拒绝，
    #: 而不是返回旧结果（否则第二笔业务会被静默吞掉）。
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    arguments: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ToolExecutionStatus.IN_FLIGHT)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    external_reference_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime] = _now_column()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    __table_args__ = (
        # 全局唯一：同一个幂等键在整张表里只允许一条记录。
        # 这条约束是「至多一次副作用」的物理保证。
        UniqueConstraint("idempotency_key", name="uq_execution_idempotency_key"),
        Index("ix_execution_step_status", "step_id", "status"),
    )


class ApprovalORM(Base):
    """审批单。

    审批挂起和崩溃恢复本质上是同一件事：**把任务冻在某一步，
    等外部条件满足后再继续。** 所以做好了断点续跑，
    Human-in-the-Loop 几乎是白送的。
    """

    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    step_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 待审批的动作快照（工具名 + 参数）。审批人看到的必须是**将要执行的确切内容**，
    #: 而不是一句模糊的「客服申请了折扣」。
    requested_action: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    requester: Mapped[str] = mapped_column(String(64), nullable=False)
    approver_role: Mapped[str] = mapped_column(String(64), nullable=False)
    approver_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ApprovalStatus.PENDING, index=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default=RiskLevel.MEDIUM)
    created_at: Mapped[datetime] = _now_column()
    #: 审批超时时间。**必须有**：否则任务会永远悬着，没有终态。
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # 同一步骤同一时间只允许一个待审批单：防止重复创建审批把审批人淹没。
        Index("ix_approval_status_expires", "status", "expires_at"),
    )


class AuditEventORM(Base):
    """审计事件。

    审计要能回答两个问题：
    1. 「这一步到底是谁干的」——所以有 actor_type / actor_id；
    2. 「为什么这么判」——所以 payload 里必须带决策依据，而不只是结论。

    **payload 里绝不允许出现未脱敏的个人信息和任何密钥。**
    写入前会统一过一遍 redact + masking。
    """

    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    step_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    created_at: Mapped[datetime] = _now_column()

    __table_args__ = (Index("ix_audit_task_created", "task_id", "created_at"),)


class CheckpointORM(Base):
    """任务检查点快照。

    步骤表本身已经足够支撑恢复，检查点是**额外的一层**，用途有两个：

    1. 调试与回放：能看到「第 3 步结束时整个任务长什么样」；
    2. 复杂编排里的回退点：多 Agent 并行时，聚合前的快照便于人工介入分析。

    检查点不是恢复的必要条件——**恢复的唯一权威是步骤表**。
    这一点必须清楚，否则会出现两份状态互相矛盾时不知道信谁的问题。
    """

    __tablename__ = "task_checkpoints"

    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    step_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = _now_column()


class TokenMappingORM(Base):
    """脱敏代号映射表。

    **这张表是企业内部安全边界的核心资产，绝不能被模型接触到。**

    模型只看到 ``PERSON_8F29A1`` 这样的代号，还原动作由内部程序按权限执行。
    请注意：这不是「反哈希」——哈希本身通常不可逆，
    可恢复的代号之所以能还原，靠的正是这张映射表。
    """

    __tablename__ = "token_mappings"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: 原始值。真实生产环境这一列应该加密存储（KMS 信封加密），
    #: 且访问要有独立审计。Demo 里为了可读性保持明文，已在文档中标注为限制。
    original_value: Mapped[str] = mapped_column(Text, nullable=False)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = _now_column()

    __table_args__ = (
        # 同一个原值只生成一个代号：保证代号在多次调用间稳定，
        # 否则模型会把同一个人当成两个人。
        UniqueConstraint("entity_type", "value_hash", name="uq_token_entity_value"),
    )


# --------------------------------------------------------------------------------------
# 以下是**业务侧**的表（折扣示例）。
# 刻意和框架表放在同一个模块只是为了 Demo 方便；真实项目里业务表应该在自己的模块甚至自己的库里。
# 分界线很清楚：上面的表属于 Agent 框架，下面的表属于业务系统。
# --------------------------------------------------------------------------------------
class CustomerORM(Base):
    """客户主数据（演示用）。"""

    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="STANDARD")
    email: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: 客户归属部门。用于数据范围策略：客服只能操作自己部门的客户。
    department: Mapped[str] = mapped_column(String(64), nullable=False, default="cs_north")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    lifetime_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = _now_column()


class DiscountORM(Base):
    """客户折扣记录（演示用的「外部系统」）。

    这张表扮演的是**外部业务系统**的角色。它有自己的幂等键唯一约束，
    这正是真实下游系统应该提供的能力——
    「同一个键写一百次也只生效一次」不是 Agent 侧能单方面保证的事。
    """

    __tablename__ = "customer_discounts"

    discount_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    discount_rate: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE", index=True)
    #: 下游系统自己的幂等键。Agent 传进来，下游负责去重。
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = _now_column()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_discount_idempotency_key"),
        Index("ix_discount_customer_status", "customer_id", "status"),
    )


class NotificationORM(Base):
    """通知发送记录（演示用的「外部系统」）。"""

    __tablename__ = "notifications"

    notification_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="sms")
    template: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="SENT")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _now_column()

    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_notification_idempotency_key"),)
