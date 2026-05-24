"""Google Maps Platform tools."""

from __future__ import annotations

from ...config import Configuration
from ..base import ToolRegistry
from ..cache import MapsCache
from .client import GoogleMapsClient
from .directions import DirectionsTool
from .distance_matrix import DistanceMatrixTool
from .geocoding import GeocodingTool
from .places import PlacesTool

__all__ = [
    "GoogleMapsClient",
    "PlacesTool",
    "DirectionsTool",
    "GeocodingTool",
    "DistanceMatrixTool",
    "register_default_maps_tools",
]


def register_default_maps_tools(
    config: Configuration,
    registry: ToolRegistry | None = None,
    cache: MapsCache | None = None,
) -> tuple[ToolRegistry, GoogleMapsClient, MapsCache]:
    """Register all built-in Maps tools and return shared components."""
    registry = registry or ToolRegistry()
    cache = cache or MapsCache(directory=config.cache_path, ttl_seconds=config.cache_ttl_seconds)
    client = GoogleMapsClient(config=config, cache=cache)

    registry.register(PlacesTool(client=client, config=config))
    registry.register(DirectionsTool(client=client))
    registry.register(GeocodingTool(client=client))
    registry.register(DistanceMatrixTool(client=client))
    return registry, client, cache
