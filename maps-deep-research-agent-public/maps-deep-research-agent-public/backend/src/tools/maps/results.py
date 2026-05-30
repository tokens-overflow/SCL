"""每个 Maps 工具返回的类型化 payload。

每个 Tool 都返回 ``ToolResult[<以下某个>]``。
下游 ToolRunner 用 ``isinstance(payload, XxxPayload)`` 分派构造 TaskEvidence，
不再按工具名 if-else。

加新工具？在这里加一个 XxxPayload，让对应 Tool 填充，
再在 ``ToolRunner._to_evidence`` 里加一条 isinstance 分支即可。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ...models import Place, RouteLeg


class PlacesPayload(BaseModel):
    """Places 文本搜索 / 附近搜索的结果。"""

    places: list[Place] = Field(default_factory=list)
    duration_ms: int = 0


class RoutesPayload(BaseModel):
    """Directions 的结果（一条主路线下的若干 leg）。"""

    routes: list[RouteLeg] = Field(default_factory=list)


class GeocodeResult(BaseModel):
    """Geocoding 响应里的一条匹配。"""

    formatted_address: str = ""
    lat: float
    lng: float
    place_id: str | None = None


class GeocodePayload(BaseModel):
    """Geocoding（地址 → 经纬度）的结果。"""

    results: list[GeocodeResult] = Field(default_factory=list)


class MatrixCell(BaseModel):
    """Distance Matrix 里的一个 (起点, 终点) 格子。"""

    origin: str
    destination: str
    distance_meters: int | None = None
    duration_seconds: int | None = None
    status: str | None = None


class MatrixPayload(BaseModel):
    """Distance Matrix（多起点 × 多终点）的结果。"""

    matrix: list[list[MatrixCell]] = Field(default_factory=list)
    origins: list[str] = Field(default_factory=list)
    destinations: list[str] = Field(default_factory=list)
