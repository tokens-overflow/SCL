"""Distance matrix tool — batch distance/duration between origins × destinations."""

from __future__ import annotations

from typing import Any

from ..base import Tool, ToolResult
from .client import GoogleMapsClient


class DistanceMatrixTool(Tool):
    name = "distance_matrix"
    description = "Batch distance/duration between multiple origins and destinations."

    def __init__(self, client: GoogleMapsClient) -> None:
        self._client = client

    async def run(self, args: dict[str, Any]) -> ToolResult:
        origins = _as_list(args.get("origins") or args.get("origin"))
        destinations = _as_list(args.get("destinations") or args.get("destination"))
        if not origins or not destinations:
            return ToolResult(error="distance_matrix requires origins and destinations lists")
        mode = args.get("mode") or "driving"
        language = "zh-CN" if (args.get("language") or "zh") == "zh" else "en"

        try:
            data, cached = await self._client.distance_matrix(
                origins=origins,
                destinations=destinations,
                mode=mode,
                language=language,
            )
        except Exception as exc:  # pragma: no cover
            return ToolResult(error=f"distance_matrix failed: {exc}")

        rows = data.get("rows") or []
        lines = ["距离矩阵（行=起点，列=终点）："]
        matrix: list[list[dict[str, Any]]] = []
        for i, row in enumerate(rows):
            origin = origins[i] if i < len(origins) else f"origin#{i}"
            cells: list[dict[str, Any]] = []
            line_parts = [f"- {origin}:"]
            for j, element in enumerate(row.get("elements", [])):
                dest = destinations[j] if j < len(destinations) else f"dest#{j}"
                dist = element.get("distance", {})
                dur = element.get("duration", {})
                cells.append(
                    {
                        "origin": origin,
                        "destination": dest,
                        "distance_meters": dist.get("value"),
                        "duration_seconds": dur.get("value"),
                        "status": element.get("status"),
                    }
                )
                line_parts.append(f"  → {dest}: {dist.get('text', '?')}/{dur.get('text', '?')}")
            matrix.append(cells)
            lines.append("\n".join(line_parts))

        return ToolResult(
            text="\n".join(lines),
            data={"matrix": matrix, "origins": origins, "destinations": destinations},
            cached=cached,
        )


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []
