"""运维与管理路由。

这些接口服务于**运营层**：工具清单、健康检查、指标、恢复触发。

一个重要的边界：`/tools` 返回的是工具的**自描述**，
不包含 `required_permissions`。内部权限点名称对调用方没有意义，
暴露出去只会扩大信息面。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import build_orchestrator, get_registry, get_session
from app.api.schemas import HealthResponse, RecoveryResponse, ToolResponse
from app.core.config import get_settings
from app.operations.metrics import metrics
from app.state.repositories import TaskRepository

router = APIRouter(tags=["admin"])


@router.get("/health", response_model=HealthResponse)
async def health(session: AsyncSession = Depends(get_session)) -> HealthResponse:
    """健康检查。

    除了「进程还活着」，还回答一个更有用的问题：
    **有多少任务卡在非终态**。这是 Agent 系统健康度最直接的指标——
    比 CPU 和内存有用得多。

    注意 WAITING_APPROVAL 的任务是「正常等待」，不是「卡住」，
    所以在响应里是分开列的。
    """
    settings = get_settings()
    repo = TaskRepository(session)
    pending = await repo.list_resumable_tasks(limit=1000)
    counts: dict[str, int] = {}
    for task in pending:
        counts[str(task.status)] = counts.get(str(task.status), 0) + 1

    return HealthResponse(
        status="ok",
        app_name=settings.app_name,
        environment=settings.environment,
        llm_provider=settings.llm_provider,
        # 只暴露方言，不暴露连接串（里面有密码）。
        database=settings.database_url.split("://")[0],
        registered_tools=len(get_registry().all_tools()),
        pending_tasks=counts,
    )


@router.get("/tools", response_model=list[ToolResponse])
async def list_tools() -> list[ToolResponse]:
    """列出全部已注册工具。

    这是「不允许模型调用未注册工具」的另一面：
    工具清单是**封闭且可枚举**的。任何不在这个列表里的名字，
    控制层都会拒绝。
    """
    registry = get_registry()
    return [ToolResponse.model_validate(desc) for desc in registry.describe_all()]


@router.get("/admin/metrics")
async def get_metrics() -> dict[str, object]:
    """导出运行指标。

    包含成功率、延迟分位、重试/审批/补偿次数，
    以及**按任务归因的 LLM 成本**——
    「这次编排到底花了多少钱」是运营层的基本问题。
    """
    return metrics.snapshot()


@router.get("/admin/config")
async def get_config() -> dict[str, object]:
    """返回**脱敏后**的配置快照。

    任何名字里含 key / secret / token / password 的字段
    只会输出 ``"***set***"``，绝不输出原值。
    """
    return get_settings().safe_dump()


@router.post("/admin/recover", response_model=RecoveryResponse)
async def trigger_recovery(
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> RecoveryResponse:
    """手动触发一轮恢复扫描。

    平时由 Scheduler 周期执行；这个接口用于运维手动介入，
    或者在演示「进程重启后断点续跑」时使用。
    """
    repo = TaskRepository(session)
    pending = await repo.list_resumable_tasks(limit=limit)
    results: list[dict[str, object]] = []

    for task in pending:
        # WAITING_APPROVAL 的任务是正常等待，跳过不打扰。
        if str(task.status) == "WAITING_APPROVAL":
            results.append(
                {"task_id": task.task_id, "skipped": "等待审批中", "status": str(task.status)}
            )
            continue
        orchestrator = build_orchestrator(session)
        try:
            after = await orchestrator.resume_task(task.task_id)
            results.append(
                {
                    "task_id": task.task_id,
                    "from_status": str(task.status),
                    "to_status": str(after.status),
                    "ok": True,
                }
            )
        except Exception as exc:  # noqa: BLE001
            # 一个任务恢复失败不能阻止其它任务被恢复。
            results.append({"task_id": task.task_id, "ok": False, "error": str(exc)})

    return RecoveryResponse(scanned=len(pending), results=results)


@router.get("/admin/identities")
async def list_identities() -> dict[str, object]:
    """列出 Demo 身份，方便试用时挑选 user_id。

    Note:
        这是**演示接口**，生产环境必须移除——
        枚举身份是攻击者的第一步。
    """
    from app.security.identity import default_identity_provider

    provider = default_identity_provider
    return {
        "users": [
            {
                "user_id": u.user_id,
                "display_name": u.display_name,
                "roles": sorted(u.roles),
                "data_scopes": sorted(u.data_scopes),
            }
            for u in provider._users.values()  # noqa: SLF001 - 演示接口
        ],
        "agents": [
            {
                "agent_id": a.agent_id,
                "display_name": a.display_name,
                "allowed_tools": sorted(a.allowed_tools),
                "max_risk_level": a.max_risk_level,
            }
            for a in provider._agents.values()  # noqa: SLF001
        ],
    }
