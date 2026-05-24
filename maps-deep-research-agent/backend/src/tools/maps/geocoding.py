"""Address ↔ coordinates resolver."""

from __future__ import annotations

from typing import Any

from ..base import Tool, ToolResult
from .client import GoogleMapsClient


class GeocodingTool(Tool):
    name = "geocoding"
    description = "Translate an address or place name into latitude/longitude."

    def __init__(self, client: GoogleMapsClient) -> None:
        self._client = client

    async def run(self, args: dict[str, Any]) -> ToolResult:
        address = (args.get("query") or args.get("address") or "").strip()
        if not address:
            return ToolResult(error="geocoding requires `query` (an address)")
        language = "zh-CN" if (args.get("language") or "zh") == "zh" else "en"

        try:
            data, cached = await self._client.geocode(address=address, language=language)
        except Exception as exc:  # pragma: no cover
            return ToolResult(error=f"geocoding failed: {exc}")

        results = data.get("results") or []
        if not results:
            return ToolResult(text="未找到匹配地址", data={"results": []}, cached=cached)

        normalized = []
        lines = []
        for idx, item in enumerate(results[:5], start=1):
            loc = item.get("geometry", {}).get("location", {})
            entry = {
                "formatted_address": item.get("formatted_address"),
                "lat": loc.get("lat"),
                "lng": loc.get("lng"),
                "place_id": item.get("place_id"),
            }
            normalized.append(entry)
            lines.append(
                f"{idx}. {entry['formatted_address']} — ({entry['lat']:.5f},{entry['lng']:.5f})"
            )

        return ToolResult(text="\n".join(lines), data={"results": normalized}, cached=cached)
