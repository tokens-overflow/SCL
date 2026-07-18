<script setup lang="ts">
import { ref } from "vue";

const emit = defineEmits<{
  (
    event: "submit",
    payload: {
      topic: string;
      language: "zh" | "en";
      maxTasks: number;
      budget: string | null;
      travelDate: string | null;
    }
  ): void;
}>();

const props = defineProps<{ disabled: boolean }>();

const topic = ref("");
const language = ref<"zh" | "en">("zh");
const maxTasks = ref(6);
const budget = ref("");
const travelDate = ref("");

const samples = [
  "东京涉谷 3 日游",
  "陆家嘴 3km 内最佳粤式餐厅",
  "京都赏枫一日深度路线",
];

function fill(s: string) {
  if (props.disabled) return;
  topic.value = s;
}

function onSubmit() {
  if (!topic.value.trim() || props.disabled) return;
  emit("submit", {
    topic: topic.value.trim(),
    language: language.value,
    maxTasks: maxTasks.value,
    budget: budget.value.trim() || null,
    travelDate: travelDate.value.trim() || null,
  });
}
</script>

<template>
  <form class="console" @submit.prevent="onSubmit">
    <p class="eyebrow">Research console</p>

    <div class="field">
      <textarea
        v-model="topic"
        class="input topic"
        :disabled="props.disabled"
        placeholder="想调研什么地方？例如「东京涉谷 3 日游」"
        rows="2"
        @keydown.meta.enter="onSubmit"
        @keydown.ctrl.enter="onSubmit"
      />
    </div>

    <div class="row">
      <input
        v-model="travelDate"
        class="input"
        :disabled="props.disabled"
        placeholder="何时去（可选）· 6 月下旬"
      />
      <input
        v-model="budget"
        class="input"
        :disabled="props.disabled"
        placeholder="预算（可选）· 人均 500"
      />
    </div>

    <div class="row">
      <label class="field seg-field">
        <span>语言</span>
        <div class="seg">
          <button
            type="button"
            :class="{ on: language === 'zh' }"
            :disabled="props.disabled"
            @click="language = 'zh'"
          >
            中文
          </button>
          <button
            type="button"
            :class="{ on: language === 'en' }"
            :disabled="props.disabled"
            @click="language = 'en'"
          >
            EN
          </button>
        </div>
      </label>

      <label class="field stepper-field">
        <span>子任务 · {{ maxTasks }}</span>
        <div class="seg">
          <button
            type="button"
            :disabled="props.disabled || maxTasks <= 3"
            @click="maxTasks = Math.max(3, maxTasks - 1)"
          >
            –
          </button>
          <span class="mono count">{{ maxTasks }}</span>
          <button
            type="button"
            :disabled="props.disabled || maxTasks >= 8"
            @click="maxTasks = Math.min(8, maxTasks + 1)"
          >
            +
          </button>
        </div>
      </label>
    </div>

    <button class="btn btn--primary submit" type="submit" :disabled="props.disabled">
      <span v-if="props.disabled" class="dot dot--run" />
      {{ props.disabled ? "研究进行中…" : "▸ 开始研究" }}
    </button>

    <div class="samples" v-if="!props.disabled">
      <button
        v-for="s in samples"
        :key="s"
        type="button"
        class="sample"
        @click="fill(s)"
      >
        {{ s }}
      </button>
    </div>
  </form>
</template>

<style scoped>
.console {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.topic {
  font-size: 0.95rem;
}
.row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.seg-field span,
.stepper-field span {
  font-size: 0.72rem;
  color: var(--text-mid);
  font-weight: 600;
}
.seg {
  display: flex;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--bg-0);
}
.seg button {
  flex: 1;
  background: transparent;
  border: none;
  padding: 8px 6px;
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--text-mid);
  transition: background 0.15s, color 0.15s;
}
.seg button + button {
  border-left: 1px solid var(--line);
}
.seg button.on {
  background: var(--accent-ghost);
  color: var(--accent);
}
.seg button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.stepper-field .count {
  flex: 1;
  text-align: center;
  font-size: 0.85rem;
  color: var(--text-hi);
}
.submit {
  width: 100%;
  padding: 12px;
  font-size: 0.92rem;
}
.samples {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.sample {
  font-size: 0.72rem;
  color: var(--text-mid);
  background: transparent;
  border: 1px dashed var(--line-strong);
  border-radius: 999px;
  padding: 4px 10px;
  transition: color 0.15s, border-color 0.15s;
}
.sample:hover {
  color: var(--accent);
  border-color: var(--accent-dim);
}
</style>
