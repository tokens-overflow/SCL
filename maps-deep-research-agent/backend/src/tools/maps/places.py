"""Places search tool (text + nearby variants).

If ``tool_args`` carries ``lat``/``lng``/``radius``, we use *nearby* search;
otherwise we run *text* search with optional location bias derived from the
``location_hint`` shared across the run.
"""

from __future__ import annotations

import time
from typing import Any

from ...config import Configuration
from ...models import Place
from ..base import Tool, ToolResult
from .client import GoogleMapsClient


def _normalise_place(raw: dict[str, Any]) -> Place | None:
    location = raw.get("location") or {}
    if not location:
        return None
    name = (raw.get("displayName") or {}).get("text") or raw.get("displayName") or ""
    opening = (raw.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []
    photos = raw.get("photos") or []
    first_photo = photos[0] if photos else None
    photo_ref = first_photo.get("name") if isinstance(first_photo, dict) else None
    try:
        lat = float(location.get("latitude", location.get("lat", 0.0)))
        lng = float(location.get("longitude", location.get("lng", 0.0)))
    except (TypeError, ValueError):
        return None
    return Place(
        place_id=raw.get("id") or raw.get("place_id") or "",
        name=name or "未命名地点",
        address=raw.get("formattedAddress") or raw.get("formatted_address") or "",
        lat=lat,
        lng=lng,
        rating=raw.get("rating"),
        user_ratings_total=raw.get("userRatingCount"),
        price_level=_price_level(raw.get("priceLevel")),
        categories=list(raw.get("types") or []),
        opening_hours=list(opening),
        website=raw.get("websiteUri"),
        phone=raw.get("nationalPhoneNumber"),
        photo_reference=photo_ref,
        google_maps_url=raw.get("googleMapsUri"),
    )


def _price_level(value: Any) -> int | None:
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


class PlacesTool(Tool):
    name = "places"
    description = "Search places via Google Maps Places API (text or nearby)."

    def __init__(self, client: GoogleMapsClient, config: Configuration) -> None:
        self._client = client
        self._config = config

    async def run(self, args: dict[str, Any]) -> ToolResult:
        query = (args.get("query") or "").strip()
        language = args.get("language") or self._config.default_language
        language_code = "zh-CN" if language == "zh" else "en"
        try:
            radius = int(args.get("radius") or self._config.google_maps_default_radius)
            limit = int(args.get("limit") or self._config.google_maps_places_limit)
        except (TypeError, ValueError):
            radius = self._config.google_maps_default_radius
            limit = self._config.google_maps_places_limit
        open_now = args.get("open_now")

        start = time.perf_counter()
        try:
            if "lat" in args and "lng" in args:
                data, cached = await self._client.nearby_search_places(
                    lat=float(args["lat"]),
                    lng=float(args["lng"]),
                    radius_meters=radius,
                    included_types=args.get("included_types"),
                    max_result_count=limit,
                    language_code=language_code,
                )
            else:
                if not query:
                    return ToolResult(error="places tool requires either query or lat/lng")
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
        except Exception as exc:  # pragma: no cover - defensive
            return ToolResult(error=f"places API failed: {exc}")

        raw_places = data.get("places") or []
        places: list[Place] = []
        for entry in raw_places:
            place = _normalise_place(entry)
            if place is not None:
                places.append(place)

        duration_ms = int((time.perf_counter() - start) * 1000)
        text = _format_places_as_text(places)
        return ToolResult(
            text=text,
            data={
                "places": [p.model_dump() for p in places],
                "duration_ms": duration_ms,
            },
            cached=cached,
        )


def _format_places_as_text(places: list[Place]) -> str:
    if not places:
        return "未找到匹配的地点。"
    lines: list[str] = []
    for idx, place in enumerate(places, start=1):
        rating = f"{place.rating}⭐" if place.rating else "无评分"
        reviews = f"({place.user_ratings_total} 评价)" if place.user_ratings_total else ""
        price = f" 价位{'$' * place.price_level}" if place.price_level else ""
        opening = (place.opening_hours[0] if place.opening_hours else "") or ""
        lines.append(
            f"{idx}. **{place.name}** — {rating} {reviews}{price}\n"
            f"   地址: {place.address}\n"
            f"   坐标: ({place.lat:.5f},{place.lng:.5f}) {opening}\n"
            f"   链接: {place.google_maps_url or '-'}"
        )
    return "\n".join(lines)
