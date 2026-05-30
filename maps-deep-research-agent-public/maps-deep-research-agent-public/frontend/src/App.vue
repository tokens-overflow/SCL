<script setup lang="ts">
import { computed, ref } from "vue";
import TopicForm from "./components/TopicForm.vue";
import TaskTimeline from "./components/TaskTimeline.vue";
import MapView from "./components/MapView.vue";
import ItineraryView from "./components/ItineraryView.vue";
import ReportView from "./components/ReportView.vue";
import { startResearchStream } from "./services/sse";
import {
  handleEvent,
  researchState,
  resetState,
  type RunState,
} from "./stores/research";

const aborter = ref<AbortController | null>(null);

const isRunning = computed(() => researchState.status === "running");

async function onSubmit(payload: {
  topic: string;
  language: "zh" | "en";
  maxTasks: number;
  locationHint: string | null;
}) {
  if (aborter.value) aborter.value.abort();
  aborter.value = new AbortController();
  resetState(payload.topic, payload.language);

  try {
    await startResearchStream(
      {
        topic: payload.topic,
        language: payload.language,
        max_tasks: payload.maxTasks,
        location_hint: payload.locationHint,
      },
      {
        signal: aborter.value.signal,
        onEvent: handleEvent,
      }
    );
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    researchState.status = "failed";
    researchState.error = (err as Error).message;
  }
}

function statusLabel(state: RunState): string {
  switch (state.status) {
    case "running":
      return state.statusMessage || "运行中...";
    case "succeeded":
      return "已完成";
    case "failed":
      return state.error || "失败";
    default:
      return "等待输入";
  }
}
</script>

<template>
  <div class="app-shell">
    <header class="app-shell__header">
      <div>
        <h1 style="margin: 0; font-size: 1.15rem">🗺️ Maps Deep Research Agent</h1>
        <small style="color: #64748b"
          >DeepSeek + Google Maps Platform · 任务 DAG · SSE 流式</small
        >
      </div>
      <div style="text-align: right">
        <div>
          状态：<strong>{{ statusLabel(researchState) }}</strong>
        </div>
        <small v-if="researchState.usage" style="color: #64748b">
          tokens {{ researchState.usage.llm_prompt_tokens }}+{{
            researchState.usage.llm_completion_tokens
          }}
          · Maps {{ researchState.usage.maps_api_calls }} ·
          {{ researchState.usage.elapsed_seconds.toFixed(1) }}s
        </small>
      </div>
    </header>

    <aside class="app-shell__sidebar">
      <TopicForm :disabled="isRunning" @submit="onSubmit" />
      <hr style="margin: 16px 0; border: none; border-top: 1px solid #e2e8f0" />
      <TaskTimeline :tasks="researchState.tasks" />
    </aside>

    <main class="app-shell__main">
      <MapView :overview="researchState.mapOverview" />
      <ItineraryView :itinerary="researchState.itinerary" />
      <ReportView :markdown="researchState.reportMarkdown" />
    </main>
  </div>
</template>
