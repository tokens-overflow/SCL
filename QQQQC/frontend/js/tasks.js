/* ============================================================
 * Q-CC 前端 · tasks.js
 * 职责：任务列表 / SSE 会话流 / openTask / 发送消息 + 新建任务 + 全局点击总线(document click 分发) + 顶部按钮绑定。
 * 说明：本文件是原 app.js 的一段(按功能拆分，逻辑未改)。
 *       经典 <script> 顶层声明共享同一全局作用域，靠 index.html
 *       的引入顺序保证依赖可见——请勿单独调整加载顺序。
 * ============================================================ */
async function loadTasks() {
  TASKS = await (await fetch('/api/tasks')).json();
  renderTaskList();
}
function taskItemHtml(t) {
  return `<div class="task-item${t.id === currentTaskId ? ' active' : ''}${UNREAD.has(t.id) ? ' notify' : ''}" data-openchat="${t.id}" title="${esc(t.title)}">
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
  UNREAD.delete(id);   // 点开看了就不晃了
  renderFriends();     // 该好友头像停止晃动
  const t = TASKS.find(x => x.id === id);
  // 当前对话的对方身份:优先任务自带的 agent，其次从标题里取名字
  currentAgent = {
    name: (t && t.agent_name) || (t ? (t.title || '').replace(/^💬\s*/, '') : '') || 'Claude',
    avatar: (t && t.agent_avatar) || 'qq1',
  };
  $('#chat-avatar').innerHTML = avatarHtml(currentAgent.avatar);
  $('#chat-title').textContent = t ? t.title : '';
  $('#win-title').textContent = `Q-CC - ${t ? t.title : ''}`;
  syncChatModel(t && t.model);
  $('#chat-meta').textContent = '';
  syncPermButton((t && t.permission_mode) || uiPermMode);   // 权限按钮跟随当前会话
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
let PICKED_CWD = '';   // 「📁 浏览…」选中的自定义工作目录（留空=用项目下拉）
function renderPickedCwd() {
  $('#f-cwd-row').style.display = PICKED_CWD ? '' : 'none';
  $('#f-cwd').textContent = PICKED_CWD;
  $('#f-project').disabled = !!PICKED_CWD;
}
/* 鼠标选目录：原生窗口用系统对话框，浏览器兜底手输 */
async function pickFolder() {
  const a = (window.pywebview && window.pywebview.api) || null;
  if (a && a.pick_folder) return (await Promise.resolve(a.pick_folder())) || '';
  return (prompt('浏览器模式下没有系统对话框，请粘贴目录绝对路径（支持 ~）：') || '').trim();
}
$('#f-browse').addEventListener('click', async () => {
  const dir = await pickFolder();
  if (dir) { PICKED_CWD = dir; renderPickedCwd(); }
});
$('#f-cwd-clear').addEventListener('click', () => { PICKED_CWD = ''; renderPickedCwd(); });

function openNewTaskModal(projectName, prefillPrompt) {
  $('#modal-mask').classList.add('show');
  if (projectName) $('#f-project').value = projectName;
  if (prefillPrompt) $('#f-prompt').value = prefillPrompt;
  $('#f-perm').value = uiPermMode;   // 用输入栏权限按钮选定的模式作为新任务默认
  $('#f-prompt').focus();
}
function closeModal() { $('#modal-mask').classList.remove('show'); }

async function createTask() {
  const prompt = $('#f-prompt').value.trim();
  if (!prompt) { $('#f-prompt').focus(); return; }
  const body = {
    title: $('#f-title').value.trim(),
    project: $('#f-project').value,
    cwd_path: PICKED_CWD,          // 有值则以它为本次会话的工作目录
    model: $('#f-model').value,
    permission_mode: $('#f-perm').value,
    prompt,
  };
  const res = await fetch('/api/tasks', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  const task = await res.json();
  if (task.error) { alert(task.error); return; }
  closeModal();
  $('#f-title').value = ''; $('#f-prompt').value = '';
  PICKED_CWD = ''; renderPickedCwd();
  TASKS.unshift(task);
  openTask(task.id);
}

let sending = false;
let PENDING_IMAGES = [];   // 待发送的粘贴图片 {media_type, data(base64), url}
function renderPasteStrip() {
  const strip = $('#paste-strip');
  strip.classList.toggle('show', PENDING_IMAGES.length > 0);
  strip.innerHTML = PENDING_IMAGES.map((im, i) =>
    `<div class="paste-thumb"><img src="${im.url}" alt=""><span class="pt-del" data-delimg="${i}">✕</span></div>`).join('');
}
/* 直接把复制的图片粘贴到输入框 */
$('#prompt').addEventListener('paste', (e) => {
  const items = (e.clipboardData && e.clipboardData.items) || [];
  let added = false;
  for (const it of items) {
    if (it.kind === 'file' && it.type.startsWith('image/')) {
      const file = it.getAsFile(); if (!file) continue;
      added = true;
      const reader = new FileReader();
      reader.onload = () => {
        const url = String(reader.result);
        PENDING_IMAGES.push({ media_type: file.type || 'image/png', data: url.slice(url.indexOf(',') + 1), url });
        renderPasteStrip();
      };
      reader.readAsDataURL(file);
    }
  }
  if (added) e.preventDefault();   // 别把图片当二进制塞进文本框
});
$('#paste-strip').addEventListener('click', (e) => {
  const del = e.target.closest('[data-delimg]');
  if (del) { PENDING_IMAGES.splice(+del.dataset.delimg, 1); renderPasteStrip(); }
});
async function sendMessage() {
  if (sending) return;
  const text = $('#prompt').value.trim();
  const images = PENDING_IMAGES.map(im => ({ media_type: im.media_type, data: im.data }));
  if (!text && !images.length) return;
  if (!currentTaskId) {
    // 还没有打开任务：把这句话带进「新建任务」（图片会被清掉，先开一段对话再贴）
    openNewTaskModal(null, text);
    $('#prompt').value = '';
    return;
  }
  sending = true; $('#send-btn').disabled = true;
  try {
    const res = await fetch(`/api/tasks/${currentTaskId}/message`, {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ text, images })
    });
    const t = await res.json();
    if (t.error) { alert(t.error); return; }
    $('#prompt').value = '';
    PENDING_IMAGES = []; renderPasteStrip();
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
    else if (a === 'font') { toggleFontPanel(act); }
    else if (a === 'todo') { showView('chat'); addSysLine('🚧 该功能敬请期待（v2）'); }
    return;
  }
  // 权限确认卡片的按钮
  const pd = e.target.closest('[data-permdecide]');
  if (pd) {
    const card = pd.closest('.perm-card');
    const reqId = card && card.dataset.req;
    if (reqId && currentTaskId) {
      const bb = card.querySelector('.pc-btns'); if (bb) bb.style.pointerEvents = 'none';  // 乐观禁用，等 resolved 事件回来定稿
      fetch(`/api/tasks/${currentTaskId}/perm-decide`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ request_id: reqId, behavior: pd.dataset.permdecide, scope: pd.dataset.scope }),
      }).catch(() => { if (bb) bb.style.pointerEvents = ''; });
    }
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

