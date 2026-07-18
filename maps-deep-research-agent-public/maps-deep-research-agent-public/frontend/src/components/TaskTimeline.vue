<script setup lang="ts">
import { computed } from "vue";
import type { TaskNode } from "../types/events";
import { progress, researchState } from "../stores/research";

const props = defineProps<{ tasks: TaskNode[] }>();

const ordered = computed(() => [...props.tasks].sort((a, b) => a.id - b.id));

const toolLabel: Record<string, string> = {
  places: "寻址",
  geocoding: "定位",
  directions: "问途",
  distance_matrix: "测距",
};

function statusInfo(t: TaskNode) {
  switch (t.status) {
    case "in_progress":
      return { cls: "run", dot: "dot--run", label: "进行中" };
    case "completed":
      return { cls: "ok", dot: "dot--ok", label: "已成" };
    case "failed":
      return { cls: "err", dot: "dot--err", label: "失败" };
    case "skipped":
      return { cls: "skip", dot: "", label: "略过" };
    default:
      return { cls: "wait", dot: "", label: "待行" };
  }
}
</script>

<template>
  <section class="timeline">
    <header class="tl-head">
      <p class="eyebrow">行程脉络 · Task DAG</p>
      <span class="mono count" v-if="progress.total">
        {{ progress.done }}/{{ progress.total }}
      </span>
    </header>

    <div class="bar" v-if="progress.total">
      <div class="bar__fill" :style="{ width: `${progress.ratio * 100}%` }" />
    </div>

    <div v-if="ordered.length === 0" class="empty">○ 山水未铺，先题一句研究主题</div>

    <ol class="tl">
      <li
        v-for="task in ordered"
        :key="task.id"
        :class="['tl-item', `is-${statusInfo(task).cls}`, { live: researchState.streamingTaskId === task.id }]"
      >
        <span class="rail">
          <span class="node"><span class="dot" :class="statusInfo(task).dot" /></span>
        </span>

        <div class="body">
          <div class="title-row">
            <span class="id mono">{{ String(task.id).padStart(2, "0") }}</span>
            <span class="title">{{ task.title }}</span>
          </div>

          <div class="meta">
            <span class="chip">{{ toolLabel[task.tool] || task.tool }}</span>
            <span class="chip" :class="{ 'chip--accent': task.status === 'in_progress' }">
              {{ statusInfo(task).label }}
            </span>
            <span v-if="task.depends_on?.length" class="dep mono">
              承 {{ task.depends_on.map((d) => "#" + d).join(" ") }}
            </span>
          </div>

          <p class="intent">{{ task.intent }}</p>

          <p
            v-if="task.summary"
            class="summary"
            :class="{ streaming: researchState.streamingTaskId === task.id }"
          >
            {{ task.summary }}<span
              v-if="researchState.streamingTaskId === task.id"
              class="caret"
            />
          </p>

          <p v-if="task.error" class="err-msg">⚠ {{ task.error }}</p>
        </div>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.timeline {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.tl-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.count {
  font-size: 0.74rem;
  color: var(--text-mid);
}
.bar {
  height: 2px;
  border-radius: 99px;
  background: var(--line);
  overflow: hidden;
}
.bar__fill {
  height: 100%;
  border-radius: 99px;
  background: linear-gradient(90deg, var(--accent-2), var(--accent));
  transition: width 0.6s cubic-bezier(0.2, 0.7, 0.3, 1);
}

.tl {
  list-style: none;
  margin: 0;
  padding: 0;
}
.tl-item {
  display: grid;
  grid-template-columns: 22px 1fr;
  gap: 10px;
  animation: rise 0.4s both;
}
.rail {
  position: relative;
  display: flex;
  justify-content: center;
}
.rail::before {
  content: "";
  position: absolute;
  top: 18px;
  bottom: -8px;
  width: 1px;
  background: repeating-linear-gradient(
    to bottom,
    var(--line-strong) 0 4px,
    transparent 4px 8px
  );
}
.tl-item:last-child .rail::before {
  display: none;
}
.node {
  position: relative;
  z-index: 1;
  width: 16px;
  height: 16px;
  margin-top: 4px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: var(--bg-3);
  border: 1px solid var(--line-strong);
}
.is-ok .node { border-color: var(--jade); }
.is-run .node { border-color: var(--run); box-shadow: 0 0 0 3px rgba(90, 125, 146, 0.15); }
.is-err .node { border-color: var(--err); }

.body {
  padding-bottom: 16px;
  min-width: 0;
}
.title-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.id {
  font-size: 0.68rem;
  color: var(--accent);
  opacity: 0.7;
}
.title {
  font-weight: 600;
  font-size: 0.95rem;
  color: var(--text-hi);
  line-height: 1.3;
}
.is-skip .title,
.is-wait .title {
  color: var(--text-mid);
}
.meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin: 7px 0;
}
.dep {
  font-size: 0.66rem;
  color: var(--text-lo);
}
.intent {
  margin: 0;
  font-size: 0.82rem;
  color: var(--text-mid);
  line-height: 1.5;
}
.summary {
  margin: 8px 0 0;
  font-size: 0.82rem;
  line-height: 1.6;
  color: var(--text-mid);
  background: var(--bg-3);
  border: 1px solid var(--line-soft);
  border-left: 2px solid var(--accent-dim);
  border-radius: var(--radius-xs);
  padding: 8px 10px;
  max-height: 170px;
  overflow: auto;
  white-space: pre-wrap;
}
.summary.streaming {
  border-left-color: var(--accent);
}
.caret {
  display: inline-block;
  width: 6px;
  height: 0.95em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--accent);
  animation: pulse 0.9s steps(1) infinite;
}
.err-msg {
  margin: 7px 0 0;
  font-size: 0.78rem;
  color: var(--err);
}
</style>
