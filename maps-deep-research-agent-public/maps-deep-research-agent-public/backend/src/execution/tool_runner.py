"""单任务的工具调用：args 合并 → 调工具 → 转 typed evidence。

职责切分：
    * ExecuteStage  —— 编排 DAG + LLM 流式总结 + SSE 事件分发；
    * **ToolRunner** —— 给一个 TaskNode：合并好参数、跑对应 Tool、把 typed
      payload 归一化成 :class:`TaskEvidence`。

把 evidence 构造放在这里有两点好处：
    (a) 让 ExecuteStage 的主循环保持薄；
    (b) 用 ``isinstance(payload, XxxPayload)`` 分派替代原来的"按工具名 if/else"。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..models import Place, ResearchState, TaskEvidence, TaskNode
from ..tools.base import ToolRegistry, ToolResult
from ..tools.maps.results import (
    GeocodePayload,
    MatrixPayload,
    PlacesPayload,
    RoutesPayload,
)

logger = logging.getLogger(__name__)


def _place_ref(place: Place) -> str:
    """把一个地点转成 Maps directions/matrix 能消费的引用字符串。

    优先用地点名（让矩阵/路线输出里的标签可读，便于 LLM 对应到具体餐厅）；
    名字缺失时退回精确坐标。
    """
    name = (place.name or "").strip()
    if name and name != "未命名地点":
        return name
    if place.lat and place.lng:
        return f"{place.lat},{place.lng}"
    return name or "unknown"


class ToolRunner:
    """无状态的单任务工具驱动器（除了持有 registry 引用）。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    # ------------------------------------------------------------------ 对外 API
    def prepare_args(
        self,
        node: TaskNode,
        state: ResearchState,
        all_nodes: dict[int, TaskNode],
    ) -> dict[str, Any]:
        """为当前任务算出最终要喂给 Tool 的 args。

        顺序：
            1. 以 planner 给的 ``node.tool_args`` 为基底；
            2. 补齐 ``language`` / ``query`` 等通用默认值；
            3. 从已完成的上游依赖里"借" 经纬度 / 地点名等提示（比如下游
               places 任务沿用上游 geocoding 任务拿到的 lat/lng）。
        """
        args = self._merge_dependency_hints(node, all_nodes)
        args.setdefault("language", state.language)
        if "query" not in args and node.query:
            args["query"] = node.query
        return args

    async def execute(
        self,
        node: TaskNode,
        args: dict[str, Any],
    ) -> tuple[ToolResult[Any], int]:
        """实际调用 ``node.tool`` 指定的工具。

        返回 (result, duration_ms)。成功时把 evidence 填进 ``node.evidence``；
        失败时（工具名未知 / 工具返回 error）保持 evidence 为空，由 caller 处理。
        """
        try:
            tool = self._registry.get(node.tool)
        except KeyError as exc:
            return ToolResult(error=str(exc)), 0

        start = time.perf_counter()
        result: ToolResult[Any] = await tool.run(args)
        duration_ms = int((time.perf_counter() - start) * 1000)

        if result.ok:
            node.evidence = self._to_evidence(result)
        return result, duration_ms

    @staticmethod
    def redact_args(args: dict[str, Any]) -> dict[str, Any]:
        """发 ``tool_call`` SSE 事件前对长字段做截断，避免前端被淹。"""
        redacted: dict[str, Any] = {}
        for key, value in args.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                redacted[key] = value
            elif isinstance(value, list):
                redacted[key] = value[:8]
            elif isinstance(value, dict):
                redacted[key] = {k: v for k, v in list(value.items())[:8]}
        return redacted

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _merge_dependency_hints(
        node: TaskNode,
        all_nodes: dict[int, TaskNode],
    ) -> dict[str, Any]:
        """从已完成的上游任务里挑出能复用的字段，注入当前任务 args。"""
        args = dict(node.tool_args)
        if not node.depends_on:
            return args

        # ── places / distance_matrix：从上游（通常是 geocoding）继承 lat/lng ──
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

        # ── distance_matrix：候选地点是上游 places 任务运行时才发现的，planner
        #    无法预先写进 tool_args，这里从依赖证据里合成 origins / destinations。
        #    约定：单点依赖（通常是 geocoding 锚点）= 起点；多点依赖 = 终点候选。
        if node.tool == "distance_matrix":
            has_origin = bool(args.get("origins") or args.get("origin"))
            has_dest = bool(args.get("destinations") or args.get("destination"))
            if not has_origin or not has_dest:
                dep_groups: list[list[Place]] = []
                for dep_id in node.depends_on:
                    dep = all_nodes.get(dep_id)
                    if dep and dep.status == "completed" and dep.evidence.places:
                        dep_groups.append(list(dep.evidence.places))
                if dep_groups:
                    flat = [p for group in dep_groups for p in group]
                    candidates = max(dep_groups, key=len)
                    anchor_group = next((g for g in dep_groups if len(g) == 1), None)
                    anchor = anchor_group[0] if anchor_group else flat[0]
                    if not has_origin:
                        args["origins"] = [_place_ref(anchor)]
                    if not has_dest:
                        # 终点排除起点自身，并限制数量以满足 Maps 元素上限
                        dests = [_place_ref(p) for p in candidates if p.place_id != anchor.place_id]
                        args["destinations"] = (dests or [_place_ref(p) for p in candidates])[:10]

        # ── directions：从上游 place 名里挑 origin / destination ──
        if node.tool == "directions":
            anchor_places: list[Place] = []
            for dep_id in node.depends_on:
                dep = all_nodes.get(dep_id)
                if dep and dep.evidence.places:
                    anchor_places.extend(dep.evidence.places)
            if anchor_places:
                args.setdefault("origin", _place_ref(anchor_places[0]))
                if len(anchor_places) > 1:
                    args.setdefault("destination", _place_ref(anchor_places[1]))

        return args

    @staticmethod
    def _to_evidence(result: ToolResult[Any]) -> TaskEvidence:
        """按 typed payload 分派构造 TaskEvidence（替代原来的 if-else 大杂烩）。

        每个 Tool 返回 ``tools/maps/results.py`` 里的一种 *Payload 类型，
        本函数为每种类型给出一条对应的归一化路径。
        """
        evidence = TaskEvidence()
        payload = result.data

        if isinstance(payload, PlacesPayload):
            evidence.places = list(payload.places)
        elif isinstance(payload, RoutesPayload):
            evidence.routes = list(payload.routes)
        elif isinstance(payload, GeocodePayload):
            # Geocoding 不是 Place，但要让下游（前端地图、行程）能统一渲染，
            # 这里把每个结果"伪装"成只有名字 + 坐标的 Place 钉子。
            for r in payload.results:
                evidence.places.append(
                    Place(
                        place_id=r.place_id or r.formatted_address or "geo",
                        name=r.formatted_address or "Geocoded location",
                        address=r.formatted_address,
                        lat=r.lat,
                        lng=r.lng,
                    )
                )
        elif isinstance(payload, MatrixPayload):
            # Distance Matrix 不映射到 Place / Route；只在 notes 里留痕，
            # Reporter 可以引用。
            evidence.notes.append(
                f"matrix: {len(payload.origins)} origins × {len(payload.destinations)} destinations"
            )

        if result.cached:
            evidence.notes.append("cache_hit")
        return evidence
