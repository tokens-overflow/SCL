<script setup lang="ts">
import { marked } from "marked";
import { computed, ref } from "vue";
import { researchState } from "../stores/research";

const props = defineProps<{ markdown: string }>();

function slugify(s: string, i: number) {
  return "h-" + i + "-" + s.replace(/[^\w一-龥]+/g, "-").slice(0, 24);
}

interface Heading {
  level: number;
  text: string;
  id: string;
}

const headings = computed<Heading[]>(() => {
  if (!props.markdown) return [];
  const out: Heading[] = [];
  let i = 0;
  for (const line of props.markdown.split("\n")) {
    const m = /^(#{1,3})\s+(.*)$/.exec(line.trim());
    if (m) out.push({ level: m[1].length, text: m[2].trim(), id: slugify(m[2].trim(), i++) });
  }
  return out;
});

const html = computed(() => {
  if (!props.markdown) return "";
  let i = 0;
  const renderer = new marked.Renderer();
  renderer.heading = ({ tokens, depth }: any) => {
    const text = tokens.map((t: any) => t.raw).join("");
    const id = slugify(text, i++);
    return `<h${depth} id="${id}">${marked.parseInline(text)}</h${depth}>`;
  };
  return marked.parse(props.markdown, { gfm: true, breaks: true, renderer });
});

const copied = ref(false);
async function copyMd() {
  await navigator.clipboard.writeText(props.markdown);
  copied.value = true;
  setTimeout(() => (copied.value = false), 1600);
}

function downloadMd() {
  const blob = new Blob([props.markdown], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${researchState.topic || "report"}.md`;
  a.click();
  URL.revokeObjectURL(url);
}

function jump(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}
</script>

<template>
  <section class="report">
    <header class="rh">
      <p class="eyebrow">手记 · Report</p>
      <div class="actions" v-if="props.markdown">
        <button class="btn btn--ghost btn--sm" @click="copyMd">
          {{ copied ? "已抄录 ✓" : "抄录 Markdown" }}
        </button>
        <button class="btn btn--sm" @click="downloadMd">下载 .md</button>
      </div>
    </header>

    <div v-if="!props.markdown" class="empty">
      ○ 待诸般考据齐备，手记自成
    </div>

    <div v-else class="layout">
      <nav class="toc" v-if="headings.length">
        <p class="toc-t mono">目次</p>
        <a
          v-for="h in headings"
          :key="h.id"
          class="toc-l"
          :class="`lv${h.level}`"
          @click="jump(h.id)"
          >{{ h.text }}</a
        >
      </nav>
      <article class="prose ink-in" v-html="html" />
    </div>
  </section>
</template>

<style scoped>
.report {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.rh {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.actions {
  display: flex;
  gap: 8px;
}
.btn--sm {
  padding: 6px 11px;
  font-size: 0.76rem;
}
.layout {
  display: grid;
  grid-template-columns: 150px 1fr;
  gap: 22px;
  overflow: auto;
  min-height: 0;
}
.toc {
  position: sticky;
  top: 0;
  align-self: start;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-right: 12px;
  border-right: 1px solid var(--line);
}
.toc-t {
  font-size: 0.64rem;
  letter-spacing: 0.2em;
  color: var(--text-lo);
  text-transform: uppercase;
  margin: 0 0 4px;
}
.toc-l {
  font-size: 0.8rem;
  color: var(--text-mid);
  cursor: pointer;
  line-height: 1.4;
  padding: 2px 0;
  border-left: 2px solid transparent;
  padding-left: 8px;
  margin-left: -2px;
  transition: color 0.15s, border-color 0.15s;
}
.toc-l:hover {
  color: var(--accent);
  border-left-color: var(--accent);
  text-decoration: none;
}
.toc-l.lv2 { padding-left: 18px; font-size: 0.76rem; }
.toc-l.lv3 { padding-left: 28px; font-size: 0.74rem; color: var(--text-lo); }

/* rendered markdown — woodblock-print serif */
.prose {
  min-width: 0;
  font-family: var(--font-serif);
  font-size: 0.96rem;
  line-height: 1.9;
  color: var(--text-hi);
  padding-bottom: 30px;
}
.prose :deep(h1),
.prose :deep(h2),
.prose :deep(h3) {
  font-family: var(--font-brush);
  font-weight: 400;
  color: var(--text-hi);
  letter-spacing: 1px;
  scroll-margin-top: 8px;
}
.prose :deep(h1) {
  font-size: 1.7rem;
  margin: 0 0 0.6em;
  padding-bottom: 0.3em;
  border-bottom: 2px solid var(--line-strong);
}
.prose :deep(h2) {
  font-size: 1.32rem;
  margin: 1.5em 0 0.5em;
  padding-left: 12px;
  border-left: 3px solid var(--accent);
}
.prose :deep(h3) {
  font-size: 1.1rem;
  margin: 1.2em 0 0.4em;
  color: var(--accent);
}
.prose :deep(p) { margin: 0.7em 0; }
.prose :deep(a) { color: var(--accent); border-bottom: 1px solid var(--accent-dim); }
.prose :deep(ul),
.prose :deep(ol) { padding-left: 1.4em; margin: 0.6em 0; }
.prose :deep(li) { margin: 0.3em 0; }
.prose :deep(li::marker) { color: var(--accent); }
.prose :deep(strong) { color: var(--text-hi); font-weight: 700; }
.prose :deep(blockquote) {
  margin: 1em 0;
  padding: 0.4em 1em;
  border-left: 3px solid var(--line-strong);
  background: var(--bg-2);
  color: var(--text-mid);
  border-radius: 0 var(--radius-xs) var(--radius-xs) 0;
}
.prose :deep(code) {
  font-family: var(--font-mono);
  font-size: 0.84em;
  background: var(--bg-2);
  padding: 1px 5px;
  border-radius: 4px;
  border: 1px solid var(--line);
}
.prose :deep(pre) {
  background: var(--bg-2);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  overflow: auto;
}
.prose :deep(pre code) { background: none; border: none; padding: 0; }
.prose :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 1em 0;
  font-size: 0.88rem;
}
.prose :deep(th),
.prose :deep(td) {
  border: 1px solid var(--line);
  padding: 7px 11px;
  text-align: left;
}
.prose :deep(th) {
  background: var(--bg-2);
  font-weight: 700;
  color: var(--text-hi);
}
.prose :deep(tr:nth-child(even) td) { background: rgba(255, 253, 247, 0.4); }
.prose :deep(hr) {
  border: none;
  border-top: 1px solid var(--line-strong);
  margin: 1.6em 0;
}
.prose :deep(img) { max-width: 100%; border-radius: var(--radius-sm); }

@media (max-width: 720px) {
  .layout { grid-template-columns: 1fr; }
  .toc { display: none; }
}
</style>
