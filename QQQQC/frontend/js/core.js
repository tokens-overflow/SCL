/* ============================================================
 * Q-CC 前端 · core.js
 * 职责：全局状态 + 基础工具(\$ / esc) + 轻量 Markdown 渲染。最先加载：定义所有页面共享的可变状态与工具函数。
 * 说明：本文件是原 app.js 的一段(按功能拆分，逻辑未改)。
 *       经典 <script> 顶层声明共享同一全局作用域，靠 index.html
 *       的引入顺序保证依赖可见——请勿单独调整加载顺序。
 * ============================================================ */
/* ================= 状态 ================= */
const $ = s => document.querySelector(s);
let CONFIG = { projects: [], models: ["sonnet"], default_model: "sonnet", user_name: "我" };
let TASKS = [];
let SLASH = [];                // 斜杠命令列表 [{name, desc}]
let FRIEND_LIST = [];          // 我的好友 [{id,name,avatar,sign,persona,project,model}]
let ME = { name: '我', avatar: 'qq1' };          // 我自己的资料
let currentAgent = { name: 'Claude 小蓝', avatar: 'qq1' };  // 当前对话的对方
let currentTaskId = null;
let es = null;                 // 当前 EventSource
let renderedCount = 0;         // 已渲染事件数（SSE 重连回放时跳过）
let view = null;               // 当前会话渲染状态
const UNREAD = new Set();      // 已回复完成但还没点开看的会话 id（列表图标晃动提醒）

const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

/* ================= 轻量 Markdown ================= */
function renderMd(text) {
  const parts = String(text).split(/```(\w*)\n?([\s\S]*?)```/g);
  let html = '';
  for (let i = 0; i < parts.length; i += 3) {
    html += mdInline(parts[i]);
    if (i + 2 < parts.length) html += codeBlock(parts[i+1] || 'code', parts[i+2]);
  }
  return html;
}
function codeBlock(lang, code) {
  return `<div class="codeblock"><div class="cb-hd"><span>${esc(lang)}</span>` +
    `<span class="cb-copy" data-code="${esc(code)}">📋 复制</span></div>` +
    `<pre>${esc(code.replace(/\n$/,''))}</pre></div>`;
}
/* GFM 表格支持：|a|b| + |---|---| + 数据行 → <table>。之前不支持，表格会以生 | 竖线乱掉 */
function mdSplitRow(s) {
  return s.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
}
function mdIsSep(s) {
  const cells = mdSplitRow(s);
  return cells.length >= 1 && cells.every(c => /^:?-{1,}:?$/.test(c));
}
function mdTable(header, rows) {
  const th = header.map(c => `<th>${inlineFmt(c)}</th>`).join('');
  const body = rows.map(r => `<tr>${header.map((_, i) => `<td>${inlineFmt(r[i] || '')}</td>`).join('')}</tr>`).join('');
  return `<table class="md-table"><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table>`;
}
function mdInline(text) {
  const lines = String(text).split('\n');
  let html = '', inList = false, i = 0;
  const closeList = () => { if (inList) { html += '</ul>'; inList = false; } };
  while (i < lines.length) {
    const ln = lines[i];
    // 表格：当前行有 | 且下一行是分隔行(|---|---|)
    if (ln.includes('|') && i + 1 < lines.length && mdIsSep(lines[i + 1])) {
      closeList();
      const header = mdSplitRow(ln);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { rows.push(mdSplitRow(lines[i])); i++; }
      html += mdTable(header, rows);
      continue;
    }
    const m = ln.match(/^\s*[-*•]\s+(.*)/);
    if (m) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += `<li>${inlineFmt(m[1])}</li>`;
      i++; continue;
    }
    closeList();
    if (ln.trim()) html += `<p>${inlineFmt(ln)}</p>`;
    i++;
  }
  closeList();
  return html;
}
function inlineFmt(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/`([^`]+?)`/g, '<code>$1</code>')
    .replace(/^#{1,4}\s+(.*)$/, '<b>$1</b>');
}

/* ================= 消息提示音 ================= */
/* QQ 风格“滴滴滴”：用 Web Audio 现场合成三声短促蜂鸣，无需任何音频文件。
   在别的会话回复完成、图标开始晃动时播放（见 prefs.js 的轮询）。
   AudioContext 首次需用户手势解锁；本应用点过好友/按钮后即可正常发声。 */
let _audioCtx = null;
function notifyDing() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    _audioCtx = _audioCtx || new Ctx();
    const ctx = _audioCtx;
    if (ctx.state === 'suspended') ctx.resume();
    const t0 = ctx.currentTime;
    for (let i = 0; i < 3; i++) {          // 三声“滴·滴·滴”
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      const start = t0 + i * 0.15;
      osc.type = 'square';
      osc.frequency.value = 784;           // 约 G5，清脆的提示音
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.16, start + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.11);
      osc.connect(gain); gain.connect(ctx.destination);
      osc.start(start); osc.stop(start + 0.13);
    }
  } catch (_) { /* 环境禁用音频时静默忽略 */ }
}

