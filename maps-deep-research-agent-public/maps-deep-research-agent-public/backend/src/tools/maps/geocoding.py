"""地址 ↔ 经纬度解析工具。"""

from __future__ import annotations

from typing import Any

from ..base import Tool, ToolResult
from .client import GoogleMapsClient
from .results import GeocodePayload, GeocodeResult


class GeocodingTool(Tool):
    name = "geocoding"
    description = "Translate an address or place name into latitude/longitude."

    def __init__(self, client: GoogleMapsClient) -> None:
        self._client = client

    async def run(self, args: dict[str, Any]) -> ToolResult[GeocodePayload]:
        address = (args.get("query") or args.get("address") or "").strip()
        if not address:
            return ToolResult(error="geocoding 需要 `query`（一个地址或地名）")

        # 仅当 caller 显式传了 language hint 时才做 zh→zh-CN 转换；
        # 否则把 None 传下去，让 client 套用 app.maps.default_language_code。
        raw_lang = args.get("language")
        language = ("zh-CN" if raw_lang == "zh" else "en") if raw_lang else None

        try:
            data, cached = await self._client.geocode(address=address, language=language)
        except Exception as exc:  # pragma: no cover
            return ToolResult(error=f"geocoding 调用失败：{exc}")

        raw_results = data.get("results") or []
        if not raw_results:
            return ToolResult(text="未找到匹配地址", data=GeocodePayload(), cached=cached)

        # 只保留前 5 条（Google 默认按相关度排序，靠前的最准）
        normalized: list[GeocodeResult] = []
        lines: list[str] = []
        for idx, item in enumerate(raw_results[:5], start=1):
            loc = item.get("geometry", {}).get("location", {})
            lat = loc.get("lat")
            lng = loc.get("lng")
            if lat is None or lng is None:
                continue
            entry = GeocodeResult(
                formatted_address=item.get("formatted_address") or "",
                lat=float(lat),
                lng=float(lng),
                place_id=item.get("place_id"),
            )
            normalized.append(entry)
            lines.append(
                f"{idx}. {entry.formatted_address} — ({entry.lat:.5f},{entry.lng:.5f})"
            )

        return ToolResult(
            text="\n".join(lines),
            data=GeocodePayload(results=normalized),
            cached=cached,
        )
