/* ============================================================
 * Q-CC 前端 · chat.js
 * 职责：会话气泡渲染 + 流式事件处理(handleEvent) + 工具调用/权限确认卡片。
 * 说明：本文件是原 app.js 的一段(按功能拆分，逻辑未改)。
 *       经典 <script> 顶层声明共享同一全局作用域，靠 index.html
 *       的引入顺序保证依赖可见——请勿单独调整加载顺序。
 * ============================================================ */
/* ================= 会话视图 ================= */
function newView() {
  return { curEl: null, curMsgId: null, streamBuf: '', toolRows: {}, hadFirst: false };
}
function msgContainer() { return $('#messages'); }
function scrollBottom() { const m = msgContainer(); m.scrollTop = m.scrollHeight; }

function addUserMsg(text, ts) {
  const div = document.createElement('div');
  div.className = 'msg user';
  div.innerHTML = `<div class="m-head"><span class="mh-ava">${avatarHtml(ME.avatar)}</span>${esc(ME.name)} ${ts ? fmtTime(ts) : ''}</div>` +
    `<div class="m-body">${mdInline(text)}</div>`;
  msgContainer().appendChild(div); scrollBottom();
}
/* 同一条 assistant 消息(相同 message.id)的多个内容块会分多个事件到达,
   合并进同一个气泡:blocks 放定稿内容,stream 放打字中的增量 */
function ensureBubble(v, msgId) {
  if (v.curEl && (v.curMsgId === null || msgId == null || v.curMsgId === msgId)) {
    if (msgId != null) v.curMsgId = msgId;
    return v.curEl;
  }
  const div = document.createElement('div');
  div.className = 'msg agent';
  div.innerHTML = `<div class="m-head"><span class="mh-ava">${avatarHtml(currentAgent.avatar)}</span>${esc(currentAgent.name)} ${fmtTime(Date.now()/1000)}</div>` +
    `<div class="m-body"><div class="blocks"></div><div class="stream"></div></div>`;
  msgContainer().appendChild(div);
  v.curEl = div; v.curMsgId = msgId || null; v.streamBuf = '';
  scrollBottom();
  return div;
}
function closeBubble(v) { v.curEl = null; v.curMsgId = null; v.streamBuf = ''; }
function appendAssistant(v, message) {
  const el = ensureBubble(v, message.id);
  const blocks = el.querySelector('.blocks');
  let html = '';
  for (const blk of message.content || []) {
    if (blk.type === 'text') html += renderMd(blk.text);
    else if (blk.type === 'thinking') html += thinkRow(blk.thinking || '');
    else if (blk.type === 'tool_use') html += toolRowHtml(blk);
  }
  blocks.insertAdjacentHTML('beforeend', html);
  blocks.querySelectorAll('.tool-row').forEach(r => { v.toolRows[r.dataset.tuid] = r; });
  el.querySelector('.stream').innerHTML = '';
  v.streamBuf = '';
  scrollBottom();
}
function thinkRow(text) {
  return `<div class="think-row"><span class="th-hd">💭 思考…（点击展开）</span><div class="th-bd">${esc(text)}</div></div>`;
}
const TOOL_ICONS = { Bash:'🖥️', Read:'📄', Write:'📝', Edit:'✏️', Grep:'🔍', Glob:'🗂️', WebFetch:'🌐', WebSearch:'🔎', Task:'🤝', TodoWrite:'🗒️' };
function toolSummary(blk) {
  const i = blk.input || {};
  return i.command || i.file_path || i.pattern || i.url || i.description || i.prompt || '';
}
function toolRowHtml(blk) {
  const ico = TOOL_ICONS[blk.name] || '🔧';
  return `<div class="tool-row" data-tuid="${esc(blk.id||'')}"><div class="tr-hd">` +
    `<span>${ico}</span><span class="tr-name">${esc(blk.name)}</span>` +
    `<span class="tr-arg">${esc(toolSummary(blk)).slice(0,120)}</span>` +
    `<span class="tr-state">运行中…</span></div>` +
    `<div class="tr-out"><pre></pre></div></div>`;
}
function attachToolResult(v, blk) {
  const row = v.toolRows[blk.tool_use_id];
  if (!row) return;
  let out = '';
  const c = blk.content;
  if (typeof c === 'string') out = c;
  else if (Array.isArray(c)) out = c.map(x => x.text || '').join('\n');
  row.querySelector('.tr-out pre').textContent = out.slice(0, 8000) || '(无输出)';
  row.querySelector('.tr-state').textContent = blk.is_error ? '出错 ❌' : '完成 ✔';
}
function addSysLine(text) {
  const d = document.createElement('div');
  d.className = 'sys-line'; d.textContent = text;
  msgContainer().appendChild(d); scrollBottom();
}
function addResultLine(ev) {
  const d = document.createElement('div');
  d.className = 'result-line' + (ev.is_error ? ' err' : '');
  const secs = ev.duration_ms ? (ev.duration_ms/1000).toFixed(1) + 's' : '?';
  const cost = ev.total_cost_usd != null ? '$' + ev.total_cost_usd.toFixed(4) : '';
  d.innerHTML = `<span>${ev.is_error ? '❌ 出错' : '✔ 本轮完成'}</span><span>⏱ ${secs}</span>` +
    (cost ? `<span>💰 ${cost}</span>` : '') +
    (ev.num_turns ? `<span>${ev.num_turns} turns</span>` : '') +
    `<span class="msg-actions" style="margin-left:auto">` +
      `<span class="react" data-react="like" title="赞">👍 赞</span>` +
      `<span class="react" data-react="dislike" title="踩：让TA换个思路重答">👎 踩</span>` +
      `<span class="react" data-react="share" title="复制这条回答">↗ 分享</span>` +
    `</span>`;
  msgContainer().appendChild(d); scrollBottom();
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
}

/* ================= 事件处理 ================= */
function handleEvent(ev) {
  const v = view;
  switch (ev.type) {
    case 'x-user':
      closeBubble(v);
      addUserMsg(ev.text, ev.ts); break;
    case 'system':
      if (ev.subtype === 'init') {
        syncChatModel(ev.model);
        $('#chat-meta').textContent = `会话 ${(ev.session_id||'').slice(0,8)}`;
        if (!v.hadFirst) { addSysLine(`—— Claude Code 已上线（${ev.model || ''}）——`); v.hadFirst = true; }
      }
      break;
    case 'stream_event': {
      const e = ev.event || {};
      if (e.type === 'message_start' && e.message && e.message.id)
        ensureBubble(v, e.message.id);
      else if (e.type === 'content_block_delta' && e.delta && e.delta.type === 'text_delta') {
        const el = ensureBubble(v, null);
        v.streamBuf += e.delta.text;
        el.querySelector('.stream').innerHTML = renderMd(v.streamBuf) + '<span class="caret"></span>';
        scrollBottom();
      }
      break;
    }
    case 'assistant':
      if (ev.message) appendAssistant(v, ev.message);
      break;
    case 'user': {
      const content = (ev.message && ev.message.content) || [];
      if (Array.isArray(content))
        for (const blk of content)
          if (blk.type === 'tool_result') attachToolResult(v, blk);
      break;
    }
    case 'result':
      closeBubble(v);
      addResultLine(ev);
      setTaskStatus(currentTaskId, ev.is_error ? 'error' : 'idle');
      break;
    case 'x-sys':
      addSysLine('📎 ' + ev.text);
      break;
    case 'x-stderr':
      addSysLine('⚠ ' + ev.text.split('\n').slice(-3).join(' / ').slice(0, 300));
      break;
    case 'x-proc-exit':
      if (ev.code) { addSysLine(`进程退出，代码 ${ev.code}`); setTaskStatus(currentTaskId, 'error'); }
      break;
    case 'x-perm-req':
      addPermCard(ev);
      break;
    case 'x-perm-resolved':
      resolvePermCard(ev.request_id, ev.behavior);
      break;
  }
}

/* ================= 权限确认卡片（Claude 想执行某工具时弹出） ================= */
const PERM_ICON = { Bash:'💻', PowerShell:'💻', Write:'📝', Edit:'📝', NotebookEdit:'📝', Read:'📖' };
function addPermCard(ev) {
  if (msgContainer().querySelector(`.perm-card[data-req="${ev.request_id}"]`)) return;  // 重连去重
  const d = document.createElement('div');
  d.className = 'perm-card'; d.dataset.req = ev.request_id;
  const ico = PERM_ICON[ev.tool_name] || '🔧';
  d.innerHTML =
    `<div class="pc-hd">${ico} Claude 想执行 <b>${esc(ev.tool_name)}</b>，是否允许？</div>` +
    (ev.preview ? `<pre class="pc-body">${esc(ev.preview)}</pre>` : '') +
    `<div class="pc-btns">` +
      `<button class="pc-btn ok" data-permdecide="allow" data-scope="once">✅ 允许一次</button>` +
      `<button class="pc-btn ok" data-permdecide="allow" data-scope="session">✅ 本会话都允许</button>` +
      `<button class="pc-btn no" data-permdecide="deny" data-scope="once">❌ 拒绝</button>` +
    `</div>`;
  msgContainer().appendChild(d); scrollBottom();
}
function resolvePermCard(reqId, behavior) {
  const d = msgContainer().querySelector(`.perm-card[data-req="${reqId}"]`);
  if (!d) return;
  d.classList.add('decided');
  const b = d.querySelector('.pc-btns');
  if (b) b.innerHTML = `<span class="pc-done">${behavior === 'allow' ? '✅ 已允许' : '❌ 已拒绝'}</span>`;
}

