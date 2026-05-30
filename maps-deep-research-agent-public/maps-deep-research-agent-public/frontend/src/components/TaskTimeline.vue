<script setup lang="ts">
import { computed } from "vue";
import type { TaskNode } from "../types/events";

const props = defineProps<{ tasks: TaskNode[] }>();

const ordered = computed(() => [...props.tasks].sort((a, b) => a.id - b.id));

function statusLabel(t: TaskNode): string {
  switch (t.status) {
    case "in_progress":
      return "进行中";
    case "completed":
      return "完成";
    case "failed":
      return "失败";
    case "skipped":
      return "跳过";
    default:
      return "待执行";
  }
}
</script>

<template>
  <section>
    <h2 class="panel-title">任务时间线</h2>
    <div v-if="ordered.length === 0" class="empty">尚未规划任务</div>
    <article
      v-for="task in ordered"
      :key="task.id"
      :class="['task-card', `status-${task.status}`]"
    >
      <header>
        <strong>#{{ task.id }} {{ task.title }}</strong>
        <span class="tag">{{ task.tool }}</span>
        <span class="tag">{{ statusLabel(task) }}</span>
      </header>
      <p class="task-card__intent">{{ task.intent }}</p>
      <details v-if="task.summary">
        <summary>任务总结</summary>
        <pre class="task-card__summary">{{ task.summary }}</pre>
      </details>
      <p v-if="task.error" class="task-card__error">{{ task.error }}</p>
    </article>
  </section>
</template>

<style scoped>
.empty {
  color: #94a3b8;
  font-size: 0.9rem;
}
.task-card__intent {
  margin: 4px 0;
  color: #475569;
  font-size: 0.85rem;
}
.task-card__summary {
  white-space: pre-wrap;
  font-family: inherit;
  font-size: 0.85rem;
  margin: 4px 0 0;
}
.task-card__error {
  color: #dc2626;
  font-size: 0.85rem;
}
</style>
