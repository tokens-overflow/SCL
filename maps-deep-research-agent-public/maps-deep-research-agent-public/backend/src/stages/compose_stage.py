"""阶段 3 —— 汇总：并行生成最终 Markdown 报告 + JSON 行程。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from ..core.events import EventEmitter
from ..llm_tasks.itinerary_task import (
    ItineraryInput,
    ItineraryTask,
    _evidence_for_itinerary,
    _map_overview,
)
from ..llm_tasks.report_task import ReportInput, ReportTask
from ..models import ReportEvent, ResearchState, StatusEvent, UsageEvent, UsageSnapshot
from ..tools.weather import fetch_weather

logger = logging.getLogger(__name__)


def _trip_context(state: ResearchState) -> str:
    """把出行日期 / 预算拼成一行给 LLM 的上下文（都为空则返回空串）。"""
    parts: list[str] = []
    if state.travel_date:
        parts.append(f"出行日期/时间：{state.travel_date}")
    if state.budget:
        parts.append(f"预算：{state.budget}")
    return "；".join(parts)


async def _weather_for_state(state: ResearchState) -> str:
    """取所有地点的几何中心作为锚点，拉一段近期天气预报（失败返回空串）。"""
    _text, places = _evidence_for_itinerary(state.tasks)
    overview = _map_overview(places)
    center = overview.get("center") if overview else None
    if not center:
        return ""
    return await fetch_weather(center["lat"], center["lng"])


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
        emit(StatusEvent(message="查天气、生成最终报告与行程..."))

        # 天气只拉一次，报告与行程共用；出行日期/预算上下文同理
        weather = await _weather_for_state(state)
        trip_context = _trip_context(state)

        # ── 两个 LLM 调用输入互不相关，可以并行 ──
        report_coro = self._report.run(ReportInput(
            topic=state.topic,
            tasks=state.tasks,
            weather=weather,
            trip_context=trip_context,
        ))
        itinerary_coro = self._safe_itinerary(state, weather, trip_context)
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

    async def _safe_itinerary(self, state: ResearchState, weather: str, trip_context: str):
        """Itinerary 走 JSON 模式，LLM 偶尔会返回非法 JSON。

        失败时不能拖垮整份报告 —— 兜底成空 days + 仅从证据算出的地图概览。
        """
        try:
            return await self._itinerary.run(ItineraryInput(
                topic=state.topic,
                tasks=state.tasks,
                weather=weather,
                trip_context=trip_context,
            ))
        except Exception:
            logger.exception("行程生成失败，使用空 days 兜底")
            _, places = _evidence_for_itinerary(state.tasks)
            return [], _map_overview(places)
