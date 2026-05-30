"""规划 / 摘要 / 报告 / 行程 4 类 Prompt 模板（中文-only）。

每个 Prompt 函数返回 OpenAI 兼容的 ``messages`` 数组（system + user）。
函数本身不调用 LLM —— 调用归 ``llm_tasks/*`` 那一层。

约定：
* 所有 Prompt 都明确告知 LLM 工具表面 (Google Maps Platform)；
* JSON 任务的 Prompt 用三反引号块画出预期 schema，便于模型对齐；
* 不再做语言分支，统一中文输出。
"""

from __future__ import annotations

from datetime import datetime


def now_iso() -> str:
    """当前日期，写进 Planner 的 Prompt 让 LLM 知道"今天"。"""
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 1) Planner —— 把研究主题拆成有依赖的 DAG 子任务
# ---------------------------------------------------------------------------
_PLANNER_SYSTEM = """\
你是一名专业的地点研究规划师。请把用户的主题拆解为 3-{max_tasks} 个互补的检索任务，
每个任务必须能映射到 Google Maps Platform 的某个具体 API：

- `places`         : 搜索某地附近的地点（餐厅、景点、商店…）
- `geocoding`      : 把地址/地名解析成经纬度
- `directions`     : 计算 A→B 的路线
- `distance_matrix`: 批量计算多组起点-终点的距离/耗时

任务之间可以声明依赖：例如先 `geocoding` 拿到坐标，再 `places` 在该坐标附近搜索。
"""

_PLANNER_USER = """\
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


def planner_messages(
    topic: str,
    max_tasks: int,
    location_hint: str | None,
) -> list[dict[str, str]]:
    hint_line = f"位置锚点：{location_hint}\n" if location_hint else ""
    return [
        {"role": "system", "content": _PLANNER_SYSTEM.format(max_tasks=max_tasks)},
        {
            "role": "user",
            "content": _PLANNER_USER.format(
                today=now_iso(), topic=topic, location_hint=hint_line
            ),
        },
    ]


# ---------------------------------------------------------------------------
# 2) Summarizer —— 单任务证据 → 流式 Markdown 摘要
# ---------------------------------------------------------------------------
_SUMMARIZER_SYSTEM = """\
你是一名地点研究执行者。基于给定的 Google Maps 检索证据，针对单个任务撰写一段
Markdown 总结，要求：

1. 用 3-5 条要点说明发现，按"评分 / 距离 / 性价比 / 营业时间"等多个维度展开；
2. 引用具体地点名（用 **加粗** 标注），并保留它们的 Google Maps URL；
3. 给出一条"建议"或"取舍"小结，体现专业判断；
4. 如果证据为空，请直接输出"暂无可用信息"。
"""


def summarizer_messages(
    topic: str,
    task_title: str,
    task_intent: str,
    evidence_block: str,
) -> list[dict[str, str]]:
    user = (
        f"研究主题：{topic}\n"
        f"任务名称：{task_title}\n"
        f"任务目标：{task_intent}\n\n"
        f"以下是 Google Maps 返回的证据：\n{evidence_block}"
    )
    return [
        {"role": "system", "content": _SUMMARIZER_SYSTEM},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 3) Reporter —— 所有子任务总结 → 最终 Markdown 报告
# ---------------------------------------------------------------------------
_REPORTER_SYSTEM = """\
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


def reporter_messages(topic: str, blocks: str) -> list[dict[str, str]]:
    user = f"研究主题：{topic}\n\n=== 子任务结果 ===\n{blocks}\n\n请生成最终报告。"
    return [
        {"role": "system", "content": _REPORTER_SYSTEM},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 4) Itinerary —— 地点证据 → 结构化行程 JSON
# ---------------------------------------------------------------------------
_ITINERARY_SYSTEM = """\
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


def itinerary_messages(topic: str, evidence_block: str) -> list[dict[str, str]]:
    user = f"主题：{topic}\n证据：\n{evidence_block}"
    return [
        {"role": "system", "content": _ITINERARY_SYSTEM},
        {"role": "user", "content": user},
    ]
