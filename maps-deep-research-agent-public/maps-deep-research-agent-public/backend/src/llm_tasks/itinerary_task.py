"""LLM 用例：地点证据 → 结构化 JSON 行程 + 地图概览。

地图概览（center / bounds / markers）是**确定性的纯几何计算**，不依赖 LLM；
LLM 仅负责把地点编排进多日时间段（days / slots）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..core.llm_task import JsonLLMTask
from ..models import Place, TaskNode
from ..prompts import itinerary_messages


class ItineraryInput(BaseModel):
    topic: str
    tasks: list[TaskNode]
    weather: str = ""        # 天气预报文本块（可空）
    trip_context: str = ""   # 出行日期/预算等上下文（可空）

    model_config = {"arbitrary_types_allowed": True}


# 返回值：(每日行程数组, 地图概览 dict)
ItineraryResult = tuple[list[dict[str, Any]], dict[str, Any]]


class ItineraryTask(JsonLLMTask[ItineraryInput, ItineraryResult]):
    """生成多日行程 + 地图概览。LLM 失败时由 ComposeStage 兜底为空行程。"""

    def build_messages(self, input: ItineraryInput) -> list[dict[str, str]]:
        evidence_text, _places = _evidence_for_itinerary(input.tasks)
        return itinerary_messages(
            topic=input.topic,
            evidence_block=evidence_text,
            weather=input.weather,
            trip_context=input.trip_context,
        )

    def parse(self, raw: Any, input: ItineraryInput) -> ItineraryResult:
        days = _extract_days(raw)
        _evidence_text, places = _evidence_for_itinerary(input.tasks)
        return days, _map_overview(places)


# ---------------------------------------------------------------------------
# 辅助函数 —— 纯几何 / 字符串处理，不调 LLM
# ---------------------------------------------------------------------------
def _evidence_for_itinerary(tasks: list[TaskNode]) -> tuple[str, list[Place]]:
    """按 place_id 去重所有任务证据中的地点，并格式化成 Prompt 用的文本。"""
    seen: set[str] = set()
    places: list[Place] = []
    for task in tasks:
        for place in task.evidence.places:
            if place.place_id and place.place_id in seen:
                continue
            seen.add(place.place_id)
            places.append(place)

    lines: list[str] = []
    for idx, place in enumerate(places, start=1):
        price = f" 价位={'¥' * place.price_level}" if place.price_level else ""
        hours = (" 营业=" + " | ".join(place.opening_hours)) if place.opening_hours else ""
        lines.append(
            f"{idx}. place_id={place.place_id} name={place.name} "
            f"rating={place.rating or '-'}{price} address={place.address}{hours}"
        )
    return "\n".join(lines) or "（无地点）", places


def _extract_days(raw: Any) -> list[dict[str, Any]]:
    """容忍 LLM 返回 ``[...]`` 或 ``{"days": [...]}`` / ``{"itinerary": [...]}`` 等包装。"""
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict) and "slots" in item]
    if isinstance(raw, dict):
        for key in ("days", "itinerary", "schedule"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _map_overview(places: list[Place]) -> dict[str, Any]:
    """根据所有 place 算出地图中心、外接矩形、marker 列表。纯几何，不调 LLM。"""
    if not places:
        return {}
    lats = [p.lat for p in places if p.lat]
    lngs = [p.lng for p in places if p.lng]
    if not lats or not lngs:
        return {}
    return {
        "center": {"lat": sum(lats) / len(lats), "lng": sum(lngs) / len(lngs)},
        "bounds": {
            "south": min(lats),
            "north": max(lats),
            "west":  min(lngs),
            "east":  max(lngs),
        },
        "markers": [
            {
                "place_id": p.place_id,
                "name":     p.name,
                "lat":      p.lat,
                "lng":      p.lng,
                "rating":   p.rating,
                "url":      p.google_maps_url,
            }
            for p in places
        ],
    }
