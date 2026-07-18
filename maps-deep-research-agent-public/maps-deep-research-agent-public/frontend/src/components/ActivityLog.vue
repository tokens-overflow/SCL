<script setup lang="ts">
import { computed } from "vue";
import { cacheStats, researchState } from "../stores/research";

const toolLabel: Record<string, string> = {
  places: "寻址 places",
  geocoding: "定位 geocoding",
  directions: "问途 directions",
  distance_matrix: "测距 distance",
};

// newest first
const entries = computed(() => [...researchState.activity].reverse());

function summarize(req: Record<string, unknown>): string {
  const q = (req.query ?? req.address ?? req.origin ?? req.keyword) as string | undefined;
  return q ? String(q) : Object.values(req).slice(0, 1).map(String).join("");
}
</script>

<template>
  <section class="log">
    <header class="lh">
      <p class="eyebrow">行迹 · Activity</p>
      <span class="stat mono" v-if="cacheStats.calls">
        {{ cacheStats.calls }} 次调用 · 命中 {{ cacheStats.hits }}
      </span>
    </header>

    <div v-if="!entries.length" class="empty">○ 每一次对外查询都会在此留痕</div>

    <ul class="rows">
      <li v-for="a in entries" :key="a.id" class="row rise" :class="`is-${a.status}`">
        <span
          class="dot"
          :class="a.status === 'error' ? 'dot--err' : a.status === 'pending' ? 'dot--run' : 'dot--ok'"
        />
        <div class="row-main">
          <div class="top">
            <span class="tool">{{ toolLabel[a.tool] || a.tool }}</span>
            <span class="chip" :class="{ 'chip--accent': a.cached }">{{
              a.cached ? "缓存" : "实时"
            }}</span>
            <span class="task mono">#{{ a.taskId }}</span>
          </div>
          <p class="q" v-if="summarize(a.request)">{{ summarize(a.request) }}</p>
          <div class="bottom mono">
            <span v-if="a.status !== 'pending'">{{ a.durationMs }}ms</span>
            <span v-if="a.placeCount">· {{ a.placeCount }} 地点</span>
            <span v-if="a.routeCount">· {{ a.routeCount }} 路线</span>
            <span v-if="a.status === 'pending'" class="pending">查询中…</span>
            <span v-if="a.error" class="err">· {{ a.error }}</span>
          </div>
        </div>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.log {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.lh {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.stat {
  font-size: 0.7rem;
  color: var(--text-mid);
}
.rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow: auto;
}
.row {
  display: grid;
  grid-template-columns: 14px 1fr;
  gap: 9px;
  padding: 9px 11px;
  background: var(--bg-3);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm);
}
.row .dot {
  margin-top: 5px;
}
.is-error {
  border-color: var(--accent-dim);
}
.row-main {
  min-width: 0;
}
.top {
  display: flex;
  align-items: center;
  gap: 7px;
}
.tool {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--text-hi);
}
.task {
  margin-left: auto;
  font-size: 0.68rem;
  color: var(--text-lo);
}
.q {
  margin: 4px 0 0;
  font-size: 0.78rem;
  color: var(--text-mid);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bottom {
  margin-top: 4px;
  font-size: 0.68rem;
  color: var(--text-lo);
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
}
.pending {
  color: var(--run);
}
.err {
  color: var(--err);
}
</style>
