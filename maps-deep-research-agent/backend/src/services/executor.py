"""DAG-aware task executor.

Algorithm (Kahn-style ready queue):

1. Track ``remaining_deps[id]`` and ``children[id]``.
2. Maintain a semaphore-bounded pool of in-flight tasks.
3. When a task completes, decrement deps for its children; push any whose
   counter reaches zero onto the ready queue.

Compared to chapter 14 which fan-outs *all* tasks immediately, this respects
ordering: a `places` task that depends on a `geocoding` task will see the
resolved coordinates passed as ``tool_args`` overrides.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from ..llm import LLMClient
from ..models import (
    Place,
    ResearchState,
    RouteLeg,
    SummaryChunkEvent,
    TaskEvidence,
    TaskNode,
    TaskUpdateEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from ..prompts import summarizer_messages
from ..tools.base import ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


class TaskExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        llm: LLMClient,
        concurrency: int,
    ) -> None:
        self._registry = registry
        self._llm = llm
        self._concurrency = max(1, concurrency)

    # ------------------------------------------------------------------
    async def execute(
        self,
        state: ResearchState,
        emit: Callable[[Any], None],
    ) -> None:
        """Run all tasks honouring depends_on. ``emit`` receives Event models."""
        if not state.tasks:
            return

        id_to_node = {t.id: t for t in state.tasks}
        remaining_deps = {t.id: len(t.depends_on) for t in state.tasks}
        children: dict[int, list[int]] = {t.id: [] for t in state.tasks}
        for node in state.tasks:
            for dep in node.depends_on:
                if dep in children:
                    children[dep].append(node.id)

        ready: asyncio.Queue[int] = asyncio.Queue()
        for nid, count in remaining_deps.items():
            if count == 0:
                ready.put_nowait(nid)

        pending = len(state.tasks)
        sem = asyncio.Semaphore(self._concurrency)
        in_flight: set[asyncio.Task] = set()
        completed_lock = asyncio.Lock()

        async def runner(node: TaskNode) -> None:
            async with sem:
                await self._run_single(state, node, id_to_node, emit)
            async with completed_lock:
                for child_id in children.get(node.id, []):
                    remaining_deps[child_id] -= 1
                    if remaining_deps[child_id] == 0:
                        await ready.put(child_id)

        while pending > 0:
            # Drain everything currently ready.
            try:
                next_id = await asyncio.wait_for(ready.get(), timeout=30.0)
            except asyncio.TimeoutError:  # pragma: no cover - defensive
                logger.error("Executor stalled: no tasks ready and %d pending", pending)
                # Force-resolve to break a deadlock from broken dep declarations.
                stuck = [nid for nid, c in remaining_deps.items() if c > 0]
                for nid in stuck:
                    remaining_deps[nid] = 0
                    await ready.put(nid)
                continue

            task = asyncio.create_task(runner(id_to_node[next_id]))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
            pending -= 1

        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)

    # ------------------------------------------------------------------
    async def _run_single(
        self,
        state: ResearchState,
        node: TaskNode,
        all_nodes: dict[int, TaskNode],
        emit: Callable[[Any], None],
    ) -> None:
        node.status = "in_progress"
        node.started_at = time.time()
        emit(TaskUpdateEvent(task_id=node.id, status="in_progress"))

        # Resolve dependency hints (e.g. geocoding upstream -> lat/lng downstream).
        tool_args = _merge_dependency_hints(node, all_nodes)
        tool_args.setdefault("language", state.language)
        if "query" not in tool_args and node.query:
            tool_args["query"] = node.query

        emit(
            ToolCallEvent(
                task_id=node.id,
                tool=node.tool,
                request=_redact_args(tool_args),
            )
        )

        start = time.perf_counter()
        try:
            tool = self._registry.get(node.tool)
        except KeyError as exc:
            node.status = "failed"
            node.error = str(exc)
            node.finished_at = time.time()
            emit(
                TaskUpdateEvent(task_id=node.id, status="failed", detail=str(exc))
            )
            return

        result: ToolResult = await tool.run(tool_args)
        duration_ms = int((time.perf_counter() - start) * 1000)

        if not result.ok:
            node.status = "failed"
            node.error = result.error
            node.finished_at = time.time()
            emit(
                ToolResultEvent(
                    task_id=node.id,
                    tool=node.tool,
                    duration_ms=duration_ms,
                    error=result.error,
                )
            )
            emit(TaskUpdateEvent(task_id=node.id, status="failed", detail=result.error))
            return

        evidence = _build_evidence(node.tool, result)
        node.evidence = evidence

        emit(
            ToolResultEvent(
                task_id=node.id,
                tool=node.tool,
                place_count=len(evidence.places),
                route_count=len(evidence.routes),
                duration_ms=duration_ms,
            )
        )

        # ----- Stream the summary --------------------------------------
        evidence_block = result.text or "（无文本上下文）"
        messages = summarizer_messages(
            topic=state.topic,
            task_title=node.title,
            task_intent=node.intent,
            evidence_block=evidence_block,
            language=state.language,  # type: ignore[arg-type]
        )
        chunks: list[str] = []
        try:
            async for chunk in self._llm.stream_chat(messages):
                if not chunk:
                    continue
                chunks.append(chunk)
                emit(SummaryChunkEvent(task_id=node.id, content=chunk))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Summary stream failed for task %s", node.id)
            node.status = "failed"
            node.error = str(exc)
            node.finished_at = time.time()
            emit(TaskUpdateEvent(task_id=node.id, status="failed", detail=str(exc)))
            return

        node.summary = "".join(chunks).strip() or "暂无可用信息"
        node.status = "completed" if evidence.places or evidence.routes else "skipped"
        node.finished_at = time.time()
        emit(
            TaskUpdateEvent(
                task_id=node.id,
                status=node.status,
                summary=node.summary,
                evidence=node.evidence,
            )
        )


# ---------------------------------------------------------------------------
def _build_evidence(tool: str, result: ToolResult) -> TaskEvidence:
    evidence = TaskEvidence()
    data = result.data or {}

    places_raw = data.get("places") or []
    for entry in places_raw:
        try:
            evidence.places.append(Place(**entry))
        except Exception:  # pragma: no cover - defensive
            continue

    routes_raw = data.get("routes") or []
    for entry in routes_raw:
        try:
            evidence.routes.append(RouteLeg(**entry))
        except Exception:  # pragma: no cover - defensive
            continue

    # Geocoding doesn't produce places per se — push a "Place" with empty
    # rating for the first result so downstream views can pin it.
    if tool == "geocoding":
        for entry in data.get("results") or []:
            if entry.get("lat") is None:
                continue
            evidence.places.append(
                Place(
                    place_id=entry.get("place_id") or entry.get("formatted_address") or "geo",
                    name=entry.get("formatted_address") or "Geocoded location",
                    address=entry.get("formatted_address") or "",
                    lat=float(entry["lat"]),
                    lng=float(entry["lng"]),
                )
            )

    if result.cached:
        evidence.notes.append("cache_hit")
    return evidence


def _merge_dependency_hints(node: TaskNode, all_nodes: dict[int, TaskNode]) -> dict[str, Any]:
    """Override ``tool_args`` with information from completed dependencies."""
    args = dict(node.tool_args)
    if not node.depends_on:
        return args

    # Use the first completed dep that yielded geo coordinates as a location bias.
    if node.tool in {"places", "distance_matrix"} and ("lat" not in args or "lng" not in args):
        for dep_id in node.depends_on:
            dep = all_nodes.get(dep_id)
            if not dep or dep.status != "completed":
                continue
            for place in dep.evidence.places:
                args.setdefault("lat", place.lat)
                args.setdefault("lng", place.lng)
                break
            if "lat" in args:
                break

    # For directions: if origin/destination missing, pick from dep places.
    if node.tool == "directions":
        anchor_places: list[Place] = []
        for dep_id in node.depends_on:
            dep = all_nodes.get(dep_id)
            if dep and dep.evidence.places:
                anchor_places.extend(dep.evidence.places)
        if anchor_places:
            args.setdefault("origin", anchor_places[0].name)
            if len(anchor_places) > 1:
                args.setdefault("destination", anchor_places[1].name)

    return args


def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Strip any large/sensitive blobs before emitting tool_call events."""
    redacted: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            redacted[key] = value
        elif isinstance(value, list):
            redacted[key] = value[:8]
        elif isinstance(value, dict):
            redacted[key] = {k: v for k, v in list(value.items())[:8]}
    return redacted
