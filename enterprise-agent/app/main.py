"""FastAPI 应用入口。

启动时做四件事：

1. 初始化日志；
2. 建表（Demo）+ 写入演示数据；
3. 注册内置工具；
4. **跑一轮恢复扫描** —— 进程刚起来时正是最可能有悬挂任务的时刻，
   这一步是「断点续跑」在生产中真正发挥作用的地方。

关闭时停掉后台调度器并释放连接池。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import routes_admin, routes_approvals, routes_tasks
from app.api.deps import build_orchestrator, get_registry
from app.core.config import get_settings
from app.core.errors import AgentError
from app.core.ids import new_trace_id
from app.examples.discount_workflow import seed_demo_data
from app.operations.logging import bind_context, configure_logging, get_logger, reset_context
from app.runtime.events import register_metrics_handlers
from app.runtime.recovery import RecoveryService
from app.runtime.scheduler import BackgroundScheduler
from app.state.database import get_database

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期。"""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    logger.info("app_starting", app_name=settings.app_name, environment=settings.environment)

    db = get_database(settings)
    # Demo 用 create_all；生产环境应改用 Alembic 迁移（见 alembic/）。
    # 两者的表结构定义来自同一份 ORM 模型，不会漂移。
    await db.create_all()

    get_registry()
    await register_metrics_handlers()

    if settings.seed_demo_data:
        async with db.session() as session:
            created = await seed_demo_data(session)
        logger.info("demo_data_seeded", created=created)

    # 恢复服务 + 后台调度器。
    recovery = RecoveryService(
        orchestrator_factory=lambda session: build_orchestrator(session, settings),
        session_factory=db.session,
    )
    scheduler = BackgroundScheduler(recovery, db.session, settings=settings)
    app.state.scheduler = scheduler
    app.state.recovery = recovery

    # 启动时立刻扫一轮：进程刚重启时最可能有悬挂任务。
    await scheduler.start()

    try:
        yield
    finally:
        await scheduler.stop()
        await db.dispose()
        logger.info("app_stopped")


def create_app() -> FastAPI:
    """创建 FastAPI 应用。"""
    settings = get_settings()
    app = FastAPI(
        title="企业级 AI Agent 骨架",
        description=(
            "六层架构 + 六个核心组件的可运行工程骨架。\n\n"
            "核心原则：LLM 只提出结构化动作建议，控制层决定能否执行，"
            "Runtime 是流程主控，状态必须持久化。"
        ),
        version="1.0.0",
        lifespan=lifespan,
        debug=settings.debug,
    )

    app.include_router(routes_tasks.router)
    app.include_router(routes_approvals.router)
    app.include_router(routes_admin.router)

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):  # noqa: ANN001, ANN202
        """为每个请求绑定 trace_id。

        优先复用上游传来的 `X-Trace-Id`——
        **这样 Agent 的链路能和业务系统的链路串起来**。
        少了这一步，你只能看到 Agent 内部发生了什么，
        看不到它是被谁触发的。
        """
        trace_id = request.headers.get("X-Trace-Id") or new_trace_id()
        previous = bind_context(trace_id=trace_id)
        try:
            response = await call_next(request)
        finally:
            reset_context(previous)
        response.headers["X-Trace-Id"] = trace_id
        return response

    @app.exception_handler(AgentError)
    async def agent_error_handler(request: Request, exc: AgentError) -> JSONResponse:
        """统一处理框架异常。

        对外**只给 error_code 和一句人话**。
        缺哪个权限、哪条策略拒的、内部堆栈——那些留在审计里，
        通过 trace_id 可以关联到完整上下文。
        """
        logger.warning(
            "agent_error", error_code=str(exc.error_code), message=exc.message, path=request.url.path
        )
        return JSONResponse(
            status_code=400,
            content={
                "error_code": str(exc.error_code),
                "message": exc.message,
                "trace_id": request.headers.get("X-Trace-Id"),
            },
        )

    return app


app = create_app()
