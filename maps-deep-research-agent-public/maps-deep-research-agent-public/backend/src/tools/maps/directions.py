"""路线 / 导航工具。"""

from __future__ import annotations

from typing import Any

from ...models import RouteLeg
from ..base import Tool, ToolResult
from .client import GoogleMapsClient
from .results import RoutesPayload


def _map_language(value: Any) -> str | None:
    """把 args 里的 ``zh`` / ``en`` 简称转换成 Maps API 期待的 language code。

    返回 ``None`` 时由 ``GoogleMapsClient`` 自动套用 ``app.maps.default_language_code``。
    """
    if not value:
        return None
    return "zh-CN" if value == "zh" else "en"


class DirectionsTool(Tool):
    name = "directions"
    description = "Compute a route between two locations with optional waypoints."

    def __init__(self, client: GoogleMapsClient) -> None:
        self._client = client

    async def run(self, args: dict[str, Any]) -> ToolResult[RoutesPayload]:
        origin = (args.get("origin") or args.get("from") or "").strip()
        destination = (args.get("destination") or args.get("to") or "").strip()
        if not origin or not destination:
            return ToolResult(error="directions 需要 `origin` 和 `destination`")

        try:
            data, cached = await self._client.directions(
                origin=origin,
                destination=destination,
                mode=args.get("mode") or "driving",
                language=_map_language(args.get("language")),
                waypoints=args.get("waypoints") or None,
            )
        except Exception as exc:  # pragma: no cover
            return ToolResult(error=f"directions 调用失败：{exc}")

        routes_raw = data.get("routes") or []
        if not routes_raw:
            return ToolResult(
                text="未找到可用路线",
                data=RoutesPayload(),
                cached=cached,
            )

        # 取主推路线；它的每个 leg 转成一条 RouteLeg
        primary = routes_raw[0]
        legs: list[RouteLeg] = []
        text_lines = [f"从 **{origin}** 到 **{destination}** ({args.get('mode') or 'driving'}):"]
        for leg in (primary.get("legs") or []):
            dist = leg.get("distance", {})
            dur = leg.get("duration", {})
            leg_model = RouteLeg(
                origin=leg.get("start_address", origin),
                destination=leg.get("end_address", destination),
                mode=args.get("mode") or "driving",
                distance_meters=int(dist.get("value") or 0),
                duration_seconds=int(dur.get("value") or 0),
                polyline=primary.get("overview_polyline", {}).get("points"),
            )
            legs.append(leg_model)
            text_lines.append(
                f"  - {leg_model.origin} → {leg_model.destination}: "
                f"{dist.get('text', '?')} / {dur.get('text', '?')}"
            )

        return ToolResult(
            text="\n".join(text_lines),
            data=RoutesPayload(routes=legs),
            cached=cached,
        )
