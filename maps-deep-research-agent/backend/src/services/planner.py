"""Convert a user topic into a validated task DAG via DeepSeek JSON mode."""

from __future__ import annotations

import logging
from typing import Any

from ..llm import LLMClient
from ..models import Language, TaskNode
from ..prompts import planner_messages

logger = logging.getLogger(__name__)

VALID_TOOLS = {"places", "geocoding", "directions", "distance_matrix"}


class PlannerService:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def plan(
        self,
        topic: str,
        *,
        language: Language,
        max_tasks: int,
        location_hint: str | None,
    ) -> list[TaskNode]:
        messages = planner_messages(
            topic=topic,
            language=language,
            max_tasks=max_tasks,
            location_hint=location_hint,
        )
        raw = await self._llm.chat_json(messages)
        tasks_raw = _extract_task_list(raw)
        validated = self._validate(tasks_raw, max_tasks=max_tasks, topic=topic)

        if not validated:
            logger.warning("Planner produced no valid tasks, falling back to single places task.")
            validated = [
                TaskNode(
                    id=1,
                    title="基础搜索" if language == "zh" else "Baseline search",
                    intent=f"对主题 '{topic}' 做基础地点搜索",
                    query=topic,
                    tool="places",
                    tool_args={},
                    depends_on=[],
                )
            ]
        return validated

    # ------------------------------------------------------------------
    def _validate(
        self,
        items: list[dict[str, Any]],
        *,
        max_tasks: int,
        topic: str,
    ) -> list[TaskNode]:
        seen_ids: set[int] = set()
        nodes: list[TaskNode] = []
        for raw_index, raw in enumerate(items[:max_tasks], start=1):
            try:
                tool = (raw.get("tool") or "places").strip()
                if tool not in VALID_TOOLS:
                    logger.info("Replacing invalid tool '%s' with 'places'", tool)
                    tool = "places"

                node = TaskNode(
                    id=int(raw.get("id") or raw_index),
                    title=str(raw.get("title") or f"任务{raw_index}").strip()[:32],
                    intent=str(raw.get("intent") or "").strip() or "围绕主题展开",
                    query=str(raw.get("query") or topic).strip() or topic,
                    tool=tool,  # type: ignore[arg-type]
                    tool_args=dict(raw.get("tool_args") or {}),
                    depends_on=[int(x) for x in (raw.get("depends_on") or []) if isinstance(x, (int, str))],
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Skipping malformed task entry: %s (%s)", raw, exc)
                continue

            if node.id in seen_ids:
                node.id = max(seen_ids) + 1
            seen_ids.add(node.id)

            # Drop dangling dependencies — they'll be re-checked in topo sort.
            node.depends_on = [d for d in node.depends_on if d in seen_ids and d != node.id]
            nodes.append(node)

        return _ensure_topologically_valid(nodes)


def _extract_task_list(raw: dict | list) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        for key in ("tasks", "data", "items", "result"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        # If dict has keys that look like task fields, treat as single-item list.
        if any(k in raw for k in ("title", "query")):
            return [raw]
    return []


def _ensure_topologically_valid(nodes: list[TaskNode]) -> list[TaskNode]:
    """Drop edges that create cycles using a stable Kahn-style check."""
    if not nodes:
        return nodes

    id_to_node = {node.id: node for node in nodes}

    # Drop deps not in id set (shouldn't happen after the seen_ids gate, but be safe).
    for node in nodes:
        node.depends_on = [d for d in node.depends_on if d in id_to_node and d != node.id]

    # Detect cycles by repeated DFS coloring.
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in id_to_node}

    def dfs(nid: int) -> bool:
        """Return True if a cycle is detected starting from nid."""
        color[nid] = GRAY
        for dep in list(id_to_node[nid].depends_on):
            if color[dep] == GRAY:
                # Cycle: drop the offending edge.
                id_to_node[nid].depends_on.remove(dep)
                continue
            if color[dep] == WHITE and dfs(dep):
                return True
        color[nid] = BLACK
        return False

    for nid in list(id_to_node):
        if color[nid] == WHITE:
            dfs(nid)

    return nodes
