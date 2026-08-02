"""断点续跑（Recovery）。

**为什么断点续跑不能依赖模型记忆？**

因为模型的「记忆」是上下文窗口里的文本。它会被截断、会过期、
会因为一次对话重置而清零，而且它对「我上次到底有没有执行成功」
这个问题的回答本质上是**生成**，不是**读取**——模型会流畅地告诉你
一个听起来很合理的答案，而那个答案可能是错的。

状态表不会。它就在那儿，`SELECT * FROM task_steps WHERE task_id = ?`，
每次读出来都一样。

所以这个模块从头到尾**没有一次模型调用**。

恢复的完整算法（对应架构文档第十四节）：

    1. 按 task_id 读出任务状态
    2. 读出所有步骤状态
    3. 已 SUCCESS 的步骤直接跳过
    4. RUNNING 且长时间无更新 → 标记 UNKNOWN / TIMEOUT
    5. 写操作查询外部系统真实状态（对账）
    6. 可重试步骤安排重试
    7. 不可重试步骤 → FAILED 或 MANUAL_REVIEW
    8. WAITING_APPROVAL 的任务继续等待
    9. 需要补偿的流程执行补偿
    10. 从正确的步骤继续执行
"""

from __future__ import annotations

from typing import Any

from app.core.enums import TaskStatus
from app.operations.logging import get_logger
from app.operations.metrics import metrics
from app.runtime.models import AgentTask
from app.runtime.orchestrator import Orchestrator
from app.state.repositories import TaskRepository

logger = get_logger(__name__)


class RecoveryService:
    """扫描并恢复所有非终态任务。

    Args:
        orchestrator_factory: 一个可调用对象，接收 session 返回 Orchestrator。
            **为什么不直接传 Orchestrator**：每个任务的恢复应该在
            自己的事务里进行，一个任务恢复失败不能影响其它任务。
        session_factory: 会话工厂（`Database.session`）。
    """

    def __init__(self, orchestrator_factory: Any, session_factory: Any) -> None:
        self.orchestrator_factory = orchestrator_factory
        self.session_factory = session_factory

    async def recover_all(self, limit: int = 100) -> list[dict[str, Any]]:
        """恢复所有非终态任务。

        通常在**进程启动时**调用一次，之后由 Scheduler 周期调用。

        Args:
            limit: 单次处理的任务数上限。

        Returns:
            每个任务的恢复结果摘要。

        Note:
            **每个任务用独立的会话和事务。**
            如果所有任务共用一个事务，一个任务恢复时抛异常会导致
            整批回滚——包括那些本来已经恢复成功的。
        """
        async with self.session_factory() as session:
            repo = TaskRepository(session)
            pending = await repo.list_resumable_tasks(limit=limit)

        logger.info("recovery_scan", candidate_count=len(pending))
        results: list[dict[str, Any]] = []

        for task in pending:
            results.append(await self.recover_one(task.task_id))
        return results

    async def recover_one(self, task_id: str) -> dict[str, Any]:
        """恢复单个任务。

        Args:
            task_id: 任务 ID。

        Returns:
            恢复结果摘要 ``{task_id, from_status, to_status, ok, error}``。

        Note:
            恢复失败**不抛异常**，而是记录在返回值里。
            一个损坏的任务不应该阻止其它任务被恢复——
            否则一条脏数据就能让整个恢复流程瘫痪。
        """
        async with self.session_factory() as session:
            repo = TaskRepository(session)
            before = await repo.get_task(task_id)
            if before is None:
                return {"task_id": task_id, "ok": False, "error": "任务不存在"}
            if before.is_terminal():
                return {
                    "task_id": task_id,
                    "from_status": str(before.status),
                    "to_status": str(before.status),
                    "ok": True,
                    "skipped": "已是终态",
                }

            orchestrator: Orchestrator = self.orchestrator_factory(session)
            try:
                after = await orchestrator.resume_task(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("recovery_failed", task_id=task_id)
                metrics.increment("agent_recovery_total", outcome="error")
                return {
                    "task_id": task_id,
                    "from_status": str(before.status),
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }

        metrics.increment("agent_recovery_total", outcome="ok")
        logger.info(
            "task_recovered",
            task_id=task_id,
            from_status=str(before.status),
            to_status=str(after.status),
            skipped_steps=[s.step_name for s in after.steps if str(s.status) == "SUCCESS"],
        )
        return {
            "task_id": task_id,
            "from_status": str(before.status),
            "to_status": str(after.status),
            "ok": True,
            # 已成功的步骤被跳过了——这正是断点续跑的价值所在：
            # 不重复执行已经生效的写操作。
            "skipped_successful_steps": [
                s.step_name for s in after.steps if str(s.status) == "SUCCESS"
            ],
        }

    async def summarize_pending(self) -> dict[str, int]:
        """统计各状态下的待恢复任务数（运维视图）。"""
        async with self.session_factory() as session:
            repo = TaskRepository(session)
            tasks: list[AgentTask] = await repo.list_resumable_tasks(limit=1000)
        counts: dict[str, int] = {}
        for task in tasks:
            counts[str(task.status)] = counts.get(str(task.status), 0) + 1
        # WAITING_APPROVAL 的任务是「正常等待」，不是「卡住」。
        # 把它们分开统计，避免运维看到一堆非终态任务就以为系统出问题了。
        counts.setdefault(str(TaskStatus.WAITING_APPROVAL), 0)
        return counts
