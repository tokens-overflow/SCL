"""Build an optional itinerary timeline + map overview from task evidence.

This module is independent of the report writer so it can be invoked in
parallel after all tasks finish.
"""

from __future__ import annotations

from typing import Any

from ..llm import LLMClient
from ..models import Place, ResearchState
from ..prompts import itinerary_messages


class ItineraryService:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def build(self, state: ResearchState) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        evidence_block, all_places = _evidence_for_itinerary(state)
        if not all_places:
            return [], _map_overview(all_places)

        try:
            raw = await self._llm.chat_json(
                itinerary_messages(
                    topic=state.topic,
                    evidence_block=evidence_block,
                    language=state.language,  # type: ignore[arg-type]
                ),
                temperature=0.4,
            )
        except Exception:
            raw = []

        days = _extract_days(raw)
        return days, _map_overview(all_places)


# ---------------------------------------------------------------------------
def _evidence_for_itinerary(state: ResearchState) -> tuple[str, list[Place]]:
    seen: set[str] = set()
    places: list[Place] = []
    for task in state.tasks:
        for place in task.evidence.places:
            if place.place_id and place.place_id in seen:
                continue
            seen.add(place.place_id)
            places.append(place)

    lines: list[str] = []
    for idx, place in enumerate(places, start=1):
        opening = place.opening_hours[0] if place.opening_hours else ""
        lines.append(
            f"{idx}. place_id={place.place_id} name={place.name} "
            f"rating={place.rating or '-'} address={place.address} {opening}"
        )
    return "\n".join(lines) or "（无地点）", places


def _extract_days(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict) and "slots" in item]
    if isinstance(raw, dict):
        # Common wrappers
        for key in ("days", "itinerary", "schedule"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _map_overview(places: list[Place]) -> dict[str, Any]:
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
            "west": min(lngs),
            "east": max(lngs),
        },
        "markers": [
            {
                "place_id": p.place_id,
                "name": p.name,
                "lat": p.lat,
                "lng": p.lng,
                "rating": p.rating,
                "url": p.google_maps_url,
            }
            for p in places
        ],
    }
