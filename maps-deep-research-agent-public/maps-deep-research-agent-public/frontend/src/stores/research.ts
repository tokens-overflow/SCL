/**
 * Reactive store for a research run.
 * Holds tasks, streaming summaries, final report, a live tool-call activity
 * log, and derived progress.
 */

import { computed, reactive } from "vue";
import type {
  ItineraryDay,
  MapOverview,
  Place,
  ServerEvent,
  TaskNode,
} from "../types/events";
import { loadHistory, saveSnapshot, type RunSnapshot } from "../services/history";

export interface UsageState {
  llm_prompt_tokens: number;
  llm_completion_tokens: number;
  maps_api_calls: number;
  elapsed_seconds: number;
}

export interface ActivityEntry {
  id: number;
  taskId: number;
  tool: string;
  request: Record<string, unknown>;
  cached: boolean;
  status: "pending" | "done" | "error";
  placeCount: number;
  routeCount: number;
  durationMs: number;
  error: string | null;
  at: number;
}

export interface RunState {
  runId: string;
  topic: string;
  language: "zh" | "en";
  status: "idle" | "running" | "succeeded" | "failed";
  statusMessage: string;
  tasks: TaskNode[];
  reportMarkdown: string;
  itinerary: ItineraryDay[];
  mapOverview: MapOverview;
  usage: UsageState | null;
  error: string | null;
  activity: ActivityEntry[];
  streamingTaskId: number | null;
}

function emptyState(): RunState {
  return {
    runId: "",
    topic: "",
    language: "zh",
    status: "idle",
    statusMessage: "",
    tasks: [],
    reportMarkdown: "",
    itinerary: [],
    mapOverview: {},
    usage: null,
    error: null,
    activity: [],
    streamingTaskId: null,
  };
}

export const researchState = reactive<RunState>(emptyState());

let activitySeq = 0;

export function resetState(topic: string, language: "zh" | "en") {
  Object.assign(researchState, emptyState());
  researchState.topic = topic;
  researchState.language = language;
  researchState.status = "running";
  researchState.statusMessage = language === "zh" ? "连接中…" : "Connecting…";
  activitySeq = 0;
}

/* ---- derived selectors ---- */

function scorePlace(p: Place): number {
  return (
    (p.rating != null ? 2 : 0) +
    (p.opening_hours?.length ? 1 : 0) +
    (p.website ? 1 : 0) +
    (p.phone ? 1 : 0) +
    (p.photo_reference ? 1 : 0)
  );
}

/** All evidence places across tasks, de-duplicated by place_id, keeping richest. */
export const allPlaces = computed<Place[]>(() => {
  const map = new Map<string, Place>();
  for (const task of researchState.tasks) {
    for (const p of task.evidence?.places ?? []) {
      const prev = map.get(p.place_id);
      if (!prev || scorePlace(p) > scorePlace(prev)) map.set(p.place_id, p);
    }
  }
  return [...map.values()];
});

export const progress = computed(() => {
  const tasks = researchState.tasks;
  const total = tasks.length;
  const done = tasks.filter(
    (t) => t.status === "completed" || t.status === "skipped"
  ).length;
  const failed = tasks.filter((t) => t.status === "failed").length;
  const ratio = total ? done / total : 0;
  return { total, done, failed, ratio };
});

export const cacheStats = computed(() => {
  const calls = researchState.activity;
  const hits = calls.filter((a) => a.cached).length;
  return { calls: calls.length, hits };
});

/* ---- event handling ---- */

export function handleEvent(event: ServerEvent) {
  switch (event.type) {
    case "status":
      researchState.statusMessage = event.message;
      break;

    case "plan_ready":
      researchState.runId = event.run_id;
      researchState.tasks = event.tasks.map((t) => ({
        ...t,
        summary: t.summary || "",
      }));
      researchState.statusMessage =
        researchState.language === "zh" ? "已生成研究计划" : "Plan ready";
      break;

    case "task_update": {
      const task = researchState.tasks.find((t) => t.id === event.task_id);
      if (!task) return;
      task.status = event.status;
      if (event.status === "in_progress")
        researchState.streamingTaskId = event.task_id;
      if (event.summary) task.summary = event.summary;
      if (event.evidence) task.evidence = event.evidence;
      if (event.detail) task.error = event.detail;
      if (
        researchState.streamingTaskId === event.task_id &&
        event.status !== "in_progress"
      )
        researchState.streamingTaskId = null;
      break;
    }

    case "summary_chunk": {
      const task = researchState.tasks.find((t) => t.id === event.task_id);
      if (!task) return;
      researchState.streamingTaskId = event.task_id;
      task.summary = (task.summary || "") + event.content;
      break;
    }

    case "tool_call":
      researchState.activity.push({
        id: ++activitySeq,
        taskId: event.task_id,
        tool: event.tool,
        request: event.request,
        cached: event.cached,
        status: "pending",
        placeCount: 0,
        routeCount: 0,
        durationMs: 0,
        error: null,
        at: event.timestamp,
      });
      break;

    case "tool_result": {
      for (let i = researchState.activity.length - 1; i >= 0; i--) {
        const a = researchState.activity[i];
        if (
          a.taskId === event.task_id &&
          a.tool === event.tool &&
          a.status === "pending"
        ) {
          a.status = event.error ? "error" : "done";
          a.placeCount = event.place_count;
          a.routeCount = event.route_count;
          a.durationMs = event.duration_ms;
          a.error = event.error ?? null;
          break;
        }
      }
      break;
    }

    case "report":
      researchState.reportMarkdown = event.markdown;
      researchState.itinerary = event.itinerary;
      researchState.mapOverview = event.map_overview;
      researchState.statusMessage =
        researchState.language === "zh" ? "报告已生成" : "Report ready";
      break;

    case "usage":
      researchState.usage = {
        llm_prompt_tokens: event.llm_prompt_tokens,
        llm_completion_tokens: event.llm_completion_tokens,
        maps_api_calls: event.maps_api_calls,
        elapsed_seconds: event.elapsed_seconds,
      };
      break;

    case "error":
      researchState.status = "failed";
      researchState.error = event.detail;
      break;

    case "done":
      researchState.streamingTaskId = null;
      if (researchState.status === "running")
        researchState.status = researchState.error ? "failed" : "succeeded";
      persistRun();
      break;
  }
}

function persistRun() {
  if (!researchState.runId) return;
  const snap: RunSnapshot = {
    runId: researchState.runId,
    topic: researchState.topic,
    language: researchState.language,
    savedAt: Date.now(),
    status: researchState.status === "succeeded" ? "succeeded" : "failed",
    tasks: JSON.parse(JSON.stringify(researchState.tasks)),
    reportMarkdown: researchState.reportMarkdown,
    itinerary: researchState.itinerary,
    mapOverview: researchState.mapOverview,
    usage: researchState.usage,
  };
  saveSnapshot(snap);
}

/** Restore a saved run into the live state (read-only view of past results). */
export function restoreSnapshot(snap: RunSnapshot) {
  Object.assign(researchState, emptyState());
  researchState.runId = snap.runId;
  researchState.topic = snap.topic;
  researchState.language = snap.language;
  researchState.status = snap.status;
  researchState.tasks = snap.tasks;
  researchState.reportMarkdown = snap.reportMarkdown;
  researchState.itinerary = snap.itinerary;
  researchState.mapOverview = snap.mapOverview;
  researchState.usage = snap.usage;
  researchState.statusMessage =
    snap.language === "zh" ? "已载入历史记录" : "Loaded from history";
}

export function listHistory() {
  return loadHistory();
}
