"""阶段 2 —— 执行：驱动任务 DAG，跑工具，流式生成每个任务的摘要。

整个项目里**唯一一处**同时混合：
    * DAG 调度（并发 / 拓扑序）
    * 工具调用（通过 ToolRunner）
    * LLM 流式（通过 SummarizeTask）
    * SSE 事件分发

其它模块都保持单一职责。所以加"每任务重试 / 计数 / 限流"这种横切需求时，
只会动这一个文件。
"""

from __future__ import annotations

import logging
import time

from ..core.events import EventEmitter
from ..execution.scheduler import DAGScheduler
from ..execution.tool_runner import ToolRunner
from ..llm_tasks.summarize_task import SummarizeInput, SummarizeTask
from ..models import (
    ResearchState,
    SummaryChunkEvent,
    TaskNode,
    TaskUpdateEvent,
    ToolCallEvent,
    ToolResultEvent,
)

logger = logging.getLogger(__name__)


class ExecuteStage:
    name = "execute"

    def __init__(
        self,
        scheduler: DAGScheduler[TaskNode],
        tool_runner: ToolRunner,
        summarize: SummarizeTask,
    ) -> None:
        self._scheduler = scheduler
        self._tool_runner = tool_runner
        self._summarize = summarize

    async def run(self, state: ResearchState, emit: EventEmitter) -> None:
        if not state.tasks:
            return

        # 给 ToolRunner 用的反查表：依赖提示（上游 lat/lng 等）需要按 id 查找
        id_to_node = {t.id: t for t in state.tasks}

        async def run_one(node: TaskNode) -> None:
            await self._run_task(node, state, id_to_node, emit)

        await self._scheduler.run(state.tasks, run_one)

    # ------------------------------------------------------------------ 单任务
    async def _run_task(
        self,
        node: TaskNode,
        state: ResearchState,
        id_to_node: dict[int, TaskNode],
        emit: EventEmitter,
    ) -> None:
        """单任务生命周期：准备 args → 调工具 → LLM 流式总结 → 标记完成。

        三个失败点（下方分别注释 A / B / C）：
            (A) 工具名未知
            (B) 工具返回 error
            (C) 摘要流式过程中异常
        """
        node.status = "in_progress"
        node.started_at = time.time()
        emit(TaskUpdateEvent(task_id=node.id, status="in_progress"))

        # ── 1. 算出最终 args（合并上游依赖提示 + 默认值）──
        args = self._tool_runner.prepare_args(node, state, id_to_node)
        emit(ToolCallEvent(
            task_id=node.id,
            tool=node.tool,
            request=ToolRunner.redact_args(args),
        ))

        # ── 2. 实际跑工具 ──
        result, duration_ms = await self._tool_runner.execute(node, args)
        if not result.ok:
            # 失败 (A) 或 (B) —— 处理方式相同
            self._finalize_failed(node, result.error or "tool failed", emit, duration_ms)
            return

        emit(ToolResultEvent(
            task_id=node.id,
            tool=node.tool,
            place_count=len(node.evidence.places),
            route_count=len(node.evidence.routes),
            duration_ms=duration_ms,
        ))

        # ── 3. 流式总结，每个 chunk 通过 SSE 转发到前端 ──
        summary = await self._stream_summary(node, state, result.text or "（无文本上下文）", emit)
        if summary is None:
            return  # 失败 (C) 已经在内部 emit 了

        # ── 4. 标记完成（或 "skipped"：没拿到任何有用证据时）──
        node.summary = summary or "暂无可用信息"
        node.status = "completed" if (node.evidence.places or node.evidence.routes) else "skipped"
        node.finished_at = time.time()
        emit(TaskUpdateEvent(
            task_id=node.id,
            status=node.status,
            summary=node.summary,
            evidence=node.evidence,
        ))

    async def _stream_summary(
        self,
        node: TaskNode,
        state: ResearchState,
        evidence_block: str,
        emit: EventEmitter,
    ) -> str | None:
        """流式拉取摘要、累积 chunk；返回完整字符串或异常时返回 None。"""
        chunks: list[str] = []

        def forward(chunk: str) -> None:
            chunks.append(chunk)
            emit(SummaryChunkEvent(task_id=node.id, content=chunk))

        try:
            await self._summarize.stream(
                SummarizeInput(
                    topic=state.topic,
                    task_title=node.title,
                    task_intent=node.intent,
                    evidence_block=evidence_block,
                ),
                on_chunk=forward,
            )
        except Exception as exc:  # pragma: no cover - 防御
            logger.exception("任务 %s 摘要流式失败", node.id)
            self._finalize_failed(node, str(exc), emit)
            return None

        return "".join(chunks).strip()

    @staticmethod
    def _finalize_failed(
        node: TaskNode,
        error: str,
        emit: EventEmitter,
        duration_ms: int = 0,
    ) -> None:
        """统一的"任务失败"收尾：写 state、发 SSE 事件。"""
        node.status = "failed"
        node.error = error
        node.finished_at = time.time()
        emit(ToolResultEvent(
            task_id=node.id,
            tool=node.tool,
            duration_ms=duration_ms,
            error=error,
        ))
        emit(TaskUpdateEvent(task_id=node.id, status="failed", detail=error))
