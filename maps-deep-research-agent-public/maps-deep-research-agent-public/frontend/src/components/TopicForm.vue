<script setup lang="ts">
import { ref } from "vue";

const emit = defineEmits<{
  (event: "submit", payload: { topic: string; language: "zh" | "en"; maxTasks: number; locationHint: string | null }): void;
}>();

const props = defineProps<{ disabled: boolean }>();

const topic = ref("");
const language = ref<"zh" | "en">("zh");
const maxTasks = ref(5);
const locationHint = ref("");

function onSubmit() {
  if (!topic.value.trim()) return;
  emit("submit", {
    topic: topic.value.trim(),
    language: language.value,
    maxTasks: maxTasks.value,
    locationHint: locationHint.value.trim() || null,
  });
}
</script>

<template>
  <form class="topic-form" @submit.prevent="onSubmit">
    <h2 class="panel-title">研究主题</h2>
    <textarea
      v-model="topic"
      class="topic-form__input"
      placeholder="例如：东京涉谷 3 日游 / 陆家嘴 3km 内最佳粤式餐厅"
      rows="3"
    />
    <div class="topic-form__row">
      <label>
        语言
        <select v-model="language">
          <option value="zh">中文</option>
          <option value="en">English</option>
        </select>
      </label>
      <label>
        子任务数
        <input v-model.number="maxTasks" type="number" min="3" max="8" />
      </label>
    </div>
    <input
      v-model="locationHint"
      class="topic-form__input"
      placeholder="位置锚点（可选），例如 Tokyo, Japan"
    />
    <button class="topic-form__submit" type="submit" :disabled="props.disabled">
      {{ props.disabled ? "运行中..." : "开始研究" }}
    </button>
  </form>
</template>

<style scoped>
.topic-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.topic-form__input,
.topic-form select,
.topic-form input[type="number"] {
  font: inherit;
  padding: 8px 10px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  width: 100%;
}
.topic-form__row {
  display: flex;
  gap: 8px;
}
.topic-form__row label {
  flex: 1;
  font-size: 0.85rem;
  color: #475569;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.topic-form__submit {
  background: #2563eb;
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 10px 14px;
  font-weight: 600;
}
.topic-form__submit:disabled {
  background: #94a3b8;
  cursor: not-allowed;
}
</style>
