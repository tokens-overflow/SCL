"""Final report generator: consolidates per-task summaries into Markdown."""

from __future__ import annotations

from ..llm import LLMClient
from ..models import ResearchState
from ..prompts import reporter_messages


class ReporterService:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def generate(self, state: ResearchState) -> str:
        blocks = []
        for task in state.tasks:
            place_lines = []
            for place in task.evidence.places[:6]:
                rating = f"{place.rating}⭐" if place.rating else "-"
                place_lines.append(
                    f"  - **{place.name}** ({rating}) {place.address} {place.google_maps_url or ''}"
                )
            route_lines = []
            for leg in task.evidence.routes[:4]:
                km = leg.distance_meters / 1000.0 if leg.distance_meters else 0.0
                mins = leg.duration_seconds / 60.0 if leg.duration_seconds else 0.0
                route_lines.append(
                    f"  - {leg.origin} → {leg.destination} ({leg.mode}): "
                    f"{km:.1f}km / {mins:.0f}min"
                )

            block = (
                f"### 子任务 {task.id} — {task.title} [{task.status}]\n"
                f"- 目标: {task.intent}\n"
                f"- 查询: `{task.query}` (tool={task.tool})\n"
                f"- 任务总结:\n{task.summary or '（无）'}\n"
            )
            if place_lines:
                block += "- 涉及地点:\n" + "\n".join(place_lines) + "\n"
            if route_lines:
                block += "- 路线信息:\n" + "\n".join(route_lines) + "\n"
            blocks.append(block)

        messages = reporter_messages(
            topic=state.topic,
            blocks="\n".join(blocks),
            language=state.language,  # type: ignore[arg-type]
        )
        report = await self._llm.chat(messages, temperature=0.3)
        return report.strip()
