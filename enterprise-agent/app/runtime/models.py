"""运行时领域模型（Pydantic）。

**为什么要有一套与 ORM 平行的 Pydantic 模型？**

1. ORM 对象绑定在 Session 上。一旦脱离 session，访问未加载的属性就会炸
   （异步下更是直接 `MissingGreenlet`）。领域模型是纯数据，可以自由地在
   各层之间传递、缓存、序列化。
2. 持久化结构和业务语义应该能独立演化。给 ORM 加一个索引列，
   不应该导致 API 契约变化。
3. 领域模型可以带**行为**（如 `is_terminal()`、`next_pending_step()`），
   而这些行为放在 ORM 上会诱导出「在模型里偷偷发起数据库查询」的坏味道。

依赖方向严格单向：`state.models(ORM) → runtime.models(领域) → api.schemas(接口)`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import (
    ACTIONABLE_STEP_STATUSES,
    BLOCKING_STEP_STATUSES,
    TERMINAL_STEP_STATUSES,
    TERMINAL_TASK_STATUSES,
    ActorType,
    ApprovalStatus,
    AuditEventType,
    CompensationStatus,
    RiskLevel,
    StepStatus,
    StepType,
    TaskStatus,
)
from app.core.ids import ensure_utc, utcnow


class _UtcAwareModel(BaseModel):
    """把所有 datetime 字段统一归一化为带 UTC 时区的基类。

    SQLite 读回来的 datetime 是 naive 的（它没有原生时区类型），
    直接与 :func:`~app.core.ids.utcnow` 比较会抛 TypeError。
    在领域模型的**入口**统一归一化，业务代码就永远只面对 aware datetime，
    不需要在每个比较点上写防御代码——那种写法必然会漏掉一两处，
    而漏掉的那一处通常是审批超时判定这类平时跑不到的路径。
    """

    @field_validator("*", mode="after")
    @classmethod
    def _normalize_datetimes(cls, value: object) -> object:
        if isinstance(value, datetime):
            return ensure_utc(value)
        return value


class TaskStep(_UtcAwareModel):
    """任务步骤的领域模型。"""

    model_config = ConfigDict(from_attributes=True)

    step_id: str
    task_id: str
    step_name: str
    step_type: StepType = StepType.COMPUTE
    sequence: int = 0
    status: StepStatus = StepStatus.PENDING
    tool_name: str | None = None
    input_payload: dict[str, Any] | None = None
    output_payload: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    idempotency_key: str | None = None
    external_reference_id: str | None = None
    compensation_status: CompensationStatus = CompensationStatus.NOT_REQUIRED
    critical: bool = True
    depends_on: list[str] = Field(default_factory=list)
    next_retry_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    version: int = 1

    def is_terminal(self) -> bool:
        """步骤是否已到终态（恢复时可直接跳过）。"""
        return self.status in TERMINAL_STEP_STATUSES

    def is_unresolved(self) -> bool:
        """步骤结果是否未知（必须先对账，不能直接重试也不能当失败）。"""
        return self.status in (StepStatus.TIMEOUT, StepStatus.UNKNOWN)

    def has_side_effect(self) -> bool:
        """这一步是否可能已经对外部世界产生了副作用。

        判定依据是**步骤类型**而不是执行状态：
        一个 TIMEOUT 的写操作，很可能已经产生了副作用——
        这正是为什么它不能被简单地当作失败回滚。
        """
        return self.step_type in (StepType.WRITE, StepType.NOTIFY)

    def can_retry(self) -> bool:
        """是否还有重试余额。"""
        return self.retry_count < self.max_retries

    def summary(self) -> str:
        """生成给 LLM 上下文用的一句话摘要（不含敏感原文）。"""
        if self.status == StepStatus.SUCCESS and self.output_payload:
            keys = ", ".join(sorted(self.output_payload)[:5])
            return f"产出字段: {keys}"
        if self.error_message:
            return self.error_message[:120]
        return ""


class AgentTask(_UtcAwareModel):
    """任务的领域模型。"""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    user_id: str
    agent_id: str
    task_type: str = "generic"
    original_input: str = ""
    status: TaskStatus = TaskStatus.CREATED
    risk_level: RiskLevel = RiskLevel.LOW
    current_step: str | None = None
    trace_id: str = ""
    result_payload: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    version: int = 1

    steps: list[TaskStep] = Field(default_factory=list)

    def is_terminal(self) -> bool:
        """任务是否已到终态。

        恢复扫描只捞非终态任务。**每个任务最终都必须落到一个明确终态**，
        否则它会永远悬着——没有终态的任务比失败的任务更麻烦，
        因为没人知道它还在不在跑。
        """
        return self.status in TERMINAL_TASK_STATUSES

    def step_by_name(self, name: str) -> TaskStep | None:
        """按名字查找步骤。"""
        for step in self.steps:
            if step.step_name == name:
                return step
        return None

    def next_actionable_step(self) -> TaskStep | None:
        """返回下一个应该被推进的步骤。

        判定顺序（按 sequence 升序遍历）：

        1. 状态不在 :data:`~app.core.enums.ACTIONABLE_STEP_STATUSES` 里 → 跳过。
           这一条同时覆盖了三种情况：
           已成功（**不重做，也不重新问模型**）、
           已失败并判定完毕（不能反复捞起来重试）、
           正在等待外部条件（审批 / 执行中）。
        2. 依赖的前置步骤未全部成功 → 还不能动。
        3. 其余第一个返回。

        Returns:
            待推进的步骤；没有可推进步骤时返回 ``None``
            （此时调用方还要用 :meth:`has_blocking_steps` 区分
            「全都处理完了」和「有步骤卡在等待中」——
            把后者误判成前者会让一个还在等审批的任务被提前收尾）。
        """
        done_names = {s.step_name for s in self.steps if s.status == StepStatus.SUCCESS}
        for step in sorted(self.steps, key=lambda s: s.sequence):
            if step.status not in ACTIONABLE_STEP_STATUSES:
                continue
            if step.depends_on and not set(step.depends_on).issubset(done_names):
                continue
            return step
        return None

    def has_blocking_steps(self) -> bool:
        """是否存在正在等待外部条件的步骤（审批中 / 执行中 / 补偿中）。"""
        return any(s.status in BLOCKING_STEP_STATUSES for s in self.steps)

    def completed_side_effect_steps(self) -> list[TaskStep]:
        """返回已成功、且产生了外部副作用的步骤（按执行顺序）。

        补偿时需要**逆序**遍历这个列表：后做的先撤。
        """
        return [
            s
            for s in sorted(self.steps, key=lambda s: s.sequence)
            if s.status == StepStatus.SUCCESS and s.has_side_effect()
        ]

    def summarize_steps(self) -> list[dict[str, Any]]:
        """生成步骤概览，用于 API 返回和最终结果汇总。"""
        return [
            {
                "step_name": s.step_name,
                "status": str(s.status),
                "retry_count": s.retry_count,
                "error_code": s.error_code,
                "external_reference_id": s.external_reference_id,
            }
            for s in sorted(self.steps, key=lambda s: s.sequence)
        ]


class ApprovalRequest(_UtcAwareModel):
    """审批单的领域模型。"""

    model_config = ConfigDict(from_attributes=True)

    approval_id: str
    task_id: str
    step_id: str
    requested_action: dict[str, Any] = Field(default_factory=dict)
    requester: str
    approver_role: str
    approver_id: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    reason: str = ""
    decision_comment: str | None = None
    risk_level: RiskLevel = RiskLevel.MEDIUM
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    decided_at: datetime | None = None

    def is_pending(self) -> bool:
        """是否仍在等待人工决策。"""
        return self.status == ApprovalStatus.PENDING

    def is_expired(self, now: datetime | None = None) -> bool:
        """是否已超时。

        审批超时回收是**必须有**的一条：否则任务会永远悬着。
        """
        if self.expires_at is None:
            return False
        return (now or utcnow()) >= self.expires_at


class AuditEvent(_UtcAwareModel):
    """审计事件的领域模型。"""

    model_config = ConfigDict(from_attributes=True)

    event_id: str
    task_id: str | None = None
    step_id: str | None = None
    event_type: AuditEventType | str
    actor_type: ActorType
    actor_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class AgentResult(BaseModel):
    """子 Agent 对上层编排器暴露的统一结果契约。

    **契约里最重要的字段是 `retryable`，而且它必须由被调方声明。**

    只有子 Agent 自己知道「文档缺失」是重试也没用（要人补材料），
    而「向量库连接失败」重试就能好。上层编排器如果靠错误码字符串去猜，
    被调方一改文案就全乱套——这是嵌套编排里最常见的一种耦合。

    后四个字段（cost / trace_id / elapsed_ms / external_reference_id）是运营层要的：
    少了 trace_id 就没法跨 Agent 串链路，少了 cost 就算不出这次编排花了多少钱。
    """

    model_config = ConfigDict(from_attributes=True)

    agent_id: str
    task_id: str
    status: str
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    idempotency_key: str | None = None
    external_reference_id: str | None = None
    cost: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = ""
    elapsed_ms: int = 0
