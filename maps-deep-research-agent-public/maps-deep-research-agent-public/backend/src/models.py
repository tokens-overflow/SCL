"""域模型 + SSE 事件模型。

两个集合：
    1. 业务域：Place / RouteLeg / TaskEvidence / TaskNode / ResearchState。
    2. SSE 事件：StatusEvent / PlanReadyEvent / TaskUpdateEvent / ...

约定：
    * 所有 Event 继承 :class:`BaseEvent`，``type`` 字段是 Literal，前端可 switch；
    * 任务为 DAG —— ``TaskNode.depends_on`` 列出前置任务 id；
    * ``ResearchState`` 是每次 run 的中心快照，所有 stage 都通过它通信。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


TaskStatus = Literal["pending", "in_progress", "completed", "skipped", "failed"]


# ---------------------------------------------------------------------------
# 业务域模型
# ---------------------------------------------------------------------------
class Place(BaseModel):
    """从 Google Maps 工具里归一化出来的一个地点。"""

    place_id: str
    name: str
    address: str = ""
    lat: float
    lng: float
    rating: float | None = None
    user_ratings_total: int | None = None
    price_level: int | None = None
    categories: list[str] = Field(default_factory=list)
    opening_hours: list[str] = Field(default_factory=list)
    website: str | None = None
    phone: str | None = None
    photo_reference: str | None = None
    google_maps_url: str | None = None
    editorial_summary: str | None = None  # Google 官方一句话简介
    reviews: list[str] = Field(default_factory=list)  # 精选评价正文片段


class RouteLeg(BaseModel):
    """一条路线里的一段 leg（一个起点→终点对）。"""

    origin: str
    destination: str
    mode: str
    distance_meters: int
    duration_seconds: int
    polyline: str | None = None


class TaskEvidence(BaseModel):
    """单个子任务跑完后聚合出来的 Maps 证据。"""

    places: list[Place] = Field(default_factory=list)
    routes: list[RouteLeg] = Field(default_factory=list)
    raw_calls: int = 0
    notes: list[str] = Field(default_factory=list)


class TaskNode(BaseModel):
    """研究 DAG 里的一个节点。"""

    id: int
    title: str = Field(description="简短任务名，≤ 12 字")
    intent: str = Field(description="本任务要回答的问题")
    query: str = Field(description="给 Maps 工具的查询字符串")
    tool: Literal["places", "directions", "geocoding", "distance_matrix"] = "places"
    tool_args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)

    # 运行时字段（PlanStage 不填，ExecuteStage 填）
    status: TaskStatus = "pending"
    summary: str = ""
    evidence: TaskEvidence = Field(default_factory=TaskEvidence)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None


class ResearchState(BaseModel):
    """每次请求的中心状态对象，Pipeline 各 Stage 都通过它互传数据。

    字段按 stage 顺序填入：
        PlanStage      → tasks
        ExecuteStage   → tasks[*].evidence / summary / status
        ComposeStage   → report_markdown / itinerary / map_overview / finished_at
    """

    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    topic: str
    language: Literal["zh", "en"] = "zh"
    # ── 请求级覆盖（由 Agent 根据 HTTP 请求填入）──
    location_hint: str | None = None
    requested_max_tasks: int | None = None
    budget: str | None = None
    travel_date: str | None = None
    # ── 由各 stage 填入 ──
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    tasks: list[TaskNode] = Field(default_factory=list)
    report_markdown: str = ""
    itinerary: list[dict[str, Any]] = Field(default_factory=list)
    map_overview: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# SSE 事件（强类型）
# ---------------------------------------------------------------------------
class BaseEvent(BaseModel):
    """所有 SSE 事件的基类；``type`` 字段让前端可以 switch 分派。"""

    type: str
    timestamp: float = Field(default_factory=time.time)


class StatusEvent(BaseEvent):
    type: Literal["status"] = "status"
    message: str
    task_id: int | None = None


class PlanReadyEvent(BaseEvent):
    type: Literal["plan_ready"] = "plan_ready"
    run_id: str
    tasks: list[TaskNode]


class TaskUpdateEvent(BaseEvent):
    type: Literal["task_update"] = "task_update"
    task_id: int
    status: TaskStatus
    summary: str | None = None
    detail: str | None = None
    evidence: TaskEvidence | None = None


class SummaryChunkEvent(BaseEvent):
    """LLM 流式摘要的一个 chunk —— 前端按 task_id 拼接到该任务的 summary 里。"""

    type: Literal["summary_chunk"] = "summary_chunk"
    task_id: int
    content: str


class ToolCallEvent(BaseEvent):
    type: Literal["tool_call"] = "tool_call"
    task_id: int
    tool: str
    request: dict[str, Any]
    cached: bool = False


class ToolResultEvent(BaseEvent):
    type: Literal["tool_result"] = "tool_result"
    task_id: int
    tool: str
    place_count: int = 0
    route_count: int = 0
    duration_ms: int = 0
    error: str | None = None


class ReportEvent(BaseEvent):
    """最终报告就绪。包含 markdown / 行程 / 地图概览三件套。"""

    type: Literal["report"] = "report"
    markdown: str
    itinerary: list[dict[str, Any]] = Field(default_factory=list)
    map_overview: dict[str, Any] = Field(default_factory=dict)


class UsageEvent(BaseEvent):
    """整次 run 的累计开销。"""

    type: Literal["usage"] = "usage"
    llm_prompt_tokens: int
    llm_completion_tokens: int
    maps_api_calls: int
    elapsed_seconds: float


class ErrorEvent(BaseEvent):
    type: Literal["error"] = "error"
    detail: str
    task_id: int | None = None


class DoneEvent(BaseEvent):
    """流结束标志，前端收到后关闭 EventSource。"""

    type: Literal["done"] = "done"


# 联合类型，方便 FastAPI 端做类型提示
Event = (
    StatusEvent
    | PlanReadyEvent
    | TaskUpdateEvent
    | SummaryChunkEvent
    | ToolCallEvent
    | ToolResultEvent
    | ReportEvent
    | UsageEvent
    | ErrorEvent
    | DoneEvent
)


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------
class ResearchRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=500)
    max_tasks: int | None = Field(default=None, ge=1, le=10)
    language: Literal["zh", "en"] | None = None
    location_hint: Optional[str] = Field(
        default=None,
        description="可选的位置锚点，例如 'Tokyo, Japan'，作为 Maps 查询的先验偏置",
    )
    budget: Optional[str] = Field(
        default=None, max_length=120, description="可选预算，如 '人均 500 元' / '总预算 5000'"
    )
    travel_date: Optional[str] = Field(
        default=None, max_length=120, description="可选出行日期/时间，如 '2026-06-20' / '6 月下旬周末'"
    )


class ResearchResponse(BaseModel):
    run_id: str
    report_markdown: str
    itinerary: list[dict[str, Any]] = Field(default_factory=list)
    map_overview: dict[str, Any] = Field(default_factory=dict)
    tasks: list[TaskNode]


class UsageSnapshot(BaseModel):
    """``/usage`` 端点返回的累计统计。"""

    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    maps_api_calls: int = 0
    cache_hits: int = 0
