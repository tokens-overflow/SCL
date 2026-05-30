"""距离矩阵工具 —— 一次性算多起点 × 多终点之间的距离 / 耗时。"""

from __future__ import annotations

from typing import Any

from ..base import Tool, ToolResult
from .client import GoogleMapsClient
from .results import MatrixCell, MatrixPayload


def _as_list(value: Any) -> list[str]:
    """接受单字符串或字符串列表，统一返回字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


class DistanceMatrixTool(Tool):
    name = "distance_matrix"
    description = "Batch distance/duration between multiple origins and destinations."

    def __init__(self, client: GoogleMapsClient) -> None:
        self._client = client

    async def run(self, args: dict[str, Any]) -> ToolResult[MatrixPayload]:
        origins = _as_list(args.get("origins") or args.get("origin"))
        destinations = _as_list(args.get("destinations") or args.get("destination"))
        if not origins or not destinations:
            return ToolResult(error="distance_matrix 需要 origins 和 destinations")

        mode = args.get("mode") or "driving"
        raw_lang = args.get("language")
        language = ("zh-CN" if raw_lang == "zh" else "en") if raw_lang else None

        try:
            data, cached = await self._client.distance_matrix(
                origins=origins,
                destinations=destinations,
                mode=mode,
                language=language,
            )
        except Exception as exc:  # pragma: no cover
            return ToolResult(error=f"distance_matrix 调用失败：{exc}")

        rows = data.get("rows") or []
        matrix: list[list[MatrixCell]] = []
        lines = ["距离矩阵（行=起点，列=终点）："]
        for i, row in enumerate(rows):
            origin = origins[i] if i < len(origins) else f"origin#{i}"
            cells: list[MatrixCell] = []
            line_parts = [f"- {origin}:"]
            for j, element in enumerate(row.get("elements", [])):
                dest = destinations[j] if j < len(destinations) else f"dest#{j}"
                dist = element.get("distance", {})
                dur = element.get("duration", {})
                cells.append(MatrixCell(
                    origin=origin,
                    destination=dest,
                    distance_meters=dist.get("value"),
                    duration_seconds=dur.get("value"),
                    status=element.get("status"),
                ))
                line_parts.append(f"  → {dest}: {dist.get('text', '?')}/{dur.get('text', '?')}")
            matrix.append(cells)
            lines.append("\n".join(line_parts))

        return ToolResult(
            text="\n".join(lines),
            data=MatrixPayload(matrix=matrix, origins=origins, destinations=destinations),
            cached=cached,
        )
