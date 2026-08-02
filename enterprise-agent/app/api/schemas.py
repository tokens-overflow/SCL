"""API 请求 / 响应模型。

**为什么 API 模型要和领域模型分开？**

因为它们的演化速度不同。领域模型跟着业务逻辑走，
API 模型跟着对外契约走。合并之后，任何一次内部重构都可能
意外地改变对外接口——而调用方是不会读你的 commit message 的。

另外 API 层还承担一个安全职责：**决定哪些字段可以出去**。
比如 `TaskDetailResponse` 里没有 `original_input` 的完整原文，
也没有策略评估的内部细节——那些留在审计里，不对外。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ======================================================================
# 请求
# ======================================================================
class CreateTaskRequest(BaseModel):
    """POST /tasks 请求体。"""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64, examples=["user_001"])
    agent_id: str = Field(min_length=1, max_length=64, examples=["discount_agent"])
    message: str = Field(
        min_length=1, max_length=4000, examples=["给客户 C001 打九折，并通知客户。"]
    )
    #: 上游链路 ID。**应该传**——这样 Agent 的 trace 能和业务系统的串起来。
    trace_id: str | None = None


class ApprovalDecisionRequest(BaseModel):
    """审批决策请求体。"""

    model_config = ConfigDict(extra="forbid")

    approver_id: str = Field(min_length=1, max_length=64, examples=["manager_001"])
    comment: str | None = Field(default=None, max_length=500)


class CancelTaskRequest(BaseModel):
    """取消任务请求体。"""

    model_config = ConfigDict(extra="forbid")

    actor_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=500)


# ======================================================================
# 响应
# ======================================================================
class StepResponse(BaseModel):
    """步骤视图。"""

    model_config = ConfigDict(from_attributes=True)

    step_id: str
    step_name: str
    step_type: str
    sequence: int
    status: str
    tool_name: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    error_code: str | None = None
    error_message: str | None = None
    #: 幂等键对外暴露是有意的：客服排查时需要它去下游系统对账。
    idempotency_key: str | None = None
    external_reference_id: str | None = None
    compensation_status: str = "NOT_REQUIRED"
    critical: bool = True
    started_at: datetime | None = None
    completed_at: datetime | None = None


class TaskResponse(BaseModel):
    """任务视图。"""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    user_id: str
    agent_id: str
    task_type: str
    status: str
    risk_level: str
    current_step: str | None = None
    trace_id: str
    result_payload: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    version: int


class TaskDetailResponse(TaskResponse):
    """任务详情（含步骤）。"""

    steps: list[StepResponse] = Field(default_factory=list)


class AuditEventResponse(BaseModel):
    """审计事件视图。"""

    model_config = ConfigDict(from_attributes=True)

    event_id: str
    task_id: str | None = None
    step_id: str | None = None
    event_type: str
    actor_type: str
    actor_id: str
    payload: dict[str, Any]
    trace_id: str
    created_at: datetime


class ApprovalResponse(BaseModel):
    """审批单视图。"""

    model_config = ConfigDict(from_attributes=True)

    approval_id: str
    task_id: str
    step_id: str
    requested_action: dict[str, Any]
    requester: str
    approver_role: str
    approver_id: str | None = None
    status: str
    reason: str
    decision_comment: str | None = None
    risk_level: str
    created_at: datetime
    expires_at: datetime | None = None
    decided_at: datetime | None = None


class ToolResponse(BaseModel):
    """工具描述视图。

    Note:
        **不包含 `required_permissions`。**
        内部权限点名称对调用方没有意义，暴露出去只会扩大信息面。
    """

    name: str
    description: str
    risk_level: str
    idempotent: bool
    supports_compensation: bool
    step_type: str
    arguments_schema: dict[str, Any]


class HealthResponse(BaseModel):
    """健康检查响应。"""

    status: str
    app_name: str
    environment: str
    llm_provider: str
    database: str
    registered_tools: int
    pending_tasks: dict[str, int] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """统一错误响应。

    对外只给 error_code 和一句人话，**不给内部细节**（缺哪个权限、
    哪条策略拒的、内部堆栈）。那些留在审计里，
    通过 trace_id 可以关联到完整上下文。
    """

    error_code: str
    message: str
    trace_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RecoveryResponse(BaseModel):
    """恢复结果响应。"""

    scanned: int
    results: list[dict[str, Any]] = Field(default_factory=list)
