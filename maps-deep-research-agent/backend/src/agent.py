"""Top-level orchestrator: plan → execute (DAG) → report + itinerary.

The orchestrator wires together planner / executor / reporter / itinerary
services around the shared LLM and Maps tool registry. It exposes both a
synchronous ``run`` and an asynchronous ``run_stream`` API mirroring chapter 14
but with typed SSE events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator

from .config import Configuration
from .llm import LLMUsage, build_llm_client
from .models import (
    BaseEvent,
    DoneEvent,
    ErrorEvent,
    Event,
    PlanReadyEvent,
    ReportEvent,
    ResearchState,
    StatusEvent,
    UsageEvent,
    UsageSnapshot,
)
from .services.executor import TaskExecutor
from .services.itinerary import ItineraryService
from .services.planner import PlannerService
from .services.reporter import ReporterService
from .tools.maps import register_default_maps_tools

logger = logging.getLogger(__name__)


class MapsDeepResearchAgent:
    def __init__(self, config: Configuration) -> None:
        config.assert_ready()
        self._config = config
        self._usage = LLMUsage()
        # 根据 LLMConfig.yaml 中 active 字段实例化对应 provider
        self._llm = build_llm_client(config.llm_config_path, usage=self._usage)
        self._registry, self._maps_client, self._cache = register_default_maps_tools(config)
        self._planner = PlannerService(llm=self._llm)
        self._executor = TaskExecutor(
            registry=self._registry,
            llm=self._llm,
            concurrency=config.task_concurrency,
        )
        self._reporter = ReporterService(llm=self._llm)
        self._itinerary = ItineraryService(llm=self._llm)

    @property
    def usage_snapshot(self) -> UsageSnapshot:
        snap = self._usage.snapshot()
        return UsageSnapshot(
            llm_prompt_tokens=snap["llm_prompt_tokens"],
            llm_completion_tokens=snap["llm_completion_tokens"],
            maps_api_calls=self._maps_client.api_calls,
            cache_hits=self._cache.hits,
        )

    async def aclose(self) -> None:
        await self._maps_client.aclose()

    # ------------------------------------------------------------------
    async def run(
        self,
        topic: str,
        *,
        language: str | None = None,
        max_tasks: int | None = None,
        location_hint: str | None = None,
    ) -> ResearchState:
        """Synchronous end-to-end run; returns the final state."""
        state = ResearchState(
            topic=topic,
            language=language or self._config.default_language,  # type: ignore[arg-type]
        )
        state.tasks = await self._planner.plan(
            topic=topic,
            language=state.language,
            max_tasks=max_tasks or self._config.max_tasks,
            location_hint=location_hint,
        )

        await self._executor.execute(state, emit=_noop)

        report_task = asyncio.create_task(self._reporter.generate(state))
        itinerary_task = asyncio.create_task(self._itinerary.build(state))
        state.report_markdown, (state.itinerary, state.map_overview) = await asyncio.gather(
            report_task, itinerary_task
        )
        state.finished_at = time.time()
        return state

    # ------------------------------------------------------------------
    async def run_stream(
        self,
        topic: str,
        *,
        language: str | None = None,
        max_tasks: int | None = None,
        location_hint: str | None = None,
    ) -> AsyncIterator[Event]:
        """Stream typed events while the workflow executes."""
        state = ResearchState(
            topic=topic,
            language=language or self._config.default_language,  # type: ignore[arg-type]
        )

        queue: asyncio.Queue[Event | None] = asyncio.Queue()

        def emit(event: BaseEvent) -> None:
            queue.put_nowait(event)

        async def producer() -> None:
            try:
                emit(StatusEvent(message="规划研究任务..." if state.language == "zh" else "Planning..."))
                state.tasks = await self._planner.plan(
                    topic=topic,
                    language=state.language,
                    max_tasks=max_tasks or self._config.max_tasks,
                    location_hint=location_hint,
                )
                emit(PlanReadyEvent(run_id=state.run_id, tasks=state.tasks))

                await self._executor.execute(state, emit=emit)

                emit(
                    StatusEvent(
                        message="生成最终报告..." if state.language == "zh" else "Composing report..."
                    )
                )
                report_task = asyncio.create_task(self._reporter.generate(state))
                itinerary_task = asyncio.create_task(self._itinerary.build(state))
                state.report_markdown, (state.itinerary, state.map_overview) = await asyncio.gather(
                    report_task, itinerary_task
                )
                state.finished_at = time.time()

                emit(
                    ReportEvent(
                        markdown=state.report_markdown,
                        itinerary=state.itinerary,
                        map_overview=state.map_overview,
                    )
                )

                snap = self.usage_snapshot
                emit(
                    UsageEvent(
                        llm_prompt_tokens=snap.llm_prompt_tokens,
                        llm_completion_tokens=snap.llm_completion_tokens,
                        maps_api_calls=snap.maps_api_calls,
                        elapsed_seconds=(state.finished_at or time.time()) - state.started_at,
                    )
                )
            except Exception as exc:
                logger.exception("Streaming run failed")
                emit(ErrorEvent(detail=str(exc)))
            finally:
                emit(DoneEvent())
                queue.put_nowait(None)

        producer_task = asyncio.create_task(producer())

        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            await producer_task


def _noop(_event: object) -> None:
    pass
