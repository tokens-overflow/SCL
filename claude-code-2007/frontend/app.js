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
function mdInline(text) {
  const lines = String(text).split('\n');
  let html = '', inList = false;
  for (const ln of lines) {
    const m = ln.match(/^\s*[-*•]\s+(.*)/);
    if (m) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += `<li>${inlineFmt(m[1])}</li>`;
    } else {
      if (inList) { html += '</ul>'; inList = false; }
      if (ln.trim()) html += `<p>${inlineFmt(ln)}</p>`;
    }
  }
  if (inList) html += '</ul>';
  return html;
}
function inlineFmt(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/`([^`]+?)`/g, '<code>$1</code>')
    .replace(/^#{1,4}\s+(.*)$/, '<b>$1</b>');
}

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
        $('#chat-meta').textContent = `模型 ${ev.model || ''} · 会话 ${(ev.session_id||'').slice(0,8)}`;
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
  }
}

/* ================= 任务列表 / SSE ================= */
async function loadConfig() {
  CONFIG = await (await fetch('/api/config')).json();
  const sel = $('#f-project'); sel.innerHTML = '';
  for (const p of CONFIG.projects) {
    const o = document.createElement('option');
    o.value = p.name; o.textContent = `${p.name} — ${p.path}`;
    if (!p.exists) o.disabled = true;
    sel.appendChild(o);
  }
  const ms = $('#f-model'); ms.innerHTML = '';
  for (const m of CONFIG.models || ['sonnet']) {
    const o = document.createElement('option');
    o.value = m; o.textContent = m;
    if (m === CONFIG.default_model) o.selected = true;
    ms.appendChild(o);
  }
  // 定时任务弹窗 / CLAUDE.md 视图 / 添加好友弹窗共用同一批项目/模型
  fillProjectSelect($('#s-project'));
  fillProjectSelect($('#md-project'));
  fillProjectSelect($('#fr-project'));
  fillModelSelect($('#s-model'));
  fillModelSelect($('#fr-model'));
}
/* 经典 QQ 风格的原创卡通头像（自绘 SVG，可看图选择） */
const QQ_AVATARS = [
  { id: 'qq1', svg: '<svg viewBox="0 0 40 40"><rect width="40" height="40" rx="9" fill="#5b9be8"/><circle cx="20" cy="23" r="10.5" fill="#ffd2a6"/><path d="M9.5 20a10.5 10.5 0 0 1 21 0c-3.5-3-6-4.5-10.5-4.5S13 17 9.5 20z" fill="#6b4326"/><circle cx="16.5" cy="23" r="1.5" fill="#3a2a20"/><circle cx="23.5" cy="23" r="1.5" fill="#3a2a20"/><path d="M17.5 28q2.5 2 5 0" stroke="#c47a4e" stroke-width="1.4" fill="none" stroke-linecap="round"/></svg>' },
  { id: 'qq2', svg: '<svg viewBox="0 0 40 40"><rect width="40" height="40" rx="9" fill="#f48fb1"/><circle cx="10" cy="21" r="4" fill="#8a5a2c"/><circle cx="30" cy="21" r="4" fill="#8a5a2c"/><circle cx="20" cy="23" r="10.5" fill="#ffd2a6"/><path d="M9.5 21a10.5 10.5 0 0 1 21 0c-3-3.5-6-5-10.5-5S12.5 17.5 9.5 21z" fill="#8a5a2c"/><circle cx="16.5" cy="23" r="1.5" fill="#3a2a20"/><circle cx="23.5" cy="23" r="1.5" fill="#3a2a20"/><circle cx="13.5" cy="26.5" r="1.5" fill="#ff9db0"/><circle cx="26.5" cy="26.5" r="1.5" fill="#ff9db0"/><path d="M17.5 28q2.5 2 5 0" stroke="#c47a4e" stroke-width="1.4" fill="none" stroke-linecap="round"/></svg>' },
  { id: 'qq3', svg: '<svg viewBox="0 0 40 40"><rect width="40" height="40" rx="9" fill="#4db6ac"/><circle cx="20" cy="23" r="10.5" fill="#ffd2a6"/><path d="M9.5 19a10.5 10.5 0 0 1 21 0c-3.5-3-6-4-10.5-4S13 16 9.5 19z" fill="#2f2a26"/><circle cx="15.5" cy="23" r="3.3" fill="#d6f2ff" stroke="#2f2a26" stroke-width="1.3"/><circle cx="24.5" cy="23" r="3.3" fill="#d6f2ff" stroke="#2f2a26" stroke-width="1.3"/><path d="M18.8 23h2.4M12.2 22l-2.2-1M27.8 22l2.2-1" stroke="#2f2a26" stroke-width="1.3" stroke-linecap="round"/><path d="M17.5 29q2.5 1.6 5 0" stroke="#c47a4e" stroke-width="1.3" fill="none" stroke-linecap="round"/></svg>' },
  { id: 'qq4', svg: '<svg viewBox="0 0 40 40"><rect width="40" height="40" rx="9" fill="#ff9d5c"/><circle cx="20" cy="24" r="10" fill="#ffd2a6"/><path d="M8 20a12 6 0 0 1 24 0z" fill="#d84343"/><rect x="8" y="19" width="18" height="2.6" rx="1.3" fill="#b23434"/><path d="M8 20q-3.5 .3-4.5 2 3.5 .5 6 0z" fill="#b23434"/><circle cx="16.5" cy="25" r="1.5" fill="#3a2a20"/><circle cx="23.5" cy="25" r="1.5" fill="#3a2a20"/><path d="M17.5 29q2.5 2 5 0" stroke="#c47a4e" stroke-width="1.4" fill="none" stroke-linecap="round"/></svg>' },
  { id: 'qq5', svg: '<svg viewBox="0 0 40 40"><rect width="40" height="40" rx="9" fill="#5c6bc0"/><circle cx="20" cy="23" r="10.5" fill="#ffd2a6"/><path d="M9.5 19a10.5 10.5 0 0 1 21 0c-3.5-3-6-4-10.5-4S13 16 9.5 19z" fill="#2f2a26"/><path d="M11.5 21.5h7v2.8a3.5 3.5 0 0 1-7 0zM21.5 21.5h7v2.8a3.5 3.5 0 0 1-7 0zM18.5 22.4h3" fill="#111" stroke="#111" stroke-width="1"/><path d="M16 29q4 1.6 8-.5" stroke="#c47a4e" stroke-width="1.4" fill="none" stroke-linecap="round"/></svg>' },
  { id: 'qq6', svg: '<svg viewBox="0 0 40 40"><rect width="40" height="40" rx="9" fill="#90caf9"/><ellipse cx="20" cy="9" rx="6.5" ry="2.2" fill="none" stroke="#ffe082" stroke-width="1.7"/><circle cx="20" cy="24" r="10.5" fill="#ffe0c0"/><path d="M10 21a10 10 0 0 1 20 0c-3-3-6-4.5-10-4.5S13 18 10 21z" fill="#f4c56b"/><circle cx="16.5" cy="24" r="1.5" fill="#3a2a20"/><circle cx="23.5" cy="24" r="1.5" fill="#3a2a20"/><circle cx="13.5" cy="27" r="1.6" fill="#ffb3ba"/><circle cx="26.5" cy="27" r="1.6" fill="#ffb3ba"/><path d="M17.5 29q2.5 1.6 5 0" stroke="#c47a4e" stroke-width="1.3" fill="none" stroke-linecap="round"/></svg>' },
  { id: 'qq7', svg: '<svg viewBox="0 0 40 40"><rect width="40" height="40" rx="9" fill="#ef5350"/><path d="M12 15c-1-3-3-4-4-5 0 3 1 5 3 6zM28 15c1-3 3-4 4-5 0 3-1 5-3 6z" fill="#7a1f1f"/><circle cx="20" cy="24" r="10.5" fill="#ffc1a0"/><path d="M10 20a10 10 0 0 1 20 0c-3-3-6-4-10-4s-7 1-10 4z" fill="#5a2a2a"/><path d="M15 23l3 1M25 23l-3 1" stroke="#3a2a20" stroke-width="1.5" stroke-linecap="round"/><path d="M15.5 28q4.5 3 9 0" stroke="#8a3b3b" stroke-width="1.5" fill="none" stroke-linecap="round"/></svg>' },
  { id: 'qq8', svg: '<svg viewBox="0 0 40 40"><rect width="40" height="40" rx="9" fill="#ab6fd6"/><path d="M12 16l-2-6 7 3zM28 16l2-6-7 3z" fill="#ffd2a6"/><circle cx="20" cy="24" r="10" fill="#ffd2a6"/><circle cx="16.5" cy="24" r="1.5" fill="#3a2a20"/><circle cx="23.5" cy="24" r="1.5" fill="#3a2a20"/><path d="M20 26.6l-1.2 1.4M20 26.6l1.2 1.4" stroke="#c47a4e" stroke-width="1.2" stroke-linecap="round" fill="none"/><path d="M9 24h5M9 26h5M26 24h5M26 26h5" stroke="#d8b48a" stroke-width=".8"/></svg>' },
  { id: 'qq9', svg: '<svg viewBox="0 0 40 40"><rect width="40" height="40" rx="9" fill="#8d6e63"/><circle cx="20" cy="23" r="10.5" fill="#ffd2a6"/><path d="M10 19a10 10 0 0 1 20 0c-3-2.5-6-3.5-10-3.5S13 16.5 10 19z" fill="#59524e"/><circle cx="16.5" cy="23" r="1.5" fill="#3a2a20"/><circle cx="23.5" cy="23" r="1.5" fill="#3a2a20"/><path d="M14 28q3-2 6 0 3-2 6 0" stroke="#59524e" stroke-width="1.9" fill="none" stroke-linecap="round"/></svg>' },
  { id: 'qq10', svg: '<svg viewBox="0 0 40 40"><rect width="40" height="40" rx="9" fill="#ffd54f"/><circle cx="20" cy="23" r="11" fill="#ffe0c0"/><path d="M20 12c2-1 4 1 2 2.5" stroke="#c98a3a" stroke-width="1.6" fill="none" stroke-linecap="round"/><circle cx="16.5" cy="23" r="1.6" fill="#3a2a20"/><circle cx="23.5" cy="23" r="1.6" fill="#3a2a20"/><circle cx="13.5" cy="26.5" r="1.8" fill="#ffb3ba"/><circle cx="26.5" cy="26.5" r="1.8" fill="#ffb3ba"/><circle cx="20" cy="28.5" r="1.6" fill="#e0708a"/></svg>' },
  { id: 'qq11', svg: '<svg viewBox="0 0 40 40"><rect width="40" height="40" rx="9" fill="#26c6da"/><ellipse cx="20" cy="24" rx="9" ry="11" fill="#2a2f3a"/><ellipse cx="20" cy="26" rx="6" ry="8" fill="#fff"/><circle cx="17" cy="19" r="1.4" fill="#222"/><circle cx="23" cy="19" r="1.4" fill="#222"/><path d="M18.3 21.5h3.4l-1.7 2.2z" fill="#ff9d1e"/></svg>' },
  { id: 'qq12', svg: '<svg viewBox="0 0 40 40"><rect width="40" height="40" rx="9" fill="#90a4ae"/><rect x="10" y="15" width="20" height="17" rx="4" fill="#e6ebee"/><rect x="19" y="10" width="2" height="4" fill="#546670"/><circle cx="20" cy="9.3" r="1.7" fill="#ffce54"/><circle cx="16" cy="22" r="2.3" fill="#3aa0ff"/><circle cx="24" cy="22" r="2.3" fill="#3aa0ff"/><rect x="15" y="27" width="10" height="2.2" rx="1.1" fill="#546670"/></svg>' },
];
function avatarSvg(id) { const a = QQ_AVATARS.find(x => x.id === id); return a ? a.svg : null; }
function avatarHtml(av) {
  if (av && av.startsWith('img:'))
    return `<img class="av-img" src="/frontend/avatars/${encodeURIComponent(av.slice(4))}" alt="">`;
  const s = avatarSvg(av);
  return s || `<span class="emo">${esc(av || '🙂')}</span>`;
}
let IMG_AVATARS = [];   // 头像图片文件名（放在 frontend/avatars/ 下）
async function loadAvatars() {
  try { IMG_AVATARS = await (await fetch('/api/avatars')).json(); } catch (e) { IMG_AVATARS = []; }
}

async function loadFriends() {
  FRIEND_LIST = await (await fetch('/api/friends')).json();
  renderFriends();
}
function renderFriends() {
  const box = $('#friends');
  $('#friends-hd').textContent = `我的好友 (${FRIEND_LIST.length})`;
  box.innerHTML = FRIEND_LIST.map((f, i) =>
    `<div class="friend" data-friend="${i}">
       <div class="f-ava" data-editfriend="${esc(f.id)}">${avatarHtml(f.avatar)}</div>
       <div style="flex:1;min-width:0"><div class="f-name">${esc(f.name)}</div>
       <div class="f-sign">[在线] ${esc(f.sign || '')}</div></div>
       <span class="f-del" data-delfriend="${esc(f.id)}" title="删除好友">✕</span>
     </div>`).join('');
}
/* 悬停好友头像/行 —— 显示这个好友主要帮你干啥 */
function friendTipEl() {
  let t = document.getElementById('friend-tip');
  if (!t) { t = document.createElement('div'); t.id = 'friend-tip'; document.body.appendChild(t); }
  return t;
}
function showFriendTip(row) {
  const f = FRIEND_LIST[+row.dataset.friend];
  if (!f) return;
  const role = (f.persona || f.sign || '').trim() || '还没设置人设';
  const t = friendTipEl();
  t.innerHTML = `<div class="ft-name">${esc(f.name)}</div>` +
    `<div class="ft-role">${esc(role)}</div>` +
    `<div class="ft-hint">点头像编辑资料 · 点名字开聊</div>`;
  t.classList.add('show');
  const r = row.getBoundingClientRect();
  const place = () => {
    let left = r.left - t.offsetWidth - 10;      // 好友在右栏，气泡放左边
    if (left < 6) left = r.right + 10;
    let top = Math.max(6, Math.min(r.top, window.innerHeight - t.offsetHeight - 8));
    t.style.left = left + 'px'; t.style.top = top + 'px';
  };
  place(); requestAnimationFrame(place);
}
$('#friends').addEventListener('mouseover', e => {
  const row = e.target.closest('.friend'); if (row) showFriendTip(row);
});
$('#friends').addEventListener('mouseout', e => {
  const to = e.relatedTarget;
  if (!to || !(to.closest && to.closest('.friend'))) {
    const t = document.getElementById('friend-tip'); if (t) t.classList.remove('show');
  }
});
async function startFriendChat(f) {
  if (!f) return;
  const proj = f.project || (CONFIG.projects[0] || {}).name || '';
  const prompt = `${f.persona}\n\n请用上面的人设，跟我打个招呼、简短开场（别超过三句）。之后一直保持这个人设跟我聊。`;
  const res = await fetch('/api/tasks', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ title: `💬 ${f.name}`, project: proj, model: f.model || CONFIG.default_model, permission_mode: CONFIG.default_permission_mode, prompt,
      agent_name: f.name, agent_avatar: f.avatar }) });
  const task = await res.json();
  if (task.error) { alert(task.error); return; }
  TASKS.unshift(task);
  openTask(task.id);
}
async function deleteFriend(id) {
  if (!confirm('删除这个好友？（不影响已有的聊天信息）')) return;
  await fetch(`/api/friends/${id}/delete`, { method: 'POST' });
  loadFriends();
}
/* 添加好友弹窗 */
/* 头像选择器（好友弹窗 & 我的资料弹窗共用）：看图选择，或输入 emoji */
function buildAvatarGrid(grid, selectedId) {
  const first = IMG_AVATARS.length ? 'img:' + IMG_AVATARS[0] : (QQ_AVATARS[0] && QQ_AVATARS[0].id);
  grid.dataset.sel = selectedId || first;
  const imgCells = IMG_AVATARS.map(fn => {
    const id = 'img:' + fn;
    return `<div class="av-cell${id === grid.dataset.sel ? ' sel' : ''}" data-avpick="${esc(id)}"><img class="av-img" src="/frontend/avatars/${encodeURIComponent(fn)}" alt=""></div>`;
  }).join('');
  // 只用生成的头像；万一头像文件夹为空才退回内置 SVG，避免选择器空白
  const svgCells = IMG_AVATARS.length ? '' : QQ_AVATARS.map(a =>
    `<div class="av-cell${a.id === grid.dataset.sel ? ' sel' : ''}" data-avpick="${a.id}">${a.svg}</div>`).join('');
  grid.innerHTML = imgCells + svgCells;
}
function pickedAvatar(grid, emojiInput) {
  const emo = emojiInput.value.trim();
  return emo || grid.dataset.sel || (IMG_AVATARS.length ? 'img:' + IMG_AVATARS[0] : (QQ_AVATARS[0] && QQ_AVATARS[0].id));
}
document.addEventListener('click', e => {
  const cell = e.target.closest('.av-cell[data-avpick]');
  if (!cell) return;
  const grid = cell.parentElement;
  grid.dataset.sel = cell.dataset.avpick;
  [...grid.children].forEach(c => c.classList.toggle('sel', c === cell));
  const emo = grid.closest('.modal') && grid.closest('.modal').querySelector('.av-emoji');
  if (emo) emo.value = '';
  if (grid.id === 'fr-avatar-grid') renderBigAva(cell.dataset.avpick);   // 同步大头像预览
});

let editingFriendId = null;
function renderBigAva(av) { $('#fr-bigava').innerHTML = avatarHtml(av); }
function openFriendModal(friend) {
  editingFriendId = friend ? friend.id : null;
  const selAv = friend && (friend.avatar || '').startsWith('img:') ? friend.avatar : undefined;
  buildAvatarGrid($('#fr-avatar-grid'), selAv);
  $('#fr-name').value = friend ? (friend.name || '') : '';
  $('#fr-sign').value = friend ? (friend.sign || '') : '';
  $('#fr-persona').value = friend ? (friend.persona || '') : '';
  $('#fr-avatar').value = (friend && !avatarSvg(friend.avatar) && !(friend.avatar || '').startsWith('img:')) ? (friend.avatar || '') : '';
  if (friend && friend.project) $('#fr-project').value = friend.project;
  if (friend && friend.model) $('#fr-model').value = friend.model;
  renderBigAva(friend ? friend.avatar : $('#fr-avatar-grid').dataset.sel);
  $('#fr-mtitle').textContent = friend ? '👤 好友资料 / 编辑' : '➕ 添加好友';
  $('#fr-ok').textContent = friend ? '保存' : '添加';
  $('#fr-del').style.display = friend ? '' : 'none';
  $('#friend-mask').classList.add('show');
  $('#fr-name').focus();
}
function closeFriendModal() { $('#friend-mask').classList.remove('show'); editingFriendId = null; }
async function createFriend() {
  const name = $('#fr-name').value.trim();
  if (!name) { $('#fr-name').focus(); return; }
  const body = {
    name, avatar: pickedAvatar($('#fr-avatar-grid'), $('#fr-avatar')), sign: $('#fr-sign').value.trim(),
    persona: $('#fr-persona').value.trim(), project: $('#fr-project').value, model: $('#fr-model').value,
  };
  const url = editingFriendId ? `/api/friends/${editingFriendId}/update` : '/api/friends';
  const res = await fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  const f = await res.json();
  if (f.error) { alert(f.error); return; }
  closeFriendModal();
  ['#fr-name', '#fr-avatar', '#fr-sign', '#fr-persona'].forEach(s => $(s).value = '');
  loadFriends();
}
async function deleteEditingFriend() {
  if (!editingFriendId) return;
  if (!confirm('删除这个好友？（不影响已有的聊天信息）')) return;
  await fetch(`/api/friends/${editingFriendId}/delete`, { method: 'POST' });
  closeFriendModal();
  loadFriends();
}

/* 我的资料 */
async function loadProfile() {
  try { ME = await (await fetch('/api/profile')).json(); } catch (e) {}
  renderMe();
}
function renderMe() {
  $('#uname').textContent = ME.name || '我';
  $('#me-avatar').innerHTML = avatarHtml(ME.avatar);
}
function openProfileModal() {
  $('#pf-name').value = ME.name || '';
  const isImg = !!(ME.avatar && ME.avatar.startsWith('img:'));
  const isSvg = !!avatarSvg(ME.avatar);
  $('#pf-avatar').value = (isImg || isSvg) ? '' : (ME.avatar || '');  // 只有纯 emoji 才回填自定义框
  buildAvatarGrid($('#pf-avatar-grid'), isImg ? ME.avatar : undefined);  // img 头像选中它，否则默认第一个
  $('#profile-mask').classList.add('show');
  $('#pf-name').focus();
}
function closeProfileModal() { $('#profile-mask').classList.remove('show'); }
async function saveProfile() {
  const body = { name: $('#pf-name').value.trim(), avatar: pickedAvatar($('#pf-avatar-grid'), $('#pf-avatar')) };
  const res = await fetch('/api/profile', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  ME = await res.json();
  renderMe();
  closeProfileModal();
}
function fillProjectSelect(sel) {
  sel.innerHTML = '';
  for (const p of CONFIG.projects) {
    const o = document.createElement('option');
    o.value = p.name; o.textContent = `${p.name} — ${p.path}`;
    if (!p.exists) o.disabled = true;
    sel.appendChild(o);
  }
}
function fillModelSelect(sel) {
  sel.innerHTML = '';
  for (const m of CONFIG.models || ['sonnet']) {
    const o = document.createElement('option');
    o.value = m; o.textContent = m;
    if (m === CONFIG.default_model) o.selected = true;
    sel.appendChild(o);
  }
}
async function loadTasks() {
  TASKS = await (await fetch('/api/tasks')).json();
  renderTaskList();
}
function taskItemHtml(t) {
  return `<div class="task-item${t.id === currentTaskId ? ' active' : ''}" data-openchat="${t.id}" title="${esc(t.title)}">
    <span class="t-ava">${avatarHtml(t.agent_avatar)}</span>
    <span class="t-dot ${t.status}"></span>
    <span class="t-title">${esc(t.title)}</span>
    <span class="t-acts">
      <span class="t-pin" data-pinchat="${t.id}" title="${t.pinned ? '取消置顶' : '置顶'}">${t.pinned ? '📌' : '📍'}</span>
      <span class="t-del" data-delchat="${t.id}" title="删除聊天">✕</span>
    </span>
  </div>`;
}
function renderTaskList(filter) {
  const show = t => !filter || (t.title || '').includes(filter);
  $('#pinned-list').innerHTML = TASKS.filter(t => t.pinned && show(t)).map(taskItemHtml).join('');
  $('#task-list').innerHTML = TASKS.filter(t => !t.pinned && show(t)).map(taskItemHtml).join('');
}
async function togglePinChat(id) {
  const t = TASKS.find(x => x.id === id); if (!t) return;
  const res = await fetch(`/api/tasks/${id}/pin`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ pinned: !t.pinned }) });
  const nt = await res.json(); if (nt.error) return;
  t.pinned = nt.pinned; renderTaskList('');
}
async function deleteChat(id) {
  if (!confirm('删除这条聊天信息？（不可恢复）')) return;
  await fetch(`/api/tasks/${id}/delete`, { method: 'POST' });
  TASKS = TASKS.filter(x => x.id !== id);
  if (currentTaskId === id) { currentTaskId = null; if (es) { es.close(); es = null; } msgContainer().innerHTML = '<div id="empty-state"><div class="big">🐧</div><p>选一条聊天信息，或点右边好友开聊～</p></div>'; $('#chat-title').textContent = '欢迎使用'; }
  renderTaskList('');
}
function setTaskStatus(id, status) {
  const t = TASKS.find(x => x.id === id);
  if (t) { t.status = status; renderTaskList(''); }
  $('#stop-btn').classList.toggle('show', status === 'running');
}
function openTask(id) {
  showView('chat');
  currentTaskId = id;
  const t = TASKS.find(x => x.id === id);
  // 当前对话的对方身份:优先任务自带的 agent，其次从标题里取名字
  currentAgent = {
    name: (t && t.agent_name) || (t ? (t.title || '').replace(/^💬\s*/, '') : '') || 'Claude',
    avatar: (t && t.agent_avatar) || 'qq1',
  };
  $('#chat-avatar').innerHTML = avatarHtml(currentAgent.avatar);
  $('#chat-title').textContent = t ? t.title : '';
  $('#win-title').textContent = `Claude Code 2007 - ${t ? t.title : ''}`;
  $('#chat-meta').textContent = t && t.model ? `模型 ${t.model}` : '';
  msgContainer().innerHTML = '';
  view = newView();
  renderedCount = 0;
  renderTaskList('');
  $('#stop-btn').classList.toggle('show', t && t.status === 'running');
  if (es) { es.close(); es = null; }
  es = new EventSource(`/api/tasks/${id}/events`);
  let seen = 0;
  es.onmessage = e => {
    seen++;
    if (seen <= renderedCount) return;   // 重连时跳过已渲染部分
    renderedCount = seen;
    try { handleEvent(JSON.parse(e.data)); } catch (err) { console.warn(err); }
  };
  es.onerror = () => { seen = 0; };      // 浏览器会自动重连并重放
}

/* ================= 交互 ================= */
function openNewTaskModal(projectName, prefillPrompt) {
  $('#modal-mask').classList.add('show');
  if (projectName) $('#f-project').value = projectName;
  if (prefillPrompt) $('#f-prompt').value = prefillPrompt;
  $('#f-prompt').focus();
}
function closeModal() { $('#modal-mask').classList.remove('show'); }

async function createTask() {
  const prompt = $('#f-prompt').value.trim();
  if (!prompt) { $('#f-prompt').focus(); return; }
  const body = {
    title: $('#f-title').value.trim(),
    project: $('#f-project').value,
    model: $('#f-model').value,
    permission_mode: $('#f-perm').value,
    prompt,
  };
  const res = await fetch('/api/tasks', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  const task = await res.json();
  if (task.error) { alert(task.error); return; }
  closeModal();
  $('#f-title').value = ''; $('#f-prompt').value = '';
  TASKS.unshift(task);
  openTask(task.id);
}

let sending = false;
async function sendMessage() {
  if (sending) return;
  const text = $('#prompt').value.trim();
  if (!text) return;
  if (!currentTaskId) {
    // 还没有打开任务：把这句话带进「新建任务」，避免点了发送却毫无反应
    openNewTaskModal(null, text);
    $('#prompt').value = '';
    return;
  }
  sending = true; $('#send-btn').disabled = true;
  try {
    const res = await fetch(`/api/tasks/${currentTaskId}/message`, {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ text })
    });
    const t = await res.json();
    if (t.error) { alert(t.error); return; }
    $('#prompt').value = '';
    setTaskStatus(currentTaskId, 'running');
  } finally { sending = false; $('#send-btn').disabled = false; }
}

/* 赞 / 踩 / 分享 —— 让它们真正发挥作用 */
function handleReact(el) {
  const actions = el.closest('.msg-actions');
  const kind = el.dataset.react;
  if (kind === 'like') {
    const on = !actions.classList.contains('liked');
    actions.classList.toggle('liked', on); actions.classList.remove('disliked');
    toast(on ? '已赞 👍' : '已取消');
  } else if (kind === 'dislike') {
    const on = !actions.classList.contains('disliked');
    actions.classList.toggle('disliked', on); actions.classList.remove('liked');
    if (on && currentTaskId && confirm('这条不太满意？让 TA 换个思路重新回答一次。')) {
      sendToCurrent('刚才那条回答我不太满意，请换个思路、重新回答一次。');
    }
  } else if (kind === 'share') {
    const line = el.closest('.result-line');
    let node = line ? line.previousElementSibling : null;
    while (node && !(node.classList && node.classList.contains('agent'))) node = node.previousElementSibling;
    const body = node && node.querySelector('.m-body');
    const text = body ? body.innerText.trim() : '';
    if (text) navigator.clipboard.writeText(text).then(() => toast('已复制这条回答 📋'));
    else toast('没找到可复制的内容');
  }
}
async function sendToCurrent(text) {
  if (!currentTaskId || sending) return;
  sending = true;
  try {
    const res = await fetch(`/api/tasks/${currentTaskId}/message`, {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ text }) });
    const t = await res.json();
    if (t.error) { alert(t.error); return; }
    setTaskStatus(currentTaskId, 'running');
  } finally { sending = false; }
}
let toastTimer;
function toast(msg) {
  let t = document.getElementById('mini-toast');
  if (!t) { t = document.createElement('div'); t.id = 'mini-toast'; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 1600);
}

document.addEventListener('click', e => {
  const act = e.target.closest('[data-act]');
  if (act) {
    const a = act.dataset.act;
    if (a === 'addfriend') openFriendModal();
    else if (a === 'new') openNewTaskModal();
    else if (a === 'chat') { showView('chat'); if (TASKS[0]) openTask(TASKS[0].id); }
    else if (a === 'sched') showView('sched');
    else if (a === 'md') showView('md');
    else if (a === 'plugins') showView('plugins');
    else if (a === 'skills') showView('skills');
    else if (a === 'games') showView('games');
    else if (a === 'qzone') showView('qzone');
    else if (a === 'todo') { showView('chat'); addSysLine('🚧 该功能敬请期待（v2）'); }
    return;
  }
  // 点头像 = 看大图 + 编辑资料；点删除 = 删好友；点其余 = 开聊
  const ef = e.target.closest('[data-editfriend]');
  if (ef) { e.stopPropagation(); openFriendModal(FRIEND_LIST.find(x => x.id === ef.dataset.editfriend)); return; }
  const df = e.target.closest('[data-delfriend]');
  if (df) { e.stopPropagation(); deleteFriend(df.dataset.delfriend); return; }
  const fr = e.target.closest('[data-friend]');
  if (fr) { startFriendChat(FRIEND_LIST[+fr.dataset.friend]); return; }
  // 聊天信息:打开 / 置顶 / 删除
  const pc = e.target.closest('[data-pinchat]');
  if (pc) { e.stopPropagation(); togglePinChat(pc.dataset.pinchat); return; }
  const dc = e.target.closest('[data-delchat]');
  if (dc) { e.stopPropagation(); deleteChat(dc.dataset.delchat); return; }
  const oc = e.target.closest('[data-openchat]');
  if (oc) { openTask(oc.dataset.openchat); return; }
  // 我的 skill
  const es2 = e.target.closest('[data-editskill]');
  if (es2) { editSkill(es2.dataset.editskill); return; }
  const ds = e.target.closest('[data-delskill]');
  if (ds) { deleteSkill(ds.dataset.delskill); return; }
  const gc = e.target.closest('[data-game]');
  if (gc) { launchGame(gc.dataset.game); return; }
  const lm = e.target.closest('[data-likemoment]');
  if (lm) { likeMoment(lm.dataset.likemoment); return; }
  const dm = e.target.closest('[data-delmoment]');
  if (dm) { delMoment(dm.dataset.delmoment); return; }
  // 定时任务卡片操作
  const sw = e.target.closest('[data-sw]');
  if (sw) { toggleSchedule(sw.dataset.sw, !sw.classList.contains('on')); return; }
  const run = e.target.closest('[data-run]');
  if (run) { runSchedule(run.dataset.run); return; }
  const del = e.target.closest('[data-del]');
  if (del) { deleteSchedule(del.dataset.del); return; }
  const react = e.target.closest('.react[data-react]');
  if (react) { handleReact(react); return; }
  const copy = e.target.closest('.cb-copy');
  if (copy) {
    navigator.clipboard.writeText(copy.dataset.code).then(() => {
      copy.textContent = '✔ 已复制';
      setTimeout(() => copy.textContent = '📋 复制', 1500);
    });
    return;
  }
  const trhd = e.target.closest('.tr-hd');
  if (trhd) { trhd.parentElement.classList.toggle('open'); return; }
  const thhd = e.target.closest('.th-hd');
  if (thhd) { thhd.parentElement.classList.toggle('open'); return; }
  const ghd = e.target.closest('.group-hd[data-toggle]');
  if (ghd) {
    const bd = document.getElementById(ghd.dataset.toggle);
    bd.classList.toggle('hide');
    ghd.querySelector('.arrow').textContent = bd.classList.contains('hide') ? '▼' : '▲';
  }
  if (!e.target.closest('#emoji-panel') && !e.target.closest('#emoji-btn'))
    $('#emoji-panel').classList.remove('show');
  if (!e.target.closest('#slash-pop') && !e.target.closest('#slash-btn') &&
      !['prompt', 'f-prompt', 's-prompt'].includes(e.target.id))
    slashClose();
  if (!e.target.closest('#attach-pop') && !e.target.closest('#attach-btn'))
    closeAttachPop();
});

$('#send-btn').onclick = sendMessage;
$('#prompt').addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); sendMessage(); }
});
$('#f-ok').onclick = createTask;
$('#f-cancel').onclick = closeModal;
$('#modal-close').onclick = closeModal;
$('#stop-btn').onclick = async () => {
  if (!currentTaskId) return;
  await fetch(`/api/tasks/${currentTaskId}/interrupt`, { method: 'POST' });
  setTaskStatus(currentTaskId, 'idle');
  addSysLine('⏹ 已停止');
};

/* ================= 斜杠命令自动补全 ================= */
async function loadSlash() {
  try { SLASH = await (await fetch('/api/slashcommands')).json(); }
  catch (e) { SLASH = []; }
}
const slashPop = $('#slash-pop');
let slashState = { ta: null, items: [], active: 0, open: false };

function slashClose() { slashPop.classList.remove('show'); slashState.open = false; slashState.ta = null; }
function slashQuery(ta) {
  // 只有当整条消息以 "/" 开头、且还没敲空格时，才把 "/" 后面的词当查询
  const m = ta.value.match(/^\/([a-z0-9:_-]*)$/i);
  return m ? m[1].toLowerCase() : null;
}
function slashFilter(q) {
  if (!q) return SLASH.slice(0, 50);
  return SLASH.filter(c => c.name.toLowerCase().includes(q))
    .sort((a, b) => (a.name.toLowerCase().startsWith(q) ? 0 : 1) - (b.name.toLowerCase().startsWith(q) ? 0 : 1))
    .slice(0, 50);
}
function slashRender() {
  const { items, active } = slashState;
  if (!items.length) { slashClose(); return; }
  slashPop.innerHTML = '<div class="sp-hd">斜杠命令 · ↑↓ 选择 · Enter/Tab 采用 · Esc 关闭</div>' +
    items.map((c, i) => `<div class="sp-item${i === active ? ' active' : ''}" data-i="${i}">` +
      `<span class="sp-name">/${esc(c.name)}</span><span class="sp-desc">${esc(c.desc || '')}</span></div>`).join('');
  slashPop.classList.add('show');
  const act = slashPop.querySelector('.sp-item.active');
  if (act) act.scrollIntoView({ block: 'nearest' });
}
function slashPosition(ta) {
  const r = ta.getBoundingClientRect();
  slashPop.style.left = r.left + 'px';
  slashPop.style.top = '-9999px';
  slashPop.classList.add('show');
  requestAnimationFrame(() => { slashPop.style.top = (r.top - slashPop.offsetHeight - 4) + 'px'; });
}
function slashOpenFor(ta, forceAll) {
  const q = forceAll ? '' : slashQuery(ta);
  if (q === null) { slashClose(); return; }
  slashState.ta = ta; slashState.items = slashFilter(q); slashState.active = 0; slashState.open = true;
  slashRender(); slashPosition(ta);
}
function slashAccept() {
  const c = slashState.items[slashState.active], ta = slashState.ta;
  if (!c || !ta) return;
  ta.value = '/' + c.name + ' '; ta.focus();
  const end = ta.value.length; ta.setSelectionRange(end, end);
  slashClose();
}
function slashKeydown(e) {
  if (!slashState.open) return;
  if (e.key === 'ArrowDown') { e.preventDefault(); e.stopPropagation(); slashState.active = (slashState.active + 1) % slashState.items.length; slashRender(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); e.stopPropagation(); slashState.active = (slashState.active - 1 + slashState.items.length) % slashState.items.length; slashRender(); }
  else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); e.stopPropagation(); slashAccept(); }
  else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); slashClose(); }
}
function attachSlash(ta) {
  ta.addEventListener('keydown', slashKeydown, true);  // 捕获阶段，先于 Ctrl+Enter 发送
  ta.addEventListener('input', () => { if (slashState.open || slashQuery(ta) !== null) slashOpenFor(ta); });
  ta.addEventListener('blur', () => setTimeout(() => { if (!slashPop.matches(':hover')) slashClose(); }, 150));
}
slashPop.addEventListener('mousedown', e => {
  const it = e.target.closest('.sp-item'); if (!it) return;
  e.preventDefault(); slashState.active = +it.dataset.i; slashAccept();
});
attachSlash($('#prompt'));
attachSlash($('#f-prompt'));
attachSlash($('#s-prompt'));
$('#slash-btn').addEventListener('mousedown', e => e.preventDefault());
$('#slash-btn').addEventListener('click', () => { const ta = $('#prompt'); ta.focus(); slashOpenFor(ta, true); });

/* ================= 附加:允许 Claude 访问的目录（--add-dir） ================= */
const attachPop = $('#attach-pop');
function currentTask() { return TASKS.find(t => t.id === currentTaskId); }
function renderAttachPop() {
  const t = currentTask();
  if (!t) { attachPop.innerHTML = '<div class="ap-hd">先打开或新建一个任务，才能设置它可访问的目录。</div>'; return; }
  const dirs = t.add_dirs || [];
  const quick = (CONFIG.projects || []).map(p => p.abspath).filter(d => d && d !== t.cwd && !dirs.includes(d));
  attachPop.innerHTML =
    '<div class="ap-hd">本任务里 Claude 可访问的目录（额外授权 --add-dir）：</div>' +
    `<div class="ap-row"><span class="ap-path ap-cwd" title="${esc(t.cwd)}">📁 ${esc(t.cwd)}</span><span style="font-size:12px;color:#8aa">主目录</span></div>` +
    dirs.map(d => `<div class="ap-row"><span class="ap-path" title="${esc(d)}">➕ ${esc(d)}</span><span class="ap-rm" data-rmdir="${esc(d)}" title="移除">✕</span></div>`).join('') +
    (quick.length ? '<div class="ap-quick">' + quick.map(d => `<span class="ap-chip" data-adddir="${esc(d)}">+ ${esc(d.split('/').pop() || d)}</span>`).join('') + '</div>' : '') +
    '<div class="ap-add"><input id="ap-input" placeholder="输入/粘贴目录路径，支持 ~"><button class="mini-btn" id="ap-add-btn">添加</button></div>';
}
function openAttachPop() {
  renderAttachPop();
  const r = $('#attach-btn').getBoundingClientRect();
  attachPop.style.left = r.left + 'px';
  attachPop.style.top = '-9999px';
  attachPop.classList.add('show');
  requestAnimationFrame(() => { attachPop.style.top = (r.top - attachPop.offsetHeight - 6) + 'px'; });
}
function closeAttachPop() { attachPop.classList.remove('show'); }
async function attachDirApi(action, path) {
  if (!path || !currentTaskId) return;
  const res = await fetch(`/api/tasks/${currentTaskId}/${action}`, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ path }) });
  const t = await res.json();
  if (t.error) { alert(t.error); return; }
  const i = TASKS.findIndex(x => x.id === t.id); if (i >= 0) TASKS[i] = t;
  renderAttachPop();
}
$('#attach-btn').addEventListener('click', (e) => {
  e.stopPropagation();
  attachPop.classList.contains('show') ? closeAttachPop() : openAttachPop();
});
attachPop.addEventListener('click', (e) => {
  const rm = e.target.closest('[data-rmdir]'); if (rm) { attachDirApi('rmdir', rm.dataset.rmdir); return; }
  const ad = e.target.closest('[data-adddir]'); if (ad) { attachDirApi('adddir', ad.dataset.adddir); return; }
  if (e.target.closest('#ap-add-btn')) { const inp = $('#ap-input'); attachDirApi('adddir', inp.value.trim()); inp.value = ''; }
});
attachPop.addEventListener('keydown', (e) => {
  if (e.target.id === 'ap-input' && e.key === 'Enter') { e.preventDefault(); attachDirApi('adddir', e.target.value.trim()); e.target.value = ''; }
});

/* ================= 视图切换 ================= */
function showView(name) {
  const chat = name === 'chat';
  $('#chat-hd').style.display = chat ? '' : 'none';
  $('#messages').style.display = chat ? '' : 'none';
  $('#input-area').style.display = chat ? '' : 'none';
  $('#sched-view').classList.toggle('show', name === 'sched');
  $('#md-view').classList.toggle('show', name === 'md');
  $('#plugins-view').classList.toggle('show', name === 'plugins');
  $('#skills-view').classList.toggle('show', name === 'skills');
  $('#games-view').classList.toggle('show', name === 'games');
  $('#qzone-view').classList.toggle('show', name === 'qzone');
  if (name !== 'games') gameHall();   // 离开就停掉正在跑的游戏
  if (name === 'sched') loadSchedules();
  else if (name === 'md') loadClaudeMd($('#md-project').value);
  else if (name === 'plugins') loadCapabilities();
  else if (name === 'skills') loadSkills();
  else if (name === 'games') gameHall();
  else if (name === 'qzone') loadMoments();
}

/* ================= QQ空间 动态 ================= */
async function loadMoments() {
  $('#qz-me').innerHTML = `<span class="qz-ava" style="width:22px;height:22px">${avatarHtml(ME.avatar)}</span>${esc(ME.name)} 的空间`;
  const list = await (await fetch('/api/moments')).json();
  $('#qz-feed').innerHTML = list.map(m => `<div class="qz-item">
      <div class="qz-ava">${avatarHtml(m.author_avatar)}</div>
      <div class="qz-main">
        <div class="qz-name">${esc(m.author_name)}${m.mine ? '<span class="me-tag">我</span>' : ''}</div>
        <div class="qz-text">${esc(m.text)}</div>
        <div class="qz-foot">
          <span>${qzTime(m.ts)}</span>
          <span class="qz-act" data-likemoment="${m.id}">👍 赞 ${m.likes || 0}</span>
          ${m.mine ? `<span class="qz-act qz-del" data-delmoment="${m.id}">🗑 删除</span>` : ''}
        </div>
      </div>
    </div>`).join('') || '<div class="cap-empty">还没有动态，发一条吧～</div>';
}
function qzTime(ts) {
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return (diff / 60 | 0) + ' 分钟前';
  if (diff < 86400) return (diff / 3600 | 0) + ' 小时前';
  return fmtDateTime(ts);
}
async function postMoment() {
  const text = $('#qz-text').value.trim();
  if (!text) return;
  const res = await fetch('/api/moments', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ text }) });
  const r = await res.json();
  if (r.error) { alert(r.error); return; }
  $('#qz-text').value = ''; loadMoments();
}
async function likeMoment(id) {
  await fetch(`/api/moments/${id}/like`, { method: 'POST' }); loadMoments();
}
async function delMoment(id) {
  if (!confirm('删除这条动态？')) return;
  await fetch(`/api/moments/${id}/delete`, { method: 'POST' }); loadMoments();
}

/* ================= QQ小游戏 ================= */
let gameTeardown = null;
function gameHall() {
  if (gameTeardown) { gameTeardown(); gameTeardown = null; }
  $('#game-menu').style.display = '';
  $('#game-stage').style.display = 'none';
  $('#game-stage').innerHTML = '';
  $('#game-back').style.display = 'none';
  $('#game-score').textContent = '';
}
function launchGame(id) {
  if (gameTeardown) { gameTeardown(); gameTeardown = null; }
  $('#game-menu').style.display = 'none';
  const stage = $('#game-stage'); stage.style.display = 'flex'; stage.innerHTML = '';
  $('#game-back').style.display = '';
  gameTeardown = ({ snake: gameSnake, g2048: game2048, memory: gameMemory }[id] || (() => {}))(stage);
}
function setScore(s) { $('#game-score').textContent = s; }

/* —— 贪吃蛇 —— */
function gameSnake(stage) {
  const N = 17, CELL = 20, W = N * CELL;
  const c = document.createElement('canvas'); c.width = W; c.height = W;
  const hint = document.createElement('div'); hint.className = 'game-hint'; hint.textContent = '方向键 / WASD 控制，空格重开';
  stage.append(c, hint);
  const ctx = c.getContext('2d');
  let snake, dir, nextDir, food, score, dead, timer;
  function reset() {
    snake = [{ x: 8, y: 8 }, { x: 7, y: 8 }, { x: 6, y: 8 }];
    dir = { x: 1, y: 0 }; nextDir = dir; score = 0; dead = false; placeFood(); setScore('得分 0');
  }
  function placeFood() {
    do { food = { x: (Math.random() * N) | 0, y: (Math.random() * N) | 0 }; }
    while (snake.some(s => s.x === food.x && s.y === food.y));
  }
  function step() {
    if (dead) return;
    dir = nextDir;
    const head = { x: snake[0].x + dir.x, y: snake[0].y + dir.y };
    if (head.x < 0 || head.y < 0 || head.x >= N || head.y >= N || snake.some(s => s.x === head.x && s.y === head.y)) {
      dead = true; draw(); return;
    }
    snake.unshift(head);
    if (head.x === food.x && head.y === food.y) { score += 10; setScore('得分 ' + score); placeFood(); }
    else snake.pop();
    draw();
  }
  function draw() {
    ctx.fillStyle = '#eaf3d0'; ctx.fillRect(0, 0, W, W);
    ctx.fillStyle = '#e5533c'; ctx.beginPath();
    ctx.arc(food.x * CELL + CELL / 2, food.y * CELL + CELL / 2, CELL / 2 - 2, 0, 7); ctx.fill();
    snake.forEach((s, i) => {
      ctx.fillStyle = i === 0 ? '#1b5cc8' : '#4b8ae8';
      ctx.fillRect(s.x * CELL + 1, s.y * CELL + 1, CELL - 2, CELL - 2);
    });
    if (dead) {
      ctx.fillStyle = 'rgba(0,0,0,.5)'; ctx.fillRect(0, 0, W, W);
      ctx.fillStyle = '#fff'; ctx.font = 'bold 22px sans-serif'; ctx.textAlign = 'center';
      ctx.fillText('游戏结束  得分 ' + score, W / 2, W / 2 - 6);
      ctx.font = '13px sans-serif'; ctx.fillText('按空格重开', W / 2, W / 2 + 20);
    }
  }
  const key = e => {
    const m = { ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0], w: [0, -1], s: [0, 1], a: [-1, 0], d: [1, 0] };
    if (e.key === ' ') { if (dead) reset(); e.preventDefault(); return; }
    const v = m[e.key]; if (!v) return;
    e.preventDefault();
    if (v[0] === -dir.x && v[1] === -dir.y) return;   // 不能反向
    nextDir = { x: v[0], y: v[1] };
  };
  window.addEventListener('keydown', key);
  reset(); draw();
  timer = setInterval(step, 120);
  return () => { clearInterval(timer); window.removeEventListener('keydown', key); };
}

/* —— 2048 —— */
function game2048(stage) {
  const grid = document.createElement('div'); grid.className = 'g2048';
  const hint = document.createElement('div'); hint.className = 'game-hint'; hint.textContent = '方向键移动合并，R 重开';
  stage.append(grid, hint);
  let board, score, over;
  const COLORS = { 2: '#eee4da', 4: '#ede0c8', 8: '#f2b179', 16: '#f59563', 32: '#f67c5f', 64: '#f65e3b', 128: '#edcf72', 256: '#edcc61', 512: '#edc850', 1024: '#edc53f', 2048: '#edc22e' };
  function reset() { board = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]; score = 0; over = false; add(); add(); draw(); }
  function add() {
    const empty = board.map((v, i) => v ? -1 : i).filter(i => i >= 0);
    if (!empty.length) return;
    board[empty[(Math.random() * empty.length) | 0]] = Math.random() < 0.9 ? 2 : 4;
  }
  function draw() {
    grid.innerHTML = board.map(v => {
      const bg = v ? (COLORS[v] || '#3c3a32') : '#cdc1b4';
      const col = v <= 4 ? '#776e65' : '#f9f6f2';
      return `<div class="cell" style="background:${bg};color:${col};font-size:${v > 512 ? 18 : 22}px">${v || ''}</div>`;
    }).join('');
    setScore('得分 ' + score + (over ? ' · 结束(按R重开)' : ''));
  }
  function slide(row) {
    let a = row.filter(x => x);
    for (let i = 0; i < a.length - 1; i++) if (a[i] === a[i + 1]) { a[i] *= 2; score += a[i]; a[i + 1] = 0; }
    a = a.filter(x => x);
    while (a.length < 4) a.push(0);
    return a;
  }
  function move(dir) {
    if (over) return;
    const before = board.join(',');
    let rows = [];
    for (let r = 0; r < 4; r++) {
      let idx = [0, 1, 2, 3].map(cc => dir === 'L' || dir === 'R' ? r * 4 + cc : cc * 4 + r);
      if (dir === 'R' || dir === 'D') idx.reverse();
      let row = idx.map(i => board[i]);
      row = slide(row);
      idx.forEach((i, k) => board[i] = row[k]);
    }
    if (board.join(',') !== before) { add(); if (!canMove()) over = true; }
    draw();
  }
  function canMove() {
    if (board.includes(0)) return true;
    for (let r = 0; r < 4; r++) for (let cc = 0; cc < 4; cc++) {
      const i = r * 4 + cc;
      if (cc < 3 && board[i] === board[i + 1]) return true;
      if (r < 3 && board[i] === board[i + 4]) return true;
    }
    return false;
  }
  const key = e => {
    const m = { ArrowLeft: 'L', ArrowRight: 'R', ArrowUp: 'U', ArrowDown: 'D' };
    if (e.key === 'r' || e.key === 'R') { reset(); return; }
    if (!m[e.key]) return; e.preventDefault(); move(m[e.key]);
  };
  window.addEventListener('keydown', key);
  reset();
  return () => window.removeEventListener('keydown', key);
}

/* —— 翻牌记忆 —— */
function gameMemory(stage) {
  const board = document.createElement('div'); board.className = 'memory';
  const hint = document.createElement('div'); hint.className = 'game-hint'; hint.textContent = '翻开两张相同的配对';
  stage.append(board, hint);
  const EMO = ['🐧', '🐱', '🐶', '🦊', '🐼', '🐸', '🦄', '🐷'];
  let deck = [...EMO, ...EMO].sort(() => Math.random() - 0.5);
  let flipped = [], matched = 0, lock = false, moves = 0;
  board.innerHTML = deck.map((e, i) => `<div class="card" data-i="${i}">${e}</div>`).join('');
  setScore('步数 0');
  const onClick = ev => {
    const card = ev.target.closest('.card'); if (!card || lock) return;
    const i = +card.dataset.i;
    if (card.classList.contains('flip') || card.classList.contains('done')) return;
    card.classList.add('flip'); flipped.push(card);
    if (flipped.length === 2) {
      moves++; setScore('步数 ' + moves); lock = true;
      const [a, b] = flipped;
      if (deck[+a.dataset.i] === deck[+b.dataset.i]) {
        setTimeout(() => { a.classList.add('done'); b.classList.add('done'); a.classList.remove('flip'); b.classList.remove('flip'); flipped = []; lock = false; matched += 2; if (matched === deck.length) setScore('🎉 通关！步数 ' + moves); }, 350);
      } else {
        setTimeout(() => { a.classList.remove('flip'); b.classList.remove('flip'); flipped = []; lock = false; }, 700);
      }
    }
  };
  board.addEventListener('click', onClick);
  return () => board.removeEventListener('click', onClick);
}

/* ================= 我的 skill 管理 ================= */
let editingSkillDir = null;
async function loadSkills() {
  const list = await (await fetch('/api/skills')).json();
  const box = $('#skills-list');
  if (!list.length) {
    box.innerHTML = '<div class="sched-empty">🛠️ 还没有 skill。点右上角「＋ 新建 skill」，<br>写好后 Claude 会在合适的时候自动调用它。</div>';
    return;
  }
  box.innerHTML = list.map(s => `<div class="sched-card">
      <div class="sc-top">
        <span class="sc-title">${esc(s.name)}</span>
        <span class="sc-when" style="color:#8aa">${esc(s.dir)}</span>
        <div class="sc-acts">
          <button class="mini-btn" data-editskill="${esc(s.dir)}">✎ 编辑</button>
          <button class="mini-btn danger" data-delskill="${esc(s.dir)}">删除</button>
        </div>
      </div>
      <div class="sc-prompt">${esc(s.description || '（无说明）')}</div>
    </div>`).join('');
}
function openSkillModal() {
  editingSkillDir = null;
  $('#skill-mtitle').textContent = '🛠️ 新建 skill';
  $('#sk-name').disabled = false;
  $('#sk-name').value = ''; $('#sk-desc').value = ''; $('#sk-body').value = '';
  $('#skill-mask').classList.add('show');
  $('#sk-name').focus();
}
async function editSkill(dir) {
  const s = await (await fetch('/api/skills/' + encodeURIComponent(dir))).json();
  if (s.error) { alert(s.error); return; }
  editingSkillDir = dir;
  $('#skill-mtitle').textContent = '🛠️ 编辑 skill';
  $('#sk-name').value = s.name; $('#sk-name').disabled = true;   // 名称不改（对应目录）
  $('#sk-desc').value = s.description || ''; $('#sk-body').value = s.body || '';
  $('#skill-mask').classList.add('show');
  $('#sk-desc').focus();
}
function closeSkillModal() { $('#skill-mask').classList.remove('show'); }
async function saveSkill() {
  const desc = $('#sk-desc').value.trim(), bodyText = $('#sk-body').value;
  let res;
  if (editingSkillDir) {
    res = await fetch(`/api/skills/${encodeURIComponent(editingSkillDir)}/save`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ description: desc, body: bodyText }) });
  } else {
    const name = $('#sk-name').value.trim();
    if (!name) { $('#sk-name').focus(); return; }
    res = await fetch('/api/skills', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ name, description: desc, body: bodyText }) });
  }
  const r = await res.json();
  if (r.error) { alert(r.error); return; }
  closeSkillModal(); loadSkills();
}
async function deleteSkill(dir) {
  if (!confirm(`删除 skill「${dir}」？会删掉 ~/.claude/skills/${dir} 整个目录，不可恢复。`)) return;
  await fetch(`/api/skills/${encodeURIComponent(dir)}/delete`, { method: 'POST' });
  loadSkills();
}

/* ================= 插件 / 能力面板 ================= */
async function loadCapabilities() {
  const c = await (await fetch('/api/capabilities')).json();
  const body = $('#plugins-body'), meta = $('#plugins-meta');
  if (!c || !c.updated_at) {
    meta.textContent = '';
    body.innerHTML = '<div class="cap-empty">🧩 还没抓到能力信息。<br>随便跑一个任务（新建任务里发一句），这里就会自动列出你本机的 MCP / 技能 / 子代理 / 插件。</div>';
    return;
  }
  meta.textContent = `Claude Code ${c.version || ''} · 更新于 ${fmtDateTime(c.updated_at)}`;
  const statusClass = s => s === 'connected' ? 'ok' : (s === 'pending' || s === 'needs-auth') ? 'wait' : (s ? 'err' : '');
  const sec = (icon, title, n, inner) =>
    `<div class="cap-sec"><div class="cap-hd">${icon} ${title}<span class="cap-n">(${n})</span></div><div class="cap-grid">${inner}</div></div>`;
  const chips = arr => (arr && arr.length)
    ? arr.map(x => `<span class="cap-chip">${esc(x)}</span>`).join('')
    : '<span class="cap-empty" style="padding:6px">（无）</span>';
  const mcp = (c.mcp_servers || []);
  const mcpChips = mcp.length
    ? mcp.map(m => `<span class="cap-chip"><span class="dot ${statusClass(m.status)}"></span>${esc(m.name)} <span style="color:#8aa;font-size:13px">${esc(m.status||'')}</span></span>`).join('')
    : '<span class="cap-empty" style="padding:6px">（无）</span>';
  body.innerHTML =
    sec('🔌', 'MCP 服务器', mcp.length, mcpChips) +
    sec('🛠️', '技能 Skills', (c.skills||[]).length, chips(c.skills)) +
    sec('🤝', '子代理 Agents', (c.agents||[]).length, chips(c.agents)) +
    sec('🧩', '插件 Plugins', (c.plugins||[]).length, chips(c.plugins)) +
    sec('⚡', '斜杠命令', c.slash_count || 0, `<span class="cap-chip">共 ${c.slash_count||0} 个（输入框打 / 可补全）</span>`);
}

/* ================= 定时任务 ================= */
function fmtDateTime(ts) {
  const d = new Date(ts * 1000);
  return `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
}
function schedWhen(s) {
  if (s.sched_type === 'daily') return `每天 ${s.at_time}`;
  if (s.sched_type === 'interval') return `每 ${s.interval_min} 分钟`;
  if (s.sched_type === 'once') return `一次性 ${s.at_datetime ? fmtDateTime(s.at_datetime) : ''}`;
  return '';
}
async function loadSchedules() {
  const list = await (await fetch('/api/schedules')).json();
  const box = $('#sched-list');
  if (!list.length) {
    box.innerHTML = '<div class="sched-empty">🐧 还没有定时任务。<br>点右上角「＋ 新建定时任务」，让 Claude 按点自动干活 ⏰</div>';
    return;
  }
  box.innerHTML = list.map(s => {
    const next = s.enabled && s.next_run ? `下次 ${fmtDateTime(s.next_run)}` : '已暂停';
    const last = s.last_run ? ` · 上次 ${fmtDateTime(s.last_run)}` : '';
    const err = s.last_error ? ` · <span style="color:#c0392b">${esc(s.last_error)}</span>` : '';
    return `<div class="sched-card${s.enabled ? '' : ' off'}">
      <div class="sc-top">
        <span class="sc-title">${esc(s.title || s.prompt.slice(0,16))}</span>
        <span class="sc-when">${esc(schedWhen(s))}</span>
        <div class="sc-acts">
          <div class="switch ${s.enabled ? 'on' : ''}" data-sw="${s.id}" title="启用/暂停"></div>
          <button class="mini-btn" data-run="${s.id}">▶ 立即运行</button>
          <button class="mini-btn danger" data-del="${s.id}">删除</button>
        </div>
      </div>
      <div class="sc-meta">${esc(s.project || '(默认)')} · ${esc(s.model)} · ${esc(s.permission_mode)} · ${next}${last}${err}</div>
      <div class="sc-prompt">${esc(s.prompt)}</div>
    </div>`;
  }).join('');
}
function openSchedModal() {
  $('#sched-mask').classList.add('show');
  syncSchedRows();
  $('#s-prompt').focus();
}
function closeSchedModal() { $('#sched-mask').classList.remove('show'); }
function syncSchedRows() {
  const t = $('#s-type').value;
  $('#s-row-time').style.display = t === 'daily' ? '' : 'none';
  $('#s-row-interval').style.display = t === 'interval' ? '' : 'none';
  $('#s-row-once').style.display = t === 'once' ? '' : 'none';
}
async function createSchedule() {
  const prompt = $('#s-prompt').value.trim();
  if (!prompt) { $('#s-prompt').focus(); return; }
  const t = $('#s-type').value;
  const body = {
    title: $('#s-title').value.trim(), project: $('#s-project').value,
    model: $('#s-model').value, permission_mode: $('#s-perm').value, prompt, sched_type: t,
  };
  if (t === 'daily') body.at_time = $('#s-time').value || '09:00';
  else if (t === 'interval') body.interval_min = +$('#s-interval').value || 60;
  else if (t === 'once') {
    const v = $('#s-datetime').value;
    if (!v) { alert('请选择运行时间'); return; }
    body.at_datetime = new Date(v).getTime() / 1000;
  }
  const res = await fetch('/api/schedules', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  const r = await res.json();
  if (r.error) { alert(r.error); return; }
  closeSchedModal(); $('#s-title').value = ''; $('#s-prompt').value = '';
  loadSchedules();
}
async function toggleSchedule(id, enabled) {
  await fetch(`/api/schedules/${id}/toggle`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ enabled }) });
  loadSchedules();
}
async function runSchedule(id) {
  const res = await fetch(`/api/schedules/${id}/run`, { method: 'POST' });
  const r = await res.json();
  if (r.error) { alert(r.error); return; }
  if (r.id) { await loadTasks(); openTask(r.id); }   // 直接跳到刚触发的任务
}
async function deleteSchedule(id) {
  if (!confirm('删除这个定时任务？')) return;
  await fetch(`/api/schedules/${id}/delete`, { method: 'POST' });
  loadSchedules();
}

/* ================= CLAUDE.md 编辑 ================= */
async function loadClaudeMd(project) {
  if (!project) return;
  const r = await (await fetch('/api/claudemd?project=' + encodeURIComponent(project))).json();
  $('#md-path').textContent = r.path + (r.exists ? '' : '（尚不存在，保存后创建）');
  $('#md-editor').value = r.content || '';
}
async function saveClaudeMd() {
  const body = { project: $('#md-project').value, content: $('#md-editor').value };
  const res = await fetch('/api/claudemd', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  const r = await res.json();
  if (r.error) { alert(r.error); return; }
  const btn = $('#md-save-btn'), old = btn.textContent;
  btn.textContent = '✔ 已保存'; setTimeout(() => btn.textContent = old, 1500);
  $('#md-path').textContent = r.path;
}

$('#sched-new-btn').onclick = openSchedModal;
$('#s-ok').onclick = createSchedule;
$('#s-cancel').onclick = closeSchedModal;
$('#sm-close').onclick = closeSchedModal;
$('#s-type').onchange = syncSchedRows;
$('#md-project').onchange = () => loadClaudeMd($('#md-project').value);
$('#md-reload-btn').onclick = () => loadClaudeMd($('#md-project').value);
$('#md-save-btn').onclick = saveClaudeMd;
$('#fr-ok').onclick = createFriend;
$('#fr-cancel').onclick = closeFriendModal;
$('#fr-close').onclick = closeFriendModal;
$('#fr-del').onclick = deleteEditingFriend;
$('#fr-avatar').addEventListener('input', () => { if ($('#fr-avatar').value.trim()) renderBigAva($('#fr-avatar').value.trim()); });
$('#usercard').onclick = openProfileModal;
$('#pf-ok').onclick = saveProfile;
$('#pf-cancel').onclick = closeProfileModal;
$('#pf-close').onclick = closeProfileModal;
$('#skill-new-btn').onclick = openSkillModal;
$('#sk-ok').onclick = saveSkill;
$('#sk-cancel').onclick = closeSkillModal;
$('#sk-close').onclick = closeSkillModal;
$('#game-back').onclick = gameHall;
$('#qz-send').onclick = postMoment;

/* 无边框原生窗口(pywebview)时，标题栏按钮驱动真实窗口，并让 QQ 窗口铺满整窗 */
const pwApi = () => (window.pywebview && window.pywebview.api) || null;
function markNative() { document.documentElement.classList.add('native'); }
if (window.pywebview) markNative();
window.addEventListener('pywebviewready', markNative);

let winMaximized = false;
const RESTORE_W = 980, RESTORE_H = 760;
async function maximizeWin() {
  const a = pwApi(); if (!a) return;
  const wa = await a.get_workarea();             // [x,y,w,h] 已是 move() 兼容坐标(避开菜单栏/Dock)
  if (!wa) return;
  a.set_bounds(wa[0], wa[1], wa[2], wa[3]);
  winMaximized = true; $('#win-max').classList.add('restore');
}
async function restoreWin() {
  const a = pwApi(); if (!a) return;
  const wa = await a.get_workarea();
  if (!wa) return;
  const x = wa[0] + Math.max(0, (wa[2] - RESTORE_W) / 2);
  const y = wa[1] + Math.max(0, (wa[3] - RESTORE_H) / 2);
  a.set_bounds(x, y, RESTORE_W, RESTORE_H);
  winMaximized = false; $('#win-max').classList.remove('restore');
}
$('#win-min').onclick = () => { const a = pwApi(); if (a) a.minimize(); };
$('#win-max').onclick = () => { winMaximized ? restoreWin() : maximizeWin(); };
$('#win-close').onclick = () => { const a = pwApi(); if (a) a.close(); else window.close(); };

/* 标题栏拖动:只用鼠标坐标算窗口的绝对左上角(screenX/Y 与 move() 同为「左上原点」),
   不读 window.x/y(mac 上有坐标系 bug)。用 rAF 合并,每帧最多调一次 move,避免刷爆 JS 桥。 */
(() => {
  const bar = $('#tb-drag');
  let off = null, target = null, raf = 0;
  function pump() {
    if (!off) { raf = 0; return; }
    if (target) { const a = pwApi(); if (a) a.move(target[0], target[1]); target = null; }
    raf = requestAnimationFrame(pump);
  }
  bar.addEventListener('mousedown', (e) => {
    if (!pwApi() || winMaximized || e.button !== 0) return;
    off = { x: e.clientX, y: e.clientY };      // 鼠标在窗口内的偏移
    if (!raf) raf = requestAnimationFrame(pump);
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (off) target = [e.screenX - off.x, e.screenY - off.y];
  });
  window.addEventListener('mouseup', () => { off = null; });
  bar.addEventListener('dblclick', () => $('#win-max').click());  // 双击标题栏最大化/还原
})();

/* 表情面板 */
const EMOJIS =['😀','😂','😊','😎','🤔','😭','👍','👎','🙏','💪','🐧','🔥','✨','🎉','❤️','🚀','🐛','☕','😴','🤖','💡','⚠️','✅','❌'];
const ep = $('#emoji-panel');
EMOJIS.forEach(em => {
  const s = document.createElement('span');
  s.textContent = em;
  s.onclick = () => { const p = $('#prompt'); p.value += em; p.focus(); ep.classList.remove('show'); };
  ep.appendChild(s);
});
$('#emoji-btn').onclick = e => {
  const r = e.target.getBoundingClientRect();
  ep.style.left = r.left + 'px';
  ep.style.top = (r.top - 130) + 'px';
  ep.classList.toggle('show');
};

/* 时钟 */
setInterval(() => {
  const d = new Date();
  $('#clock').textContent = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
}, 1000);

/* 轮询任务状态（兜底，SSE 已覆盖大部分） */
setInterval(async () => {
  if (document.hidden) return;
  const fresh = await (await fetch('/api/tasks')).json();
  for (const f of fresh) {
    const t = TASKS.find(x => x.id === f.id);
    if (t && t.status !== f.status) { t.status = f.status; renderTaskList(''); }
    if (!t) TASKS.unshift(f);
  }
}, 5000);

/* ================= 启动 ================= */
(async () => {
  await loadConfig();
  await loadSlash();
  await loadAvatars();
  await loadProfile();
  await loadFriends();
  await loadTasks();
  if (TASKS[0]) openTask(TASKS[0].id);
})();
