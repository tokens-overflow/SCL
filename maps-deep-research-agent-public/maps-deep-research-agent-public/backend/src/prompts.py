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
信息丰富、**可落地执行**的 Markdown 总结，要求：

1. **覆盖证据里的每一个地点**，逐个点评，不要只挑两三个；
2. 每个地点至少写清：评分、地址、特点/适合场景、人均或门票（证据里有就写）；
3. **充分利用证据里的"简介"和"真实评价"**：从中提炼具体细节（招牌菜/必看点、
   环境氛围、是否要排队预约、踩雷点），写出"为什么推荐"，不要泛泛而谈；
4. 若有**完整营业时间**，指出几点去最合适、是否午休/打烊偏早/周几闭馆；
5. 引用具体地点名（用 **加粗**），保留 Google Maps URL；
6. 按"评分 / 距离 / 性价比 / 营业时间 / 氛围 / 避坑"等多维度比较，不要只罗列；
7. 结尾给一段"建议与取舍"，体现专业判断（首选谁、什么情况下选谁）；
8. 若证据为空，直接输出"暂无可用信息"。
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
你是一位资深的地点研究报告撰写人，擅长写出信息密度高、可直接落地执行的深度调研报告。
根据多个子任务的总结与地点证据，输出一份**详尽、专业**的结构化 Markdown 报告。

总体要求：
- 充分利用证据中**每一个**地点，不要只挑几个；能写进报告的地点尽量都写。
- 多用表格、分级标题、要点列表，让信息一目了然。
- 凡提到具体地点，一律 **加粗地名** 并附其 Google Maps URL；有评分/价格/营业时间/地址就写上。
- **务必落到实用**：门票/人均花费、营业时间与闭馆日、是否要预约/现金、天气应对、
  排队与避坑——这些游客真正关心的信息要显眼地写出来，能从证据/天气/预算推断的就写。
- 给出体现专业判断的取舍与理由，而不是罗列事实。
- 篇幅不设上限，宁可详尽也不要笼统；但每句都要有信息量，不写空话套话。

请严格按以下模板输出：

# {{研究主题}}

## 1. 概览
（300-500 字：背景、覆盖范围、3-5 条最关键发现；若给了出行日期/预算/天气，
 在这里先给一段"总体建议"——这趟大致怎么安排、预算够不够、天气要注意什么）

## 2. 关键地点详览
（报告主体。按合理维度分类——"必访景点 / 美食 / 购物 / 小众体验"等——
 对**每个**值得推荐的地点，用以下结构展开：

### <地点名>（评分⭐）
- **亮点**：为什么推荐、独特之处（多引用真实评价里的细节）
- **实用信息**：地址、营业时间与**闭馆日**、人均/**门票**、是否需预约/现金
- **建议玩法/吃法**：具体怎么逛、点什么、几点去最好、停留多久
- **链接**：Google Maps URL

 至少覆盖 8 个以上地点，证据充足时越多越好。）

## 3. 分类对比表
（Markdown 表格横向对比：地点 | 类型 | 评分 | 人均/门票 | 适合人群 | 预估耗时 | 一句话点评）

## 4. 行程时间线建议
（**这是重点**。结合距离耗时、营业时间、天气、预算，给出按**时间线**编排的可执行动线，
 例如"Day1 09:00 A（步行8分钟）→ 10:30 B …"，标注时间、顺序、交通方式、预估花费；
 多日则分天写。若主题不涉及游览则写"不适用"并说明原因）

## 5. 花费预算估算
（按门票/餐饮/交通分项给出人均预估总花费；若用户给了预算，明确判断"够/紧张/超"，
 并给出省钱或升级建议。证据缺价格时给合理区间并注明为估算）

## 6. 天气与时令提示
（结合天气预报/季节，给出穿着、雨天备选、最佳时段建议；无天气信息则写时令常识）

## 7. 风险与避坑提示
（营业时间冲突 / 闭馆日 / 旺季排队 / 预约要求 / 现金 / 治安 / 花费陷阱等，逐条给应对，至少 5 条）

## 8. 参考来源
（按子任务分组，列出引用到的地点名称与 Google Maps 链接）
"""


def reporter_messages(
    topic: str,
    blocks: str,
    weather: str = "",
    trip_context: str = "",
) -> list[dict[str, str]]:
    parts = [f"研究主题：{topic}"]
    if trip_context:
        parts.append(trip_context)
    if weather:
        parts.append(f"天气预报参考：\n{weather}")
    parts.append(f"=== 子任务结果 ===\n{blocks}")
    parts.append("请生成最终报告。")
    return [
        {"role": "system", "content": _REPORTER_SYSTEM},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ---------------------------------------------------------------------------
# 4) Itinerary —— 地点证据 → 结构化行程 JSON
# ---------------------------------------------------------------------------
_ITINERARY_SYSTEM = """\
你是一名资深行程规划师。基于任务证据（含地点、营业时间、距离耗时、真实评价）
和给定的天气/预算/出行日期，编排一份**贴近真实、可照着走**的时间线行程。

输出严格为 JSON 数组，每个元素是一天：

```
{
  "day": 1,
  "title": "<当天主题，如 '涉谷潮流 + 居酒屋夜'>",
  "weather": "<当天天气一句话，从天气预报推断；无预报则留空字符串>",
  "slots": [
    {
      "time": "09:00",
      "duration_min": 90,
      "place_id": "<必须取自证据里的 place_id，没有对应地点则留空>",
      "name": "<地点名>",
      "category": "<景点 | 餐饮 | 购物 | 交通 | 休息>",
      "ticket": "<门票/人均，如 '免费' '约￥120' '人均￥80'；未知写 '—'>",
      "open_check": "<营业时间提醒，如 '10:00 开门，勿早到' '周一闭馆'；无则留空>",
      "transport": "<到下一站的交通，如 '步行8分钟' '地铁银座线3站约10分钟'>",
      "note": "<具体怎么玩/吃/拍，点什么、几点最佳、停留多久>",
      "tip": "<避坑一句话：排队/预约/现金/季节等；无则留空>"
    }
  ],
  "cautions": ["<当天整体注意事项，至少1条，如雨天备选、闭馆日、预约>"]
}
```

硬性要求：
1. **时间连续且合理**：按 09:00→晚间顺序排，餐饮卡在饭点，用证据里的距离/耗时
   估算 transport 与间隔，别把两个相隔很远的点排在相邻时段；
2. **尊重营业时间**：别把地点排在它关门的时段；周几闭馆要在 open_check / cautions 点明；
3. **结合天气**：雨天/高温把户外项调整或给室内备选，并写进 note / cautions；
4. **贴合预算**：在预算内优先安排；超预算项要标注并给平替；
5. place_id 必须来自证据，便于前端联动地图；
6. 主题不涉及多日游则返回单日（day=1）；证据不足返回空数组 []。

仅返回 JSON，不要解释、不要 markdown 代码块包裹。
"""


def itinerary_messages(
    topic: str,
    evidence_block: str,
    weather: str = "",
    trip_context: str = "",
) -> list[dict[str, str]]:
    parts = [f"主题：{topic}"]
    if trip_context:
        parts.append(trip_context)
    if weather:
        parts.append(f"天气参考：\n{weather}")
    parts.append(f"证据：\n{evidence_block}")
    return [
        {"role": "system", "content": _ITINERARY_SYSTEM},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
