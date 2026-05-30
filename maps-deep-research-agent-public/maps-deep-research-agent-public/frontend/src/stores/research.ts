/**
 * Tiny reactive store backed by Vue's reactivity APIs (no Pinia dependency).
 * Holds the per-run state: tasks, streaming summaries, final report, map overview.
 */

import { reactive } from "vue";
import type {
  ItineraryDay,
  MapOverview,
  ServerEvent,
  TaskNode,
} from "../types/events";

export interface UsageState {
  llm_prompt_tokens: number;
  llm_completion_tokens: number;
  maps_api_calls: number;
  elapsed_seconds: number;
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
  };
}

export const researchState = reactive<RunState>(emptyState());

export function resetState(topic: string, language: "zh" | "en") {
  Object.assign(researchState, emptyState());
  researchState.topic = topic;
  researchState.language = language;
  researchState.status = "running";
  researchState.statusMessage = language === "zh" ? "连接中..." : "Connecting...";
}

export function handleEvent(event: ServerEvent) {
  switch (event.type) {
    case "status":
      researchState.statusMessage = event.message;
      break;
    case "plan_ready":
      researchState.runId = event.run_id;
      researchState.tasks = event.tasks.map((t) => ({ ...t, summary: t.summary || "" }));
      researchState.statusMessage = "已生成研究计划";
      break;
    case "task_update": {
      const task = researchState.tasks.find((t) => t.id === event.task_id);
      if (!task) return;
      task.status = event.status;
      if (event.summary) task.summary = event.summary;
      if (event.evidence) task.evidence = event.evidence;
      if (event.detail) task.error = event.detail;
      break;
    }
    case "summary_chunk": {
      const task = researchState.tasks.find((t) => t.id === event.task_id);
      if (!task) return;
      task.summary = (task.summary || "") + event.content;
      break;
    }
    case "tool_call":
    case "tool_result":
      // could surface in a "tools log" panel; ignored in MVP
      break;
    case "report":
      researchState.reportMarkdown = event.markdown;
      researchState.itinerary = event.itinerary;
      researchState.mapOverview = event.map_overview;
      researchState.statusMessage = "报告已生成";
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
      if (researchState.status === "running") {
        researchState.status = researchState.error ? "failed" : "succeeded";
      }
      break;
  }
}
