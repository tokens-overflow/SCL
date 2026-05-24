"""Directions / route tool."""

from __future__ import annotations

from typing import Any

from ...models import RouteLeg
from ..base import Tool, ToolResult
from .client import GoogleMapsClient


class DirectionsTool(Tool):
    name = "directions"
    description = "Compute a route between two locations with optional waypoints."

    def __init__(self, client: GoogleMapsClient) -> None:
        self._client = client

    async def run(self, args: dict[str, Any]) -> ToolResult:
        origin = (args.get("origin") or args.get("from") or "").strip()
        destination = (args.get("destination") or args.get("to") or "").strip()
        if not origin or not destination:
            return ToolResult(error="directions requires `origin` and `destination`")
        mode = args.get("mode") or "driving"
        language = "zh-CN" if (args.get("language") or "zh") == "zh" else "en"
        waypoints = args.get("waypoints") or None

        try:
            data, cached = await self._client.directions(
                origin=origin,
                destination=destination,
                mode=mode,
                language=language,
                waypoints=waypoints,
            )
        except Exception as exc:  # pragma: no cover
            return ToolResult(error=f"directions failed: {exc}")

        routes_raw = data.get("routes") or []
        if not routes_raw:
            return ToolResult(text="未找到可用路线", data={"routes": []}, cached=cached)

        primary = routes_raw[0]
        legs_data = primary.get("legs") or []
        legs: list[RouteLeg] = []
        text_lines = [f"从 **{origin}** 到 **{destination}** ({mode}):"]
        for leg in legs_data:
            dist = leg.get("distance", {})
            dur = leg.get("duration", {})
            leg_model = RouteLeg(
                origin=leg.get("start_address", origin),
                destination=leg.get("end_address", destination),
                mode=mode,
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
            data={"routes": [leg.model_dump() for leg in legs]},
            cached=cached,
        )
