"""Places 搜索工具（文本搜索 + 附近搜索两个变体）。

根据 args 选用哪个 Places API endpoint：

* ``lat`` + ``lng`` 同时存在 → Nearby Search（按半径限定）
* 否则                       → Text Search（可带 ``location_hint`` 偏置）

返回 ``ToolResult[PlacesPayload]``；ToolRunner 后续转成 evidence。
"""

from __future__ import annotations

import time
from typing import Any

from ...config import AppConfig
from ...models import Place
from ..base import Tool, ToolResult
from .client import GoogleMapsClient
from .results import PlacesPayload


def _price_level(value: Any) -> int | None:
    """把 Places (New) 的价位枚举归一化成 0-4 整数。"""
    if value is None:
        return None
    mapping = {
        "PRICE_LEVEL_FREE": 0,
        "PRICE_LEVEL_INEXPENSIVE": 1,
        "PRICE_LEVEL_MODERATE": 2,
        "PRICE_LEVEL_EXPENSIVE": 3,
        "PRICE_LEVEL_VERY_EXPENSIVE": 4,
    }
    if isinstance(value, str):
        return mapping.get(value)
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _normalise_place(raw: dict[str, Any]) -> Place | None:
    """把 Places (New) 返回的 JSON 节点转成域模型 ``Place``。

    没有经纬度的条目直接丢掉 —— 前端地图渲不了。
    """
    location = raw.get("location") or {}
    if not location:
        return None
    name = (raw.get("displayName") or {}).get("text") or raw.get("displayName") or ""
    opening = (raw.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []
    photos = raw.get("photos") or []
    photo_ref = photos[0].get("name") if photos else None
    editorial = (raw.get("editorialSummary") or {}).get("text") or None
    # 取前 3 条评价正文，压平换行并截断，喂给 LLM 当"为什么推荐"的定性素材
    reviews: list[str] = []
    for rv in (raw.get("reviews") or [])[:3]:
        text = " ".join(((rv.get("text") or {}).get("text") or "").split())
        if text:
            reviews.append(text[:220])
    return Place(
        place_id=raw.get("id") or raw.get("place_id") or "",
        name=name or "未命名地点",
        address=raw.get("formattedAddress") or raw.get("formatted_address") or "",
        lat=float(location.get("latitude", location.get("lat", 0.0))),
        lng=float(location.get("longitude", location.get("lng", 0.0))),
        rating=raw.get("rating"),
        user_ratings_total=raw.get("userRatingCount"),
        price_level=_price_level(raw.get("priceLevel")),
        categories=list(raw.get("types") or []),
        opening_hours=list(opening),
        website=raw.get("websiteUri"),
        phone=raw.get("nationalPhoneNumber"),
        photo_reference=photo_ref,
        google_maps_url=raw.get("googleMapsUri"),
        editorial_summary=editorial,
        reviews=reviews,
    )


def _quality_score(place: Place) -> float:
    """综合评分：评分 × log(1+评论数)，让"高分且多人验证"的店排在前面。

    无评分的地点给一个很低的分，排到最后但不丢弃。
    """
    import math

    if place.rating is None:
        return -1.0
    reviews = place.user_ratings_total or 0
    return float(place.rating) * math.log1p(reviews)


def _format_places_as_text(places: list[Place]) -> str:
    """把地点列表渲染成给 LLM 摘要器看的多行文本。"""
    if not places:
        return "未找到匹配的地点。"
    lines: list[str] = []
    for idx, place in enumerate(places, start=1):
        rating = f"{place.rating}⭐" if place.rating else "无评分"
        reviews = f"({place.user_ratings_total} 评价)" if place.user_ratings_total else ""
        price = f" 价位{'¥' * place.price_level}" if place.price_level else ""
        block = [
            f"{idx}. **{place.name}** — {rating} {reviews}{price}",
            f"   地址: {place.address}",
            f"   坐标: ({place.lat:.5f},{place.lng:.5f})  链接: {place.google_maps_url or '-'}",
        ]
        if place.editorial_summary:
            block.append(f"   简介: {place.editorial_summary}")
        if place.opening_hours:
            # 完整每周营业时间，供下游判断"那天/那个点关不关门"
            block.append("   营业时间: " + " | ".join(place.opening_hours))
        if place.phone or place.website:
            block.append(f"   联系: {place.phone or ''} {place.website or ''}".rstrip())
        if place.reviews:
            block.append("   真实评价: " + " ‖ ".join(f"“{r}”" for r in place.reviews))
        lines.append("\n".join(block))
    return "\n".join(lines)


class PlacesTool(Tool):
    name = "places"
    description = "Search places via Google Maps Places API (text or nearby)."

    def __init__(self, client: GoogleMapsClient, app_cfg: AppConfig) -> None:
        self._client = client
        self._app = app_cfg

    async def run(self, args: dict[str, Any]) -> ToolResult[PlacesPayload]:
        query = (args.get("query") or "").strip()
        language = args.get("language") or self._app.agent.default_language
        language_code = "zh-CN" if language == "zh" else "en"
        radius = int(args.get("radius") or self._app.maps.default_radius)
        limit = int(args.get("limit") or self._app.maps.default_places_limit)
        open_now = args.get("open_now")

        start = time.perf_counter()
        try:
            if "lat" in args and "lng" in args:
                # 上游 geocoding 给了坐标 → 走 nearby search
                data, cached = await self._client.nearby_search_places(
                    lat=float(args["lat"]),
                    lng=float(args["lng"]),
                    radius_meters=radius,
                    included_types=args.get("included_types"),
                    max_result_count=limit,
                    language_code=language_code,
                )
            else:
                # 没有坐标 → 文本搜索；可选 location_hint 做偏置
                if not query:
                    return ToolResult(error="places 工具至少需要 query 或 lat/lng")
                bias = None
                if args.get("location_hint"):
                    bias = {"circle": {"center": args["location_hint"], "radius": float(radius)}}
                data, cached = await self._client.text_search_places(
                    query=query,
                    location_bias=bias,
                    max_result_count=limit,
                    open_now=bool(open_now) if open_now is not None else None,
                    language_code=language_code,
                )
        except Exception as exc:  # pragma: no cover - 防御
            return ToolResult(error=f"places API 调用失败：{exc}")

        # 把 Places (New) 返回的 JSON 节点归一化成 typed Place 列表
        places: list[Place] = []
        for entry in (data.get("places") or []):
            place = _normalise_place(entry)
            if place is not None:
                places.append(place)

        # 按质量分（评分 × log(1+评论数)）降序，让最值得推荐的店排在最前，
        # 下游摘要 / 报告 / 地图编号都受益于这个顺序。
        places.sort(key=_quality_score, reverse=True)

        duration_ms = int((time.perf_counter() - start) * 1000)
        return ToolResult(
            text=_format_places_as_text(places),
            data=PlacesPayload(places=places, duration_ms=duration_ms),
            cached=cached,
        )
