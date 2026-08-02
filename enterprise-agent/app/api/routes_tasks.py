"""任务相关路由。

入口层的职责很窄：**接收请求、鉴权、调用 Runtime、返回结果**。
它不做业务判断，也不做权限判断——那些在控制层。

一个刻意的设计：`POST /tasks` 是**同步**的，会一直等到任务落到
终态或需要外部输入（审批）为止。这让 Demo 更容易理解。
生产环境应该改成「立刻返回 task_id + 后台推进」，
但那只是入口层的改动，Runtime 一行都不用动——
因为 Runtime 本来就是靠状态表驱动的，不依赖调用方一直在线。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import build_orchestrator, get_session
from app.api.schemas import (
    AuditEventResponse,
    CancelTaskRequest,
    CreateTaskRequest,
    StepResponse,
    TaskDetailResponse,
    TaskResponse,
)
from app.core.errors import AgentError, TaskNotFoundError
from app.operations.logging import get_logger
from app.state.repositories import AuditRepository, TaskRepository

logger = get_logger(__name__)
router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", response_model=TaskDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: CreateTaskRequest,
    session: AsyncSession = Depends(get_session),
) -> TaskDetailResponse:
    """创建并执行一个 Agent 任务。

    返回的任务状态可能是：

    * ``COMPLETED``        全部成功
    * ``PARTIAL_SUCCESS``  关键步骤成功、可选步骤失败（如通知失败）
    * ``WAITING_APPROVAL`` 需要人工审批，已挂起
    * ``FAILED``           被拒绝或执行失败
    * ``MANUAL_REVIEW``    程序判断不了，已转人工
    """
    orchestrator = build_orchestrator(session)
    try:
        task = await orchestrator.start_task(
            user_id=payload.user_id,
            agent_id=payload.agent_id,
            message=payload.message,
            trace_id=payload.trace_id,
        )
    except AgentError as exc:
        # 框架异常转成结构化 HTTP 错误。
        # 注意**只暴露 error_code 和一句人话**，内部细节留在审计里。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": str(exc.error_code), "message": exc.message},
        ) from exc
    return _to_detail(task)


@router.get("/{task_id}", response_model=TaskDetailResponse)
async def get_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
) -> TaskDetailResponse:
    """查询任务详情（含全部步骤）。"""
    repo = TaskRepository(session)
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"error_code": "NOT_FOUND", "message": "任务不存在"})
    return _to_detail(task)


@router.post("/{task_id}/resume", response_model=TaskDetailResponse)
async def resume_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
) -> TaskDetailResponse:
    """从断点恢复任务。

    典型用途：

    * 审批通过后继续执行（**不会重跑已经成功的前置步骤**）；
    * 进程重启后手动触发恢复；
    * 对账完成后继续推进。

    这个接口是幂等的：对已经处于终态的任务调用它不会有任何副作用。
    """
    orchestrator = build_orchestrator(session)
    try:
        task = await orchestrator.resume_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"error_code": "NOT_FOUND", "message": exc.message}) from exc
    except AgentError as exc:
        raise HTTPException(
            status_code=400, detail={"error_code": str(exc.error_code), "message": exc.message}
        ) from exc
    return _to_detail(task)


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: str,
    payload: CancelTaskRequest,
    session: AsyncSession = Depends(get_session),
) -> TaskResponse:
    """取消任务。

    Warning:
        取消**不会自动撤销已生效的副作用**。
        「是否撤销一笔已发放的折扣」必须由明确业务规则或人工决定，
        不能因为一句「取消」就自动回滚一笔已经对客户生效的业务。
    """
    orchestrator = build_orchestrator(session)
    try:
        task = await orchestrator.cancel_task(task_id, actor_id=payload.actor_id, reason=payload.reason)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"error_code": "NOT_FOUND", "message": exc.message}) from exc
    return TaskResponse.model_validate(task.model_dump())


@router.get("/{task_id}/steps", response_model=list[StepResponse])
async def list_steps(
    task_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[StepResponse]:
    """列出任务的全部步骤。

    这是排查问题的第一站：哪一步成功了、哪一步失败了、
    重试了几次、幂等键是什么、外部凭证号是什么，全在这里。
    """
    repo = TaskRepository(session)
    task = await repo.get_task(task_id, with_steps=False)
    if task is None:
        raise HTTPException(status_code=404, detail={"error_code": "NOT_FOUND", "message": "任务不存在"})
    steps = await repo.list_steps(task_id)
    return [StepResponse.model_validate(s.model_dump()) for s in steps]


@router.get("/{task_id}/audit-events", response_model=list[AuditEventResponse])
async def list_audit_events(
    task_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
) -> list[AuditEventResponse]:
    """列出任务的审计事件（时间升序，可直接用于回放）。

    「随机抽一个历史任务，能完整回放吗」是运营层的验收标准之一，
    这个接口就是那个回放入口。
    """
    repo = AuditRepository(session)
    events = await repo.list_by_task(task_id, limit=limit)
    return [AuditEventResponse.model_validate(e.model_dump()) for e in events]


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    user_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[TaskResponse]:
    """分页列出任务。"""
    repo = TaskRepository(session)
    tasks = await repo.list_tasks(user_id=user_id, limit=limit, offset=offset)
    return [TaskResponse.model_validate(t.model_dump()) for t in tasks]


def _to_detail(task) -> TaskDetailResponse:  # noqa: ANN001
    data = task.model_dump()
    data["steps"] = [StepResponse.model_validate(s) for s in data.get("steps", [])]
    return TaskDetailResponse.model_validate(data)
