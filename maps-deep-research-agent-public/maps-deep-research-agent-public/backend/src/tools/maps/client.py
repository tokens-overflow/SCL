"""Google Maps Platform 的 async HTTP 客户端。

5 个公开方法（text_search / nearby / geocode / directions / matrix）形态都是：
    1. 拼请求体（POST）或参数字典（GET）；
    2. 走 ``_cached_request`` 套上 LRU+sqlite 缓存 + tenacity 重试；
    3. 返回 ``(json_dict, was_cached)``。

把这套样板抽到 :meth:`_post` / :meth:`_get` 两个私有方法里，
5 个公开方法只负责构造请求字段，HTTP 调用代码不再 5 份重复。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ...config import AppConfig
from ..cache import MapsCache

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- endpoints
PLACES_TEXT_SEARCH_URL   = "https://places.googleapis.com/v1/places:searchText"
PLACES_NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"
PLACES_DETAILS_URL       = "https://places.googleapis.com/v1/places/{place_id}"
GEOCODING_URL            = "https://maps.googleapis.com/maps/api/geocode/json"
DIRECTIONS_URL           = "https://maps.googleapis.com/maps/api/directions/json"
DISTANCE_MATRIX_URL      = "https://maps.googleapis.com/maps/api/distancematrix/json"

# Places (New) 的 X-Goog-FieldMask。
# 只列实际用到的字段，能显著减小响应体 + 降低计费。
PLACE_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.rating,places.userRatingCount,places.priceLevel,places.types,"
    "places.regularOpeningHours,places.websiteUri,places.nationalPhoneNumber,"
    "places.googleMapsUri,places.photos,"
    # ↓ 让 LLM 拿到真实定性材料（一句话简介 + 评价正文），写出"为什么推荐"
    "places.editorialSummary,places.reviews.text,places.reviews.rating,"
    "places.reviews.relativePublishTimeDescription"
)


class GoogleMapsClient:
    """4 个 Maps REST endpoint 的薄 async 外观。

    职责：
        - 维护共享 httpx.AsyncClient（一个 agent 一个连接池）；
        - 累计 API 调用次数（给 /usage 接口报数用）；
        - 把每个调用路由进两级缓存；
        - 按 config 中的 retry 策略走指数退避。

    刻意**不**负责：
        - 把 JSON 拆成 typed payload —— 那是每个 Tool 子类的事；
        - DAG / 依赖 / 事件分发 —— 那是 ExecuteStage 的事。
    """

    def __init__(self, app_cfg: AppConfig, cache: MapsCache) -> None:
        if not app_cfg.maps.api_key:
            raise RuntimeError("config.yaml: app.maps.api_key 未配置")
        self._app = app_cfg
        self._cache = cache
        self._api_calls = 0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=app_cfg.http.timeout_seconds)

    # ------------------------------------------------------------------ 状态
    @property
    def api_calls(self) -> int:
        return self._api_calls

    @property
    def default_language_code(self) -> str:
        return self._app.maps.default_language_code

    @property
    def default_places_limit(self) -> int:
        return self._app.maps.default_places_limit

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ 底层
    async def _cached_request(
        self,
        tool: str,
        cache_args: dict[str, Any],
        do: Callable[[], Awaitable[httpx.Response]],
    ) -> tuple[dict[str, Any], bool]:
        """套上缓存 + 重试地跑 ``do()``。返回 (json_data, was_cached)。"""
        cached = self._cache.get(tool, cache_args)
        if cached is not None:
            logger.debug("命中缓存 tool=%s", tool)
            return cached, True

        policy = self._app.retry.maps
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(policy.max_attempts),
            wait=wait_exponential(multiplier=policy.backoff_mult, max=policy.backoff_max),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                response = await do()
                response.raise_for_status()
                data = response.json()
                async with self._lock:
                    self._api_calls += 1
                self._cache.set(tool, cache_args, data)
                return data, False
        raise RuntimeError("unreachable")  # pragma: no cover

    def _places_headers(self) -> dict[str, str]:
        """Places (New) 端点要求两个 header：api key 与字段掩码。"""
        return {
            "X-Goog-Api-Key": self._app.maps.api_key,
            "X-Goog-FieldMask": PLACE_FIELD_MASK,
        }

    async def _post(
        self,
        url: str,
        body: dict[str, Any],
        *,
        tool: str,
        cache_args: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """向 Places (New) 端点发 POST。默认拿 body 当 cache key。"""
        async def do() -> httpx.Response:
            return await self._http.post(url, json=body, headers=self._places_headers())
        return await self._cached_request(tool, cache_args or body, do)

    async def _get(
        self,
        url: str,
        params: dict[str, Any],
        *,
        tool: str,
        cache_args: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """向 legacy Maps 端点发 GET。``cache_args`` 必须排除 api key。"""
        async def do() -> httpx.Response:
            return await self._http.get(url, params=params)
        return await self._cached_request(tool, cache_args, do)

    # ------------------------------------------------------------------ Places API (New)
    async def text_search_places(
        self,
        query: str,
        *,
        location_bias: dict[str, Any] | None = None,
        max_result_count: int | None = None,
        open_now: bool | None = None,
        language_code: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        body: dict[str, Any] = {
            "textQuery": query,
            "maxResultCount": max_result_count or self._app.maps.default_places_limit,
            "languageCode": language_code or self._app.maps.default_language_code,
        }
        if location_bias:
            body["locationBias"] = location_bias
        if open_now is True:
            body["openNow"] = True
        return await self._post(PLACES_TEXT_SEARCH_URL, body, tool="places.text",
                                cache_args={"endpoint": "text_search", **body})

    async def nearby_search_places(
        self,
        *,
        lat: float,
        lng: float,
        radius_meters: int,
        included_types: list[str] | None = None,
        max_result_count: int | None = None,
        language_code: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        body: dict[str, Any] = {
            "maxResultCount": max_result_count or self._app.maps.default_places_limit,
            "languageCode": language_code or self._app.maps.default_language_code,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius_meters),
                }
            },
        }
        if included_types:
            body["includedTypes"] = included_types
        return await self._post(PLACES_NEARBY_SEARCH_URL, body, tool="places.nearby",
                                cache_args={"endpoint": "nearby", **body})

    # ------------------------------------------------------------------ legacy GET
    async def geocode(
        self,
        address: str,
        language: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        lang = language or self._app.maps.default_language_code
        return await self._get(
            GEOCODING_URL,
            params={"address": address, "key": self._app.maps.api_key, "language": lang},
            tool="geocoding",
            cache_args={"endpoint": "geocode", "address": address, "language": lang},
        )

    async def directions(
        self,
        origin: str,
        destination: str,
        *,
        mode: str = "driving",
        language: str | None = None,
        waypoints: list[str] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        lang = language or self._app.maps.default_language_code
        params: dict[str, Any] = {
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "language": lang,
            "key": self._app.maps.api_key,
        }
        if waypoints:
            params["waypoints"] = "|".join(waypoints)
        # cache_args 排掉 api key：同样的查询不管谁的 key 都该命中同一份缓存
        cache_args = {"endpoint": "directions", **{k: v for k, v in params.items() if k != "key"}}
        return await self._get(DIRECTIONS_URL, params, tool="directions", cache_args=cache_args)

    async def distance_matrix(
        self,
        origins: list[str],
        destinations: list[str],
        *,
        mode: str = "driving",
        language: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        lang = language or self._app.maps.default_language_code
        params = {
            "origins": "|".join(origins),
            "destinations": "|".join(destinations),
            "mode": mode,
            "language": lang,
            "key": self._app.maps.api_key,
        }
        cache_args = {
            "endpoint": "distance_matrix",
            "origins": origins,
            "destinations": destinations,
            "mode": mode,
            "language": lang,
        }
        return await self._get(DISTANCE_MATRIX_URL, params, tool="distance_matrix", cache_args=cache_args)
