"""LLM 用例：所有完成的任务 + 证据 → 最终 Markdown 报告。"""

from __future__ import annotations

from pydantic import BaseModel

from ..core.llm_task import TextLLMTask
from ..models import TaskNode
from ..prompts import reporter_messages


class ReportInput(BaseModel):
    """组装最终报告所需的全部输入。"""

    topic: str
    tasks: list[TaskNode]
    weather: str = ""        # 天气预报文本块（可空）
    trip_context: str = ""   # 出行日期/预算等上下文（可空）

    # TaskNode 是 Pydantic 模型，BaseModel 内嵌时需打开此开关
    model_config = {"arbitrary_types_allowed": True}


class ReportTask(TextLLMTask[ReportInput, str]):
    """把每个子任务的总结 + 证据汇总成一份结构化 Markdown 报告。"""

    def build_messages(self, input: ReportInput) -> list[dict[str, str]]:
        return reporter_messages(
            topic=input.topic,
            blocks=_render_task_blocks(input.tasks),
            weather=input.weather,
            trip_context=input.trip_context,
        )

    def parse(self, raw: str, input: ReportInput) -> str:
        return raw.strip()


def _render_task_blocks(tasks: list[TaskNode]) -> str:
    """把每个完成的任务渲染成给 Reporter Prompt 的 Markdown 子块。

    放在这里（而不是 prompts.py）的意义：
    需要调整证据如何"展示给报告 LLM"时，只改本函数，不动 Prompt 模板本体。
    """
    blocks: list[str] = []
    for task in tasks:
        # 每个任务最多列 20 个地点 / 8 条路线，给报告 LLM 充足证据
        place_lines: list[str] = []
        for place in task.evidence.places[:20]:
            rating = f"{place.rating}⭐" if place.rating else "-"
            hours = f" 营业:{'; '.join(place.opening_hours)}" if place.opening_hours else ""
            price = f" 价位:{'¥' * place.price_level}" if place.price_level else ""
            place_lines.append(
                f"  - **{place.name}** ({rating}){price}{hours} {place.address} {place.google_maps_url or ''}"
            )
        route_lines: list[str] = []
        for leg in task.evidence.routes[:8]:
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
    return "\n".join(blocks)
