"""审批相关路由。

审批接口的三个设计要点：

1. **幂等**：重复点「批准」不会产生额外效果。
   审批回调可能被重复投递（人手抖、消息重投）。
2. **四眼原则**：审批人不能是发起人，由 ApprovalGate 强制。
3. **审批通过后不自动执行**：需要显式调用 `POST /tasks/{id}/resume`。
   这样做的好处是审批和执行解耦——审批系统挂了不影响执行，
   执行失败也不需要重新审批。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import build_orchestrator, get_session
from app.api.routes_tasks import _to_detail
from app.api.schemas import ApprovalDecisionRequest, ApprovalResponse, TaskDetailResponse
from app.control.approval_gate import ApprovalGate
from app.core.enums import ApprovalStatus
from app.core.errors import AgentError, ApprovalNotFoundError, PermissionDeniedError
from app.state.repositories import ApprovalRepository

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=list[ApprovalResponse])
async def list_approvals(
    status_filter: str | None = Query(default=None, alias="status"),
    approver_role: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[ApprovalResponse]:
    """列出审批单（默认按创建时间倒序）。"""
    repo = ApprovalRepository(session)
    parsed: ApprovalStatus | None = None
    if status_filter:
        try:
            parsed = ApprovalStatus(status_filter.upper())
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error_code": "INVALID_ARGUMENT", "message": f"未知审批状态：{status_filter}"},
            ) from exc
    approvals = await repo.list_approvals(status=parsed, approver_role=approver_role, limit=limit)
    return [ApprovalResponse.model_validate(a.model_dump()) for a in approvals]


@router.get("/{approval_id}", response_model=ApprovalResponse)
async def get_approval(
    approval_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApprovalResponse:
    """查询单个审批单。

    审批人看到的 `requested_action` 是**将要执行的确切内容**
    （已通过全部参数校验的最终参数），不是一句模糊的描述。
    他批准的，就是后来执行的那一个。
    """
    repo = ApprovalRepository(session)
    approval = await repo.get(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail={"error_code": "NOT_FOUND", "message": "审批单不存在"})
    return ApprovalResponse.model_validate(approval.model_dump())


@router.post("/{approval_id}/approve", response_model=TaskDetailResponse)
async def approve(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    session: AsyncSession = Depends(get_session),
) -> TaskDetailResponse:
    """批准审批单，并从断点继续执行任务。

    **不会重跑已经成功的前置步骤**——这正是断点续跑的价值：
    查询客户那一步早就成功了，批准之后只会从折扣那一步继续。
    """
    return await _decide(approval_id, payload, approved=True, session=session)


@router.post("/{approval_id}/reject", response_model=TaskDetailResponse)
async def reject(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    session: AsyncSession = Depends(get_session),
) -> TaskDetailResponse:
    """驳回审批单，任务落 FAILED。"""
    return await _decide(approval_id, payload, approved=False, session=session)


async def _decide(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    *,
    approved: bool,
    session: AsyncSession,
) -> TaskDetailResponse:
    repo = ApprovalRepository(session)
    gate = ApprovalGate(repo)
    try:
        approval = await gate.decide(
            approval_id, approved=approved, approver_id=payload.approver_id, comment=payload.comment
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"error_code": "NOT_FOUND", "message": exc.message}) from exc
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error_code": str(exc.error_code), "message": exc.message},
        ) from exc
    except AgentError as exc:
        raise HTTPException(
            status_code=400, detail={"error_code": str(exc.error_code), "message": exc.message}
        ) from exc

    # 审批已记录 → 从断点恢复任务。
    orchestrator = build_orchestrator(session)
    task = await orchestrator.resume_task(approval.task_id)
    return _to_detail(task)
