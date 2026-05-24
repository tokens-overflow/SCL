"""Prompt templates for planner / summarizer / reporter agents.

Two improvements over chapter 14:
1. Prompts explicitly target the **Google Maps tool surface** (places/directions/...)
   and constrain ``tool`` + ``tool_args`` selection per task.
2. Each prompt has a parallel zh / en variant selectable via ``language``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal


Language = Literal["zh", "en"]


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
_PLANNER_SYSTEM_ZH = """\
你是一名专业的地点研究规划师。请把用户的主题拆解为 3-{max_tasks} 个互补的检索任务，
每个任务必须能映射到 Google Maps Platform 的某个具体 API：

- `places`         : 搜索某地附近的地点（餐厅、景点、商店…）
- `geocoding`      : 把地址/地名解析成经纬度
- `directions`     : 计算 A→B 的路线
- `distance_matrix`: 批量计算多组起点-终点的距离/耗时

任务之间可以声明依赖：例如先 `geocoding` 拿到坐标，再 `places` 在该坐标附近搜索。
"""

_PLANNER_SYSTEM_EN = """\
You are a professional location-research planner. Decompose the user topic into
3-{max_tasks} complementary tasks. Every task MUST map to one of the Google Maps
Platform APIs: `places`, `geocoding`, `directions`, `distance_matrix`.

Tasks may declare dependencies (e.g. a `geocoding` task first, then a `places`
task that uses its coordinates).
"""

_PLANNER_USER_ZH = """\
今天日期：{today}
研究主题：{topic}
{location_hint}

请严格以 JSON 数组形式返回任务列表（不要任何其它解释），每个元素含字段：

```
{{
  "id": <int, 从1开始>,
  "title": "<不超过 12 个字的任务名>",
  "intent": "<1-2 句解释要解决什么问题>",
  "query": "<给 Maps API 的查询关键词或地址>",
  "tool": "places" | "geocoding" | "directions" | "distance_matrix",
  "tool_args": {{ ... 可选的工具参数，例如 {{"radius": 3000}} ... }},
  "depends_on": [<前置任务 id, 可为空>]
}}
```

约束：
1. id 唯一且递增；depends_on 中的 id 必须已经存在；
2. 至少有 1 个 `places` 任务用于发现地点；
3. 如果主题涉及"线路 / 路程"，必须包含 1 个 `directions` 或 `distance_matrix` 任务；
4. tool_args 中可以填写如 `{{"open_now": true}}` 或 `{{"mode": "transit"}}` 等过滤项。
"""

_PLANNER_USER_EN = """\
Today: {today}
Research topic: {topic}
{location_hint}

Return ONLY a JSON array of tasks (no prose), each with:

```
{{
  "id": <int starting at 1>,
  "title": "<short, <= 6 words>",
  "intent": "<1-2 sentences on the question to answer>",
  "query": "<query passed to the Maps API>",
  "tool": "places" | "geocoding" | "directions" | "distance_matrix",
  "tool_args": {{ ... optional, e.g. {{"radius": 3000}} ... }},
  "depends_on": [<prerequisite task ids, may be empty>]
}}
```

Constraints:
1. ids are unique and ascending; every entry in depends_on must already exist;
2. include at least one `places` task for discovery;
3. if the topic mentions routes or travel time, include at least one
   `directions` or `distance_matrix` task.
"""


def planner_messages(
    topic: str,
    language: Language,
    max_tasks: int,
    location_hint: str | None,
) -> list[dict[str, str]]:
    hint_line = ""
    if location_hint:
        hint_line = (
            f"位置锚点：{location_hint}\n" if language == "zh" else f"Location anchor: {location_hint}\n"
        )

    if language == "zh":
        return [
            {"role": "system", "content": _PLANNER_SYSTEM_ZH.format(max_tasks=max_tasks)},
            {
                "role": "user",
                "content": _PLANNER_USER_ZH.format(
                    today=now_iso(), topic=topic, location_hint=hint_line
                ),
            },
        ]
    return [
        {"role": "system", "content": _PLANNER_SYSTEM_EN.format(max_tasks=max_tasks)},
        {
            "role": "user",
            "content": _PLANNER_USER_EN.format(
                today=now_iso(), topic=topic, location_hint=hint_line
            ),
        },
    ]


# ---------------------------------------------------------------------------
# Summarizer
# ---------------------------------------------------------------------------
_SUMMARIZER_SYSTEM_ZH = """\
你是一名地点研究执行者。基于给定的 Google Maps 检索证据，针对单个任务撰写一段
Markdown 总结，要求：

1. 用 3-5 条要点说明发现，按"评分 / 距离 / 性价比 / 营业时间"等多个维度展开；
2. 引用具体地点名（用 **加粗** 标注），并保留它们的 Google Maps URL；
3. 给出一条"建议"或"取舍"小结，体现专业判断；
4. 如果证据为空，请直接输出"暂无可用信息"。
"""

_SUMMARIZER_SYSTEM_EN = """\
You are a location-research executor. Given Google Maps evidence for a single
task, write a concise Markdown summary:

1. 3-5 bullets covering ratings, distance, value, opening hours, etc.;
2. cite place names in **bold** and keep their Google Maps URLs;
3. close with one "Recommendation" line that shows judgment;
4. if no evidence is available, output exactly "No usable information".
"""


def summarizer_messages(
    topic: str,
    task_title: str,
    task_intent: str,
    evidence_block: str,
    language: Language,
) -> list[dict[str, str]]:
    system = _SUMMARIZER_SYSTEM_ZH if language == "zh" else _SUMMARIZER_SYSTEM_EN
    if language == "zh":
        user = (
            f"研究主题：{topic}\n"
            f"任务名称：{task_title}\n"
            f"任务目标：{task_intent}\n\n"
            f"以下是 Google Maps 返回的证据：\n{evidence_block}"
        )
    else:
        user = (
            f"Topic: {topic}\n"
            f"Task: {task_title}\n"
            f"Intent: {task_intent}\n\n"
            f"Google Maps evidence:\n{evidence_block}"
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------
_REPORTER_SYSTEM_ZH = """\
你是一位资深的地点研究报告撰写人。根据多个子任务的总结与地点证据，输出一份
结构化 Markdown 报告。模板：

# {{研究主题}}

## 1. 概览
（200 字以内，引出主题与关键发现）

## 2. 关键地点
（按 1-2 个维度分类列出 5-10 个最值得关注的地点，附 Google Maps URL）

## 3. 行程 / 路线建议
（如果主题涉及游览，给出 1-2 条切实可行的路线；若不适用就写"不适用"）

## 4. 风险与提示
（营业时间冲突 / 旺季 / 排队 / 治安等需要提醒的事项）

## 5. 参考来源
（按子任务列出主要参考地点的名称与链接）
"""

_REPORTER_SYSTEM_EN = """\
You are a senior location-research writer. From the per-task summaries and
Maps evidence below, produce a structured Markdown report using the template:

# {{topic}}

## 1. Overview
## 2. Highlighted Places
## 3. Suggested Itinerary / Routes
## 4. Caveats
## 5. References
"""


def reporter_messages(
    topic: str,
    blocks: str,
    language: Language,
) -> list[dict[str, str]]:
    system = _REPORTER_SYSTEM_ZH if language == "zh" else _REPORTER_SYSTEM_EN
    if language == "zh":
        user = f"研究主题：{topic}\n\n=== 子任务结果 ===\n{blocks}\n\n请生成最终报告。"
    else:
        user = f"Topic: {topic}\n\n=== Task results ===\n{blocks}\n\nProduce the report."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# Itinerary (optional auxiliary call)
# ---------------------------------------------------------------------------
_ITINERARY_SYSTEM_ZH = """\
基于任务证据，输出一份结构化 JSON 行程数组。每个元素：

```
{
  "day": 1,
  "slots": [
    {"time": "09:00", "place_id": "...", "name": "...", "note": "..."}
  ]
}
```

若主题不需要多日行程，可以返回单日（day=1）的 slots，或返回空数组。
仅返回 JSON，不要解释。
"""

_ITINERARY_SYSTEM_EN = """\
Given the task evidence, output a JSON itinerary array. Each item:

```
{"day": 1, "slots": [{"time": "09:00", "place_id": "...", "name": "...", "note": "..."}]}
```

Return JSON only.
"""


def itinerary_messages(
    topic: str,
    evidence_block: str,
    language: Language,
) -> list[dict[str, str]]:
    system = _ITINERARY_SYSTEM_ZH if language == "zh" else _ITINERARY_SYSTEM_EN
    user = (
        f"主题：{topic}\n证据：\n{evidence_block}"
        if language == "zh"
        else f"Topic: {topic}\nEvidence:\n{evidence_block}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
