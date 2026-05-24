"""Domain models: research state, task DAG, and SSE event envelopes.

Compared to chapter 14:
- Tasks belong to a DAG: each task may declare ``depends_on`` ids.
- Every SSE event is a *typed* Pydantic model (the original used loose dicts).
- ``ResearchState`` keeps an immutable id so the frontend can correlate runs.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


TaskStatus = Literal["pending", "in_progress", "completed", "skipped", "failed"]


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------
class Place(BaseModel):
    """A normalized place returned from the Google Maps tool."""

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


class RouteLeg(BaseModel):
    origin: str
    destination: str
    mode: str
    distance_meters: int
    duration_seconds: int
    polyline: str | None = None


class TaskEvidence(BaseModel):
    """Maps-derived evidence aggregated for a single task."""

    places: list[Place] = Field(default_factory=list)
    routes: list[RouteLeg] = Field(default_factory=list)
    raw_calls: int = 0
    notes: list[str] = Field(default_factory=list)


class TaskNode(BaseModel):
    """A node in the research DAG."""

    id: int
    title: str = Field(description="Short task name (<=12 chars)")
    intent: str = Field(description="What the task is trying to answer")
    query: str = Field(description="Free-form query passed to Maps tools")
    tool: Literal["places", "directions", "geocoding", "distance_matrix"] = "places"
    tool_args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)

    status: TaskStatus = "pending"
    summary: str = ""
    evidence: TaskEvidence = Field(default_factory=TaskEvidence)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None


class ResearchState(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    topic: str
    language: Literal["zh", "en"] = "zh"
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    tasks: list[TaskNode] = Field(default_factory=list)
    report_markdown: str = ""
    itinerary: list[dict[str, Any]] = Field(default_factory=list)
    map_overview: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# SSE Events (typed)
# ---------------------------------------------------------------------------
class BaseEvent(BaseModel):
    """All SSE events inherit from here so the frontend can switch on ``type``."""

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
    type: Literal["report"] = "report"
    markdown: str
    itinerary: list[dict[str, Any]] = Field(default_factory=list)
    map_overview: dict[str, Any] = Field(default_factory=dict)


class UsageEvent(BaseEvent):
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
    type: Literal["done"] = "done"


# Convenience union
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
# Request / Response models
# ---------------------------------------------------------------------------
class ResearchRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=500)
    max_tasks: int | None = Field(default=None, ge=1, le=10)
    language: Literal["zh", "en"] | None = None
    location_hint: Optional[str] = Field(
        default=None,
        description="Optional anchor location, e.g. 'Tokyo, Japan'. Used as a prior for Maps queries.",
    )


class ResearchResponse(BaseModel):
    run_id: str
    report_markdown: str
    itinerary: list[dict[str, Any]] = Field(default_factory=list)
    map_overview: dict[str, Any] = Field(default_factory=dict)
    tasks: list[TaskNode]


class UsageSnapshot(BaseModel):
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    maps_api_calls: int = 0
    cache_hits: int = 0
