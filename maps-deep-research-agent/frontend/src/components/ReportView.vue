<script setup lang="ts">
import { marked } from "marked";
import { computed } from "vue";

const props = defineProps<{ markdown: string }>();

const html = computed(() => {
  if (!props.markdown) return "";
  return marked.parse(props.markdown, { gfm: true, breaks: true });
});
</script>

<template>
  <section>
    <h2 class="panel-title">最终报告</h2>
    <article v-if="html" class="report-content" v-html="html" />
    <div v-else class="report-empty">报告会在所有子任务完成后生成。</div>
  </section>
</template>

<style scoped>
.report-empty {
  color: #94a3b8;
  font-size: 0.9rem;
}
</style>
