<script setup lang="ts">
import { ref, watch } from "vue";
import { loadHistory, removeSnapshot, type RunSnapshot } from "../services/history";
import { restoreSnapshot } from "../stores/research";

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ (e: "close"): void }>();

const items = ref<RunSnapshot[]>([]);

watch(
  () => props.open,
  (o) => {
    if (o) items.value = loadHistory();
  }
);

function relTime(ts: number): string {
  const d = Math.floor((Date.now() - ts) / 1000);
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}

function openSnap(snap: RunSnapshot) {
  restoreSnapshot(snap);
  emit("close");
}

function del(snap: RunSnapshot, e: Event) {
  e.stopPropagation();
  items.value = removeSnapshot(snap.runId);
}
</script>

<template>
  <transition name="fade">
    <div v-if="open" class="scrim" @click="emit('close')">
      <aside class="drawer panel" @click.stop>
        <header class="dh">
          <p class="eyebrow">旧游 · History</p>
          <button class="btn btn--ghost btn--icon" @click="emit('close')">✕</button>
        </header>

        <div v-if="!items.length" class="empty">○ 尚无旧游可追</div>

        <ul class="list">
          <li v-for="s in items" :key="s.runId" class="entry" @click="openSnap(s)">
            <span class="dot" :class="s.status === 'succeeded' ? 'dot--ok' : 'dot--err'" />
            <div class="info">
              <span class="topic">{{ s.topic }}</span>
              <span class="sub mono">
                {{ relTime(s.savedAt) }}
                <template v-if="s.usage"> · {{ s.usage.elapsed_seconds.toFixed(0) }}s · {{ s.tasks.length }} 任务</template>
              </span>
            </div>
            <button class="del" @click="del(s, $event)" title="删除">⌫</button>
          </li>
        </ul>
      </aside>
    </div>
  </transition>
</template>

<style scoped>
.scrim {
  position: fixed;
  inset: 0;
  z-index: 40;
  background: rgba(42, 38, 29, 0.32);
  backdrop-filter: blur(2px);
  display: flex;
  justify-content: flex-end;
}
.drawer {
  width: min(360px, 88vw);
  height: 100%;
  border-radius: 0;
  border-left: 1px solid var(--line-strong);
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  animation: slidein 0.28s cubic-bezier(0.2, 0.7, 0.3, 1);
}
@keyframes slidein {
  from { transform: translateX(20px); opacity: 0.6; }
  to { transform: none; opacity: 1; }
}
.dh {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow: auto;
}
.entry {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 12px;
  background: var(--bg-3);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: border-color 0.15s, transform 0.12s;
}
.entry:hover {
  border-color: var(--accent-dim);
  transform: translateX(-2px);
}
.entry .dot {
  flex: none;
}
.info {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  flex: 1;
}
.topic {
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--text-hi);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sub {
  font-size: 0.68rem;
  color: var(--text-lo);
}
.del {
  flex: none;
  background: transparent;
  border: none;
  color: var(--text-lo);
  font-size: 1rem;
  padding: 4px;
  border-radius: 5px;
}
.del:hover {
  color: var(--err);
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.22s;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
