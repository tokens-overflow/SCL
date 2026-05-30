"""阶段 3 —— 汇总：并行生成最终 Markdown 报告 + JSON 行程。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from ..core.events import EventEmitter
from ..llm_tasks.itinerary_task import ItineraryInput, ItineraryTask
from ..llm_tasks.report_task import ReportInput, ReportTask
from ..models import ReportEvent, ResearchState, StatusEvent, UsageEvent, UsageSnapshot

logger = logging.getLogger(__name__)


class ComposeStage:
    """并行跑 Report + Itinerary 两个 LLM 任务，最后发 Report + Usage 事件。"""

    name = "compose"

    def __init__(
        self,
        report: ReportTask,
        itinerary: ItineraryTask,
        usage_snapshot: Callable[[], UsageSnapshot],
    ) -> None:
        self._report = report
        self._itinerary = itinerary
        # 传 callable 而不是已计算的值：本阶段也会消耗 token，
        # 等本阶段跑完才取 snapshot 拿到的数字才准。
        self._usage_snapshot = usage_snapshot

    async def run(self, state: ResearchState, emit: EventEmitter) -> None:
        emit(StatusEvent(message="生成最终报告..."))

        # ── 两个 LLM 调用输入互不相关，可以并行 ──
        report_coro = self._report.run(ReportInput(
            topic=state.topic,
            tasks=state.tasks,
        ))
        itinerary_coro = self._safe_itinerary(state)
        state.report_markdown, (state.itinerary, state.map_overview) = await asyncio.gather(
            report_coro, itinerary_coro
        )
        state.finished_at = time.time()

        # ── 终态事件：完整报告 + 累计用量 ──
        emit(ReportEvent(
            markdown=state.report_markdown,
            itinerary=state.itinerary,
            map_overview=state.map_overview,
        ))

        snap = self._usage_snapshot()
        emit(UsageEvent(
            llm_prompt_tokens=snap.llm_prompt_tokens,
            llm_completion_tokens=snap.llm_completion_tokens,
            maps_api_calls=snap.maps_api_calls,
            elapsed_seconds=(state.finished_at or time.time()) - state.started_at,
        ))

    async def _safe_itinerary(self, state: ResearchState):
        """Itinerary 走 JSON 模式，LLM 偶尔会返回非法 JSON。

        失败时不能拖垮整份报告 —— 兜底成空 days + 仅从证据算出的地图概览。
        """
        try:
            return await self._itinerary.run(ItineraryInput(
                topic=state.topic,
                tasks=state.tasks,
            ))
        except Exception:
            logger.exception("行程生成失败，使用空 days 兜底")
            from ..llm_tasks.itinerary_task import _evidence_for_itinerary, _map_overview
            _, places = _evidence_for_itinerary(state.tasks)
            return [], _map_overview(places)
