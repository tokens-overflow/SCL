"""Google Maps Platform 工具集合。

``register_default_maps_tools`` 把 4 个内置工具一次性注册到 ToolRegistry，
并把共享的 HTTP 客户端 + 缓存实例也一并返回，方便 Agent 在
``aclose()`` 时关闭。
"""

from __future__ import annotations

from ...config import AppConfig
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
    app_cfg: AppConfig,
    registry: ToolRegistry | None = None,
    cache: MapsCache | None = None,
) -> tuple[ToolRegistry, GoogleMapsClient, MapsCache]:
    """注册 4 个内置 Maps 工具，返回共享组件。"""
    registry = registry or ToolRegistry()
    cache = cache or MapsCache(
        directory=app_cfg.cache.path,
        ttl_seconds=app_cfg.cache.ttl_seconds,
        lru_size=app_cfg.cache.in_memory_lru_size,
    )
    client = GoogleMapsClient(app_cfg=app_cfg, cache=cache)

    registry.register(PlacesTool(client=client, app_cfg=app_cfg))
    registry.register(DirectionsTool(client=client))
    registry.register(GeocodingTool(client=client))
    registry.register(DistanceMatrixTool(client=client))
    return registry, client, cache
