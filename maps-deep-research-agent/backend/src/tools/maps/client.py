"""Async HTTP client for Google Maps Platform endpoints.

We talk directly to the REST APIs via ``httpx`` rather than ``googlemaps``
(which is sync) so the orchestrator can fan out many requests concurrently.

Endpoints used:
* Places API (New) — Text Search & Nearby Search
* Geocoding API
* Directions API
* Distance Matrix API
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ...config import Configuration
from ..cache import MapsCache

logger = logging.getLogger(__name__)

PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"
PLACES_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"
DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"
DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

PLACE_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.rating,places.userRatingCount,places.priceLevel,places.types,"
    "places.regularOpeningHours,places.websiteUri,places.nationalPhoneNumber,"
    "places.googleMapsUri,places.photos"
)


class GoogleMapsClient:
    """Async wrapper around the Maps REST APIs with caching + retries."""

    def __init__(self, config: Configuration, cache: MapsCache) -> None:
        if not config.google_maps_api_key:
            raise RuntimeError("GOOGLE_MAPS_API_KEY is not configured")
        self._config = config
        self._cache = cache
        self._api_calls = 0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=20.0)

    @property
    def api_calls(self) -> int:
        return self._api_calls

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    async def _cached_request(
        self,
        tool: str,
        args: dict[str, Any],
        request_fn,
    ) -> tuple[dict[str, Any], bool]:
        """Return (data, was_cached)."""
        cached = self._cache.get(tool, args)
        if cached is not None:
            logger.debug("cache hit tool=%s", tool)
            return cached, True

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                response = await request_fn()
                response.raise_for_status()
                data = response.json()
                async with self._lock:
                    self._api_calls += 1
                self._cache.set(tool, args, data)
                return data, False
        raise RuntimeError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------
    async def text_search_places(
        self,
        query: str,
        *,
        location_bias: dict[str, Any] | None = None,
        max_result_count: int | None = None,
        open_now: bool | None = None,
        language_code: str = "zh-CN",
    ) -> tuple[dict[str, Any], bool]:
        limit = max_result_count or self._config.google_maps_places_limit
        body: dict[str, Any] = {
            "textQuery": query,
            "maxResultCount": limit,
            "languageCode": language_code,
        }
        if location_bias:
            body["locationBias"] = location_bias
        if open_now is True:
            body["openNow"] = True

        args = {"endpoint": "text_search", **body}

        async def do() -> httpx.Response:
            return await self._http.post(
                PLACES_TEXT_SEARCH_URL,
                json=body,
                headers={
                    "X-Goog-Api-Key": self._config.google_maps_api_key,
                    "X-Goog-FieldMask": PLACE_FIELD_MASK,
                },
            )

        return await self._cached_request("places.text", args, do)

    async def nearby_search_places(
        self,
        *,
        lat: float,
        lng: float,
        radius_meters: int,
        included_types: list[str] | None = None,
        max_result_count: int | None = None,
        language_code: str = "zh-CN",
    ) -> tuple[dict[str, Any], bool]:
        limit = max_result_count or self._config.google_maps_places_limit
        body: dict[str, Any] = {
            "maxResultCount": limit,
            "languageCode": language_code,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius_meters),
                }
            },
        }
        if included_types:
            body["includedTypes"] = included_types

        args = {"endpoint": "nearby", **body}

        async def do() -> httpx.Response:
            return await self._http.post(
                PLACES_NEARBY_SEARCH_URL,
                json=body,
                headers={
                    "X-Goog-Api-Key": self._config.google_maps_api_key,
                    "X-Goog-FieldMask": PLACE_FIELD_MASK,
                },
            )

        return await self._cached_request("places.nearby", args, do)

    async def geocode(self, address: str, language: str = "zh-CN") -> tuple[dict[str, Any], bool]:
        params = {"address": address, "key": self._config.google_maps_api_key, "language": language}
        args = {"endpoint": "geocode", "address": address, "language": language}

        async def do() -> httpx.Response:
            return await self._http.get(GEOCODING_URL, params=params)

        return await self._cached_request("geocoding", args, do)

    async def directions(
        self,
        origin: str,
        destination: str,
        *,
        mode: str = "driving",
        language: str = "zh-CN",
        waypoints: list[str] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        params: dict[str, Any] = {
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "language": language,
            "key": self._config.google_maps_api_key,
        }
        if waypoints:
            params["waypoints"] = "|".join(waypoints)

        args = {"endpoint": "directions", **{k: v for k, v in params.items() if k != "key"}}

        async def do() -> httpx.Response:
            return await self._http.get(DIRECTIONS_URL, params=params)

        return await self._cached_request("directions", args, do)

    async def distance_matrix(
        self,
        origins: list[str],
        destinations: list[str],
        *,
        mode: str = "driving",
        language: str = "zh-CN",
    ) -> tuple[dict[str, Any], bool]:
        params = {
            "origins": "|".join(origins),
            "destinations": "|".join(destinations),
            "mode": mode,
            "language": language,
            "key": self._config.google_maps_api_key,
        }
        args = {
            "endpoint": "distance_matrix",
            "origins": origins,
            "destinations": destinations,
            "mode": mode,
            "language": language,
        }

        async def do() -> httpx.Response:
            return await self._http.get(DISTANCE_MATRIX_URL, params=params)

        return await self._cached_request("distance_matrix", args, do)
