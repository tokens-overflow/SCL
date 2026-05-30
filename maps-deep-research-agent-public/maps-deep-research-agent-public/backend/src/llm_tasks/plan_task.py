"""LLM 用例：研究主题 → 校验过的 TaskNode DAG。

input：``PlanInput``（topic / max_tasks / location_hint）
output：``list[TaskNode]``（拓扑合法，无环）

实现关键：
    1. ``build_messages`` 把 input 转成 Planner Prompt。
    2. ``parse`` 把 LLM JSON 输出做容错抽取 + 字段校验 + 拓扑修复。
       即使 LLM 返回完全无效，也会 fallback 到单任务版本，保证下游能跑。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from ..core.llm_task import JsonLLMTask
from ..models import TaskNode
from ..prompts import planner_messages

logger = logging.getLogger(__name__)

# Planner 允许选用的 tool 名集合。
# 超出此集合的工具一律被改写为 "places"，避免下游 KeyError。
VALID_TOOLS = {"places", "geocoding", "directions", "distance_matrix"}


class PlanInput(BaseModel):
    topic: str
    max_tasks: int
    location_hint: str | None = None


class PlanTask(JsonLLMTask[PlanInput, list[TaskNode]]):
    """把研究主题拆成一组带依赖关系的、无环的 TaskNode。"""

    def build_messages(self, input: PlanInput) -> list[dict[str, str]]:
        return planner_messages(
            topic=input.topic,
            max_tasks=input.max_tasks,
            location_hint=input.location_hint,
        )

    def parse(self, raw: Any, input: PlanInput) -> list[TaskNode]:
        items = _extract_task_list(raw)
        validated = self._validate(items, topic=input.topic, max_tasks=input.max_tasks)
        if not validated:
            # LLM 没给出任何可用任务时的兜底：单 places 任务保证下游链路能跑
            logger.warning("Planner 没产出任何有效任务，使用单 places 任务兜底")
            validated = [_fallback_task(input)]
        return validated

    # ------------------------------------------------------------------ 私有
    @staticmethod
    def _validate(
        items: list[dict[str, Any]],
        *,
        max_tasks: int,
        topic: str,
    ) -> list[TaskNode]:
        """逐项构造 TaskNode，做字段清洗 + id 去重 + 悬空依赖剔除。"""
        seen_ids: set[int] = set()
        nodes: list[TaskNode] = []

        for raw_index, raw in enumerate(items[:max_tasks], start=1):
            try:
                tool = (raw.get("tool") or "places").strip()
                if tool not in VALID_TOOLS:
                    logger.info("把无效工具 '%s' 改为 'places'", tool)
                    tool = "places"

                node = TaskNode(
                    id=int(raw.get("id") or raw_index),
                    title=str(raw.get("title") or f"任务{raw_index}").strip()[:32],
                    intent=str(raw.get("intent") or "").strip() or "围绕主题展开",
                    query=str(raw.get("query") or topic).strip() or topic,
                    tool=tool,  # type: ignore[arg-type]
                    tool_args=dict(raw.get("tool_args") or {}),
                    depends_on=[
                        int(x) for x in (raw.get("depends_on") or [])
                        if isinstance(x, (int, str))
                    ],
                )
            except Exception as exc:  # pragma: no cover - 防御
                logger.warning("跳过格式异常的任务项：%s (%s)", raw, exc)
                continue

            # id 冲突时确定性地分配新 id（下一可用最大值）
            if node.id in seen_ids:
                node.id = max(seen_ids) + 1
            seen_ids.add(node.id)

            # 剔除指向不存在 / 自身的依赖；拓扑环由 _ensure_topologically_valid 处理
            node.depends_on = [d for d in node.depends_on if d in seen_ids and d != node.id]
            nodes.append(node)

        return _ensure_topologically_valid(nodes)


# ---------------------------------------------------------------------------
# 辅助函数（自由函数：方便单测，纯逻辑无依赖）
# ---------------------------------------------------------------------------
def _extract_task_list(raw: Any) -> list[dict[str, Any]]:
    """从 LLM 输出里取出 list[dict]，容忍多种包装形态。

    支持：
    * 直接是 ``[{...},...]``
    * ``{"tasks": [...]}`` / ``{"items": [...]}`` / ``{"data": [...]}`` 等常见包装
    * 整个就是一个任务对象 ``{"title":..,"query":..}`` —— 视作单元素列表
    """
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        for key in ("tasks", "data", "items", "result"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if any(k in raw for k in ("title", "query")):
            return [raw]
    return []


def _ensure_topologically_valid(nodes: list[TaskNode]) -> list[TaskNode]:
    """用 DFS 染色检测环，发现回边就删除（稳定，不改变其它顺序）。"""
    if not nodes:
        return nodes

    id_to_node = {node.id: node for node in nodes}

    # 安全网：剔除指向不存在 id / 指向自身的依赖
    for node in nodes:
        node.depends_on = [d for d in node.depends_on if d in id_to_node and d != node.id]

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in id_to_node}

    def dfs(nid: int) -> bool:
        color[nid] = GRAY
        for dep in list(id_to_node[nid].depends_on):
            if color[dep] == GRAY:
                # 回边 → 环：删除这条边
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


def _fallback_task(input: PlanInput) -> TaskNode:
    """LLM 完全没给出可用任务时的兜底单任务。"""
    return TaskNode(
        id=1,
        title="基础搜索",
        intent=f"对主题 '{input.topic}' 做基础地点搜索",
        query=input.topic,
        tool="places",
        tool_args={},
        depends_on=[],
    )
