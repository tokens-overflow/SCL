/* ============================================================
 * Q-CC 前端 · panels.js
 * 职责：输入栏弹层：斜杠命令自动补全 + 附加目录(--add-dir)。
 * 说明：本文件是原 app.js 的一段(按功能拆分，逻辑未改)。
 *       经典 <script> 顶层声明共享同一全局作用域，靠 index.html
 *       的引入顺序保证依赖可见——请勿单独调整加载顺序。
 * ============================================================ */
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
    '<div class="ap-add">' +
      (window.pywebview ? '<button class="mini-btn" id="ap-browse-btn" title="从系统对话框选择文件夹">📁 浏览…</button>' : '') +
      '<input id="ap-input" placeholder="或输入/粘贴目录路径，支持 ~">' +
      '<button class="mini-btn" id="ap-add-btn">添加</button>' +
    '</div>';
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
  if (e.target.closest('#ap-browse-btn')) {
    // 原生窗口：弹系统「选择文件夹」对话框，选中即添加为可访问目录
    const a = (window.pywebview && window.pywebview.api) || null;
    if (!a || !a.pick_folder) { alert('浏览需在原生窗口模式下使用；请手动粘贴路径。'); return; }
    Promise.resolve(a.pick_folder()).then(p => { if (p) attachDirApi('adddir', p); });
    return;
  }
  if (e.target.closest('#ap-add-btn')) { const inp = $('#ap-input'); attachDirApi('adddir', inp.value.trim()); inp.value = ''; }
});
attachPop.addEventListener('keydown', (e) => {
  if (e.target.id === 'ap-input' && e.key === 'Enter') { e.preventDefault(); attachDirApi('adddir', e.target.value.trim()); e.target.value = ''; }
});

