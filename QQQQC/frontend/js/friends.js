/* ============================================================
 * Q-CC 前端 · friends.js
 * 职责：配置加载(loadConfig) + 头像(avatarSvg/Html) + 好友列表/开聊(startFriendChat 有会话则继续) + 我的资料 + 项目/模型下拉。
 * 说明：本文件是原 app.js 的一段(按功能拆分，逻辑未改)。
 *       经典 <script> 顶层声明共享同一全局作用域，靠 index.html
 *       的引入顺序保证依赖可见——请勿单独调整加载顺序。
 * ============================================================ */
/* ================= 任务列表 / SSE ================= */
async function loadConfig() {
  CONFIG = await (await fetch('/api/config')).json();
  uiPermMode = CONFIG.default_permission_mode || uiPermMode;   // 权限切换按钮的初始默认
  syncPermButton(uiPermMode);
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
  // CLAUDE.md 编辑：默认指向全局 ~/.claude/CLAUDE.md，放在项目列表最前并默认选中
  (() => {
    const sel = $('#md-project');
    if (sel && !sel.querySelector('option[value="__global__"]')) {
      const g = document.createElement('option');
      g.value = '__global__'; g.textContent = '全局 (~/.claude)';
      sel.insertBefore(g, sel.firstChild);
    }
    if (sel) sel.value = '__global__';
  })();
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
  $('#friends-hd').textContent = `claude化身 (${FRIEND_LIST.length})`;
  box.innerHTML = FRIEND_LIST.map((f, i) =>
    `<div class="friend${TASKS.some(t => (t.agent_name || '') === f.name && UNREAD.has(t.id)) ? ' notify' : ''}" data-friend="${i}">
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
function friendTask(f) {
  // 这个好友已有的会话（按 agent_name 匹配，取最近更新的一条）
  return TASKS.filter(t => (t.agent_name || '') === f.name)
              .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))[0] || null;
}
const _friendChatInflight = new Set();   // 正在为哪些好友新建会话（防连点重复创建）
async function startFriendChat(f) {
  if (!f) return;
  // 上次有会话就继续原来的，不新开
  const existing = friendTask(f);
  if (existing) { openTask(existing.id); return; }
  // 连续点击时，第一次的创建请求还没返回、TASKS 里还没有该会话，
  // 若不拦住，后续点击也会判定“无会话”而重复新建 → 左边出现两条。用 in-flight 锁挡住。
  if (_friendChatInflight.has(f.name)) return;
  _friendChatInflight.add(f.name);
  try {
    const proj = f.project || (CONFIG.projects[0] || {}).name || '';
    const prompt = `${f.persona}\n\n请用上面的人设，跟我打个招呼、简短开场（别超过三句）。之后一直保持这个人设跟我聊。`;
    const res = await fetch('/api/tasks', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ title: `💬 ${f.name}`, project: proj, model: f.model || CONFIG.default_model, permission_mode: uiPermMode || CONFIG.default_permission_mode, prompt,
        agent_name: f.name, agent_avatar: f.avatar }) });
    const task = await res.json();
    if (task.error) { alert(task.error); return; }
    TASKS.unshift(task);
    openTask(task.id);
  } finally {
    _friendChatInflight.delete(f.name);
  }
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
/* 模型列表：config.json 里既可以写 "sonnet" 这种字符串，
   也可以写 {id:"claude-opus-5", label:"Opus 5"}（下拉显示 label，实际传 id） */
function modelOptions() {
  return (CONFIG.models || ['sonnet']).map(m =>
    typeof m === 'string' ? { id: m, label: m } : { id: m.id, label: m.label || m.id });
}
function fillModelSelect(sel) {
  sel.innerHTML = '';
  for (const m of modelOptions()) {
    const o = document.createElement('option');
    o.value = m.id; o.textContent = m.label;
    if (m.id === CONFIG.default_model) o.selected = true;
    sel.appendChild(o);
  }
}
/* 聊天顶栏的模型下拉：显示当前会话在用的模型，选中即切换 */
function syncChatModel(model) {
  const sel = $('#chat-model');
  if (!sel) return;
  fillModelSelect(sel);
  sel.classList.toggle('show', !!currentTaskId);
  if (!model) return;
  // CLI 实际回报的模型名可能不在配置列表里（别名解析成了具体版本），补一个选项
  if (![...sel.options].some(o => o.value === model)) {
    const o = document.createElement('option');
    o.value = model; o.textContent = model;
    sel.appendChild(o);
  }
  sel.value = model;
}
$('#chat-model').addEventListener('change', async e => {
  const model = e.target.value;
  if (!currentTaskId) return;
  const res = await fetch(`/api/tasks/${currentTaskId}/model`, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ model }) });
  const t = await res.json();
  if (t.error) { alert(t.error); return; }
  const local = TASKS.find(x => x.id === currentTaskId);
  if (local) local.model = model;
  toast(`已切到 ${e.target.selectedOptions[0].textContent}`);
});
