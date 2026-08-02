"""后台调度器。

负责三件周期性的事：

1. **恢复扫描**：捞起非终态任务继续推进。
   进程启动时跑一次，之后按间隔跑——**这是「凌晨三点把挂掉的任务
   捡起来接着跑」的那段代码**。

2. **重试调度**：捞起 `next_retry_at` 已到的步骤。
   退避时间是真的在等，不是阻塞调用方。

3. **审批超时回收**：把超时未决的审批标记为 EXPIRED。
   **没有这一步，等审批的任务会永远悬着，永远没有终态。**

关于实现：用 `asyncio.Task` + 循环，不引入 Celery / APScheduler。
理由是骨架项目应该让人看清机制；真实项目按需替换即可，
`RecoveryService` 和 `ApprovalGate` 的接口不用变。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from app.control.approval_gate import ApprovalGate
from app.core.config import Settings, get_settings
from app.operations.logging import get_logger
from app.runtime.recovery import RecoveryService
from app.state.repositories import ApprovalRepository

logger = get_logger(__name__)


class BackgroundScheduler:
    """周期任务调度器。

    Args:
        recovery: 恢复服务。
        session_factory: 会话工厂。
        settings: 配置对象。
        recovery_interval_seconds: 恢复扫描间隔。
        approval_sweep_interval_seconds: 审批回收间隔。
    """

    def __init__(
        self,
        recovery: RecoveryService,
        session_factory: Any,
        *,
        settings: Settings | None = None,
        recovery_interval_seconds: float = 30.0,
        approval_sweep_interval_seconds: float = 60.0,
    ) -> None:
        self.recovery = recovery
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.recovery_interval = recovery_interval_seconds
        self.approval_sweep_interval = approval_sweep_interval_seconds
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        """启动后台循环。

        Note:
            启动时**立刻跑一次恢复扫描**，不等第一个间隔。
            进程刚重启时正是最可能有悬挂任务的时刻。
        """
        self._stopping.clear()
        await self.run_recovery_once()
        self._tasks = [
            asyncio.create_task(self._loop(self.recovery_interval, self.run_recovery_once)),
            asyncio.create_task(
                self._loop(self.approval_sweep_interval, self.sweep_expired_approvals)
            ),
        ]
        logger.info("scheduler_started", recovery_interval=self.recovery_interval)

    async def stop(self) -> None:
        """停止后台循环。"""
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            # 停止阶段的异常一律忽略：我们正在关闭，没有任何人还会关心它们。
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        logger.info("scheduler_stopped")

    async def _loop(self, interval: float, fn: Any) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # 后台循环绝不能因为一次异常就死掉。
                # 一个死掉的恢复循环意味着从此以后所有挂掉的任务都没人管了，
                # 而且这件事没有任何报错——这是最危险的一类故障。
                logger.exception("scheduler_iteration_failed", job=getattr(fn, "__name__", "?"))

    async def run_recovery_once(self) -> list[dict[str, Any]]:
        """执行一次恢复扫描。"""
        results = await self.recovery.recover_all()
        if results:
            logger.info("recovery_round_finished", recovered=len(results))
        return results

    async def sweep_expired_approvals(self) -> int:
        """回收超时未决的审批单。

        Returns:
            被回收的数量。
        """
        async with self.session_factory() as session:
            gate = ApprovalGate(ApprovalRepository(session), settings=self.settings)
            expired = await gate.expire_overdue()
        if expired:
            logger.warning(
                "approvals_expired",
                count=len(expired),
                approval_ids=[a.approval_id for a in expired],
            )
        return len(expired)
