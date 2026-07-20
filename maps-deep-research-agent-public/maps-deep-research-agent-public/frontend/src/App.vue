<script setup lang="ts">
import { computed, ref, watch } from "vue";
import TopicForm from "./components/TopicForm.vue";
import TaskTimeline from "./components/TaskTimeline.vue";
import PlacesView from "./components/PlacesView.vue";
import ItineraryView from "./components/ItineraryView.vue";
import ReportView from "./components/ReportView.vue";
import ActivityLog from "./components/ActivityLog.vue";
import HistoryPanel from "./components/HistoryPanel.vue";
import { startResearchStream } from "./services/sse";
import {
  allPlaces,
  handleEvent,
  researchState,
  resetState,
  type RunState,
} from "./stores/research";

const aborter = ref<AbortController | null>(null);
const historyOpen = ref(false);

type Tab = "places" | "itinerary" | "report" | "activity";
const tab = ref<Tab>("places");

const tabs = computed(() => [
  { key: "places" as Tab, label: "佳处", en: "Places", n: allPlaces.value.length },
  { key: "itinerary" as Tab, label: "行程", en: "Plan", n: researchState.itinerary.length },
  { key: "report" as Tab, label: "手记", en: "Report" },
  { key: "activity" as Tab, label: "行迹", en: "Log", n: researchState.activity.length },
]);

const isRunning = computed(() => researchState.status === "running");

// 报告生成后自动切到「手记」
watch(
  () => researchState.reportMarkdown,
  (md) => {
    if (md) tab.value = "report";
  }
);

async function onSubmit(payload: {
  topic: string;
  language: "zh" | "en";
  maxTasks: number;
  budget: string | null;
  travelDate: string | null;
}) {
  if (aborter.value) aborter.value.abort();
  aborter.value = new AbortController();
  resetState(payload.topic, payload.language);
  tab.value = "places";

  try {
    await startResearchStream(
      {
        topic: payload.topic,
        language: payload.language,
        max_tasks: payload.maxTasks,
        budget: payload.budget,
        travel_date: payload.travelDate,
      },
      { signal: aborter.value.signal, onEvent: handleEvent }
    );
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    researchState.status = "failed";
    researchState.error = (err as Error).message;
  }
}

function stopRun() {
  aborter.value?.abort();
  if (researchState.status === "running") researchState.status = "failed";
  researchState.error = researchState.error || "已手动停止";
}

function statusMeta(state: RunState) {
  switch (state.status) {
    case "running":
      return { dot: "dot--run", text: state.statusMessage || "运行中…" };
    case "succeeded":
      return { dot: "dot--ok", text: "考竟" };
    case "failed":
      return { dot: "dot--err", text: state.error || "失败" };
    default:
      return { dot: "", text: "待题主题" };
  }
}
</script>

<template>
  <div class="shell">
    <!-- ░░ top bar ░░ -->
    <header class="topbar panel">
      <div class="brand">
        <span class="seal logo">卧<br />游</span>
        <div class="brand-txt">
          <h1>卧游<span class="brand-en">· Cartograph</span></h1>
          <small>不出户而神游山水 · DeepSeek × Google Maps</small>
        </div>
      </div>

      <div class="status-wrap">
        <div class="status">
          <span class="dot" :class="statusMeta(researchState).dot" />
          <span class="status-txt">{{ statusMeta(researchState).text }}</span>
        </div>
        <div class="usage mono" v-if="researchState.usage">
          <span>{{ (researchState.usage.llm_prompt_tokens + researchState.usage.llm_completion_tokens).toLocaleString() }} tok</span>
          <span class="sep">·</span>
          <span>{{ researchState.usage.maps_api_calls }} maps</span>
          <span class="sep">·</span>
          <span>{{ researchState.usage.elapsed_seconds.toFixed(1) }}s</span>
        </div>
        <button v-if="isRunning" class="btn btn--ghost btn--sm" @click="stopRun">停止</button>
        <button class="btn btn--ghost btn--sm" @click="historyOpen = true">旧游</button>
      </div>
    </header>

    <!-- ░░ left rail ░░ -->
    <aside class="rail panel">
      <TopicForm :disabled="isRunning" @submit="onSubmit" />
      <div class="divider" />
      <TaskTimeline :tasks="researchState.tasks" />
    </aside>

    <!-- ░░ center stage ░░ -->
    <main class="stage panel">
      <nav class="tabs">
        <button
          v-for="t in tabs"
          :key="t.key"
          class="tab"
          :class="{ on: tab === t.key }"
          @click="tab = t.key"
        >
          <span class="t-label">{{ t.label }}</span>
          <span class="t-en mono">{{ t.en }}</span>
          <span class="t-n mono" v-if="t.n">{{ t.n }}</span>
        </button>
      </nav>

      <div class="stage-body">
        <PlacesView v-show="tab === 'places'" />
        <ItineraryView v-show="tab === 'itinerary'" :itinerary="researchState.itinerary" />
        <ReportView v-show="tab === 'report'" :markdown="researchState.reportMarkdown" />
        <ActivityLog v-show="tab === 'activity'" />
      </div>
    </main>

    <HistoryPanel :open="historyOpen" @close="historyOpen = false" />
  </div>
</template>

<style scoped>
.shell {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: 360px 1fr;
  grid-template-rows: auto 1fr;
  gap: 14px;
  padding: 14px;
  height: 100vh;
}

/* top bar */
.topbar {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 18px;
  gap: 16px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 13px;
}
.logo {
  width: 44px;
  height: 44px;
  font-size: 1rem;
  line-height: 1.05;
  text-align: center;
  letter-spacing: 0;
}
.brand-txt h1 {
  margin: 0;
  font-family: var(--font-brush);
  font-size: 1.5rem;
  font-weight: 400;
  letter-spacing: 2px;
  color: var(--text-hi);
}
.brand-en {
  font-family: var(--font-display);
  font-size: 0.95rem;
  color: var(--text-lo);
  letter-spacing: 0.5px;
  margin-left: 6px;
}
.brand-txt small {
  display: block;
  color: var(--text-mid);
  font-size: 0.74rem;
  margin-top: 1px;
}
.status-wrap {
  display: flex;
  align-items: center;
  gap: 16px;
}
.status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.86rem;
}
.status-txt {
  color: var(--text-hi);
  font-weight: 600;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.usage {
  display: flex;
  gap: 6px;
  font-size: 0.72rem;
  color: var(--text-mid);
}
.usage .sep {
  color: var(--line-strong);
}
.btn--sm {
  padding: 6px 12px;
  font-size: 0.78rem;
}

/* rail */
.rail {
  padding: 18px;
  overflow: auto;
  display: flex;
  flex-direction: column;
}
.divider {
  height: 1px;
  margin: 18px 0;
  background: linear-gradient(90deg, transparent, var(--line-strong), transparent);
}

/* stage */
.stage {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 0;
}
.tabs {
  display: flex;
  gap: 2px;
  padding: 8px 8px 0;
  border-bottom: 1px solid var(--line);
}
.tab {
  position: relative;
  display: flex;
  align-items: baseline;
  gap: 7px;
  background: transparent;
  border: none;
  padding: 11px 16px;
  color: var(--text-mid);
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  transition: color 0.15s, background 0.15s;
}
.tab:hover {
  color: var(--text-hi);
  background: var(--bg-2);
}
.tab.on {
  color: var(--accent);
}
.tab.on::after {
  content: "";
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: -1px;
  height: 2px;
  background: var(--accent);
  border-radius: 2px 2px 0 0;
}
.t-label {
  font-family: var(--font-brush);
  font-size: 1.05rem;
  letter-spacing: 1px;
}
.t-en {
  font-size: 0.62rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--text-lo);
}
.tab.on .t-en {
  color: var(--accent-dim);
}
.t-n {
  font-size: 0.62rem;
  color: #fdf6ec;
  background: var(--accent);
  border-radius: 999px;
  padding: 0 6px;
  line-height: 1.5;
}
.stage-body {
  flex: 1;
  min-height: 0;
  padding: 18px;
  overflow: hidden;
}
.stage-body > * {
  height: 100%;
}

@media (max-width: 920px) {
  .shell {
    grid-template-columns: 1fr;
    grid-template-rows: auto auto 1fr;
    height: auto;
    min-height: 100vh;
  }
  .rail {
    max-height: none;
  }
  .stage {
    min-height: 70vh;
  }
}
</style>
