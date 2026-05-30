"""阶段 1 —— 规划：研究主题 → 任务 DAG。"""

from __future__ import annotations

import logging

from ..core.events import EventEmitter
from ..llm_tasks.plan_task import PlanInput, PlanTask
from ..models import PlanReadyEvent, ResearchState, StatusEvent

logger = logging.getLogger(__name__)


class PlanStage:
    """通过 :class:`PlanTask` 拆解研究主题、把结果写回 state.tasks。"""

    name = "plan"

    def __init__(self, task: PlanTask, max_tasks_default: int) -> None:
        self._task = task
        self._max_tasks_default = max_tasks_default

    async def run(self, state: ResearchState, emit: EventEmitter) -> None:
        # 状态条立刻更新，让前端 UI 不留空白
        emit(StatusEvent(message="规划研究任务..."))

        # 每次请求允许覆盖 max_tasks，否则用 config 默认
        max_tasks = state.requested_max_tasks or self._max_tasks_default

        state.tasks = await self._task.run(PlanInput(
            topic=state.topic,
            max_tasks=max_tasks,
            location_hint=state.location_hint,
        ))

        # 前端拿到 run_id + 任务列表后开始渲染任务时间线
        emit(PlanReadyEvent(run_id=state.run_id, tasks=state.tasks))
