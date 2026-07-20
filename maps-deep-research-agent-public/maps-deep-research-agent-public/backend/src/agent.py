"""顶层编排器。把 Pipeline 装起来跑的薄外观（facade）。

新人阅读顺序：
    1. ``core/pipeline.py``  —— Stage 协议 + Pipeline 类
    2. ``stages/*``          —— 三个具体阶段 (plan / execute / compose)
    3. ``llm_tasks/*``       —— 各 stage 用到的 LLM 用例（prompt + parse）
    4. ``execution/*``       —— ExecuteStage 用到的 DAG 调度 + 工具执行
    5. ``tools/maps/*``      —— Maps API 客户端 + typed 返回值

本文件几乎不做事：把上面这些零件组装一次，对 FastAPI 层暴露 ``run`` /
``run_stream`` 两个入口。
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from .config import Configuration
from .core.events import noop_emitter
from .core.pipeline import Pipeline
from .execution.scheduler import DAGScheduler
from .execution.tool_runner import ToolRunner
from .llm import LLMUsage, build_llm_client
from .llm_tasks.itinerary_task import ItineraryTask
from .llm_tasks.plan_task import PlanTask
from .llm_tasks.report_task import ReportTask
from .llm_tasks.summarize_task import SummarizeTask
from .models import (
    DoneEvent,
    ErrorEvent,
    Event,
    ResearchState,
    TaskNode,
    UsageSnapshot,
)
from .stages.compose_stage import ComposeStage
from .stages.execute_stage import ExecuteStage
from .stages.plan_stage import PlanStage
from .tools.maps import register_default_maps_tools

logger = logging.getLogger(__name__)


class MapsDeepResearchAgent:
    """持有 Pipeline + 共享基础设施（LLM 客户端、Maps 工具、缓存）。

    在 FastAPI 进程里是单例（由 main.py 持有）。``run`` 与 ``run_stream``
    都委托给**同一条** Pipeline，差别仅在传给 stage 的 emitter 不同。
    """

    def __init__(self, config: Configuration) -> None:
        config.assert_ready()
        self._config = config

        # ── 共享基础设施（Agent 生命周期内只创建一次）──
        self._usage = LLMUsage()
        self._llm = build_llm_client(config, usage=self._usage)
        registry, self._maps_client, self._cache = register_default_maps_tools(config.app)

        # ── 组装 Pipeline：每个 stage 只拿到自己需要的零件 ──
        scheduler = DAGScheduler[TaskNode](
            concurrency=config.app.agent.task_concurrency,
            stall_timeout_seconds=config.app.scheduler.task_stall_timeout_seconds,
        )
        tool_runner = ToolRunner(registry=registry)

        self._pipeline = Pipeline([
            PlanStage(
                task=PlanTask(self._llm),
                max_tasks_default=config.app.agent.max_tasks,
            ),
            ExecuteStage(
                scheduler=scheduler,
                tool_runner=tool_runner,
                summarize=SummarizeTask(self._llm),
            ),
            ComposeStage(
                report=ReportTask(
                    self._llm,
                    temperature=config.app.llm_defaults.temperatures.report,
                ),
                itinerary=ItineraryTask(
                    self._llm,
                    temperature=config.app.llm_defaults.temperatures.itinerary,
                ),
                usage_snapshot=lambda: self.usage_snapshot,
            ),
        ])

    # ------------------------------------------------------------------ 快照
    @property
    def usage_snapshot(self) -> UsageSnapshot:
        """累计 token / Maps 调用次数 / cache 命中数。"""
        snap = self._usage.snapshot()
        return UsageSnapshot(
            llm_prompt_tokens=snap["llm_prompt_tokens"],
            llm_completion_tokens=snap["llm_completion_tokens"],
            maps_api_calls=self._maps_client.api_calls,
            cache_hits=self._cache.hits,
        )

    async def aclose(self) -> None:
        await self._maps_client.aclose()

    # ------------------------------------------------------------------ 对外 API
    async def run(
        self,
        topic: str,
        *,
        language: str | None = None,
        max_tasks: int | None = None,
        location_hint: str | None = None,
        budget: str | None = None,
        travel_date: str | None = None,
    ) -> ResearchState:
        """同步端到端：事件全部丢弃，返回最终 state。"""
        state = self._make_state(
            topic, language, max_tasks, location_hint, budget, travel_date
        )
        await self._pipeline.execute(state, noop_emitter)
        return state

    async def run_stream(
        self,
        topic: str,
        *,
        language: str | None = None,
        max_tasks: int | None = None,
        location_hint: str | None = None,
        budget: str | None = None,
        travel_date: str | None = None,
    ) -> AsyncIterator[Event]:
        """流式：把每个事件按发生顺序 yield 出来（FastAPI 侧推到 SSE）。

        模式：一个 producer 协程把事件塞进 asyncio.Queue，本生成器从队列消费
        并 yield，遇到 ``None`` sentinel 表示流结束。
        """
        state = self._make_state(
            topic, language, max_tasks, location_hint, budget, travel_date
        )
        queue: asyncio.Queue[Event | None] = asyncio.Queue()

        async def producer() -> None:
            try:
                await self._pipeline.execute(state, queue.put_nowait)
            except Exception as exc:
                logger.exception("Pipeline 异常")
                queue.put_nowait(ErrorEvent(detail=str(exc)))
            finally:
                queue.put_nowait(DoneEvent())
                queue.put_nowait(None)  # 流结束标志

        producer_task = asyncio.create_task(producer())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            await producer_task

    # ------------------------------------------------------------------ 内部
    def _make_state(
        self,
        topic: str,
        language: str | None,
        max_tasks: int | None,
        location_hint: str | None,
        budget: str | None = None,
        travel_date: str | None = None,
    ) -> ResearchState:
        return ResearchState(
            topic=topic,
            language=language or self._config.app.agent.default_language,  # type: ignore[arg-type]
            location_hint=location_hint,
            requested_max_tasks=max_tasks,
            budget=budget,
            travel_date=travel_date,
        )
