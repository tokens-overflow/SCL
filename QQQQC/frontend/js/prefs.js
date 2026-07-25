/* ============================================================
 * Q-CC 前端 · prefs.js
 * 职责：字体+字号切换 + 权限模式切换 + 时钟 + 任务状态轮询(5s)。
 * 说明：本文件是原 app.js 的一段(按功能拆分，逻辑未改)。
 *       经典 <script> 顶层声明共享同一全局作用域，靠 index.html
 *       的引入顺序保证依赖可见——请勿单独调整加载顺序。
 * ============================================================ */
/* ================= 字体切换 ================= */
/* 多套字体：CSS 变量 --app-font 由此处切换，localStorage 记忆选择。
   每套都带 CJK 回退，保证中日文都能显示；等宽/衬线各留一套换口味。 */
const FONTS = [
  { id:'classic', name:'经典（默认）', stack:'Tahoma, "宋体", SimSun, "Microsoft YaHei", sans-serif' },
  { id:'yahei',   name:'圆润雅黑',     stack:'"Microsoft YaHei UI","Microsoft YaHei","Yu Gothic UI","Meiryo",sans-serif' },
  { id:'yugo',    name:'日系游黑',     stack:'"Yu Gothic UI","Yu Gothic","Meiryo","Microsoft YaHei",sans-serif' },
  { id:'meiryo',  name:'柔和メイリオ', stack:'"Meiryo","Meiryo UI","Yu Gothic UI","Microsoft YaHei",sans-serif' },
  { id:'kai',     name:'楷书手写',     stack:'"KaiTi","STKaiti","楷体","BIZ UDMincho","Yu Mincho",serif' },
  { id:'serif',   name:'优雅衬线',     stack:'"Yu Mincho","游明朝","MS PMincho","SimSun",Georgia,serif' },
  { id:'mono',    name:'极客等宽',     stack:'"Cascadia Code","Consolas","Yu Gothic UI",monospace' },
];
const FONT_KEY = 'qqqqc_font';
const fontPanel = $('#font-panel');
function applyFont(id) {
  const f = FONTS.find(x => x.id === id) || FONTS[0];
  document.documentElement.style.setProperty('--app-font', f.stack);
  try { localStorage.setItem(FONT_KEY, f.id); } catch (_) {}
  fontPanel.querySelectorAll('.fp-item').forEach(el => {
    const on = el.dataset.font === f.id;
    el.classList.toggle('active', on);
    el.querySelector('.fp-check').textContent = on ? '✓' : '';
  });
}
/* 字号：整体缩放(zoom)，在当前基础上 -2 ~ +2 档浮动，localStorage 记忆。
   用 zoom 是因为界面大量使用 px，zoom 能等比放大/缩小整套 UI（相当于浏览器 Ctrl+±）。 */
const FONT_SIZES = [
  { id:'-2', label:'特小', zoom:0.86 },
  { id:'-1', label:'小',   zoom:0.93 },
  { id:'0',  label:'标准', zoom:1.0 },
  { id:'1',  label:'大',   zoom:1.07 },
  { id:'2',  label:'特大', zoom:1.14 },
];
const FSIZE_KEY = 'qqqqc_fontsize';
function applyFontSize(id) {
  const s = FONT_SIZES.find(x => x.id === id) || FONT_SIZES[2];
  document.documentElement.style.zoom = s.zoom === 1 ? '' : String(s.zoom);
  try { localStorage.setItem(FSIZE_KEY, s.id); } catch (_) {}
  fontPanel.querySelectorAll('.fs-btn').forEach(el => el.classList.toggle('active', el.dataset.fsize === s.id));
}
(() => {   // 面板顶部：字号档位 + 「字体」小标题
  const row = document.createElement('div');
  row.className = 'fp-size';
  row.innerHTML = '<span class="fp-sec">字号</span>' +
    FONT_SIZES.map(s => `<button class="fs-btn" data-fsize="${s.id}">${s.label}</button>`).join('');
  fontPanel.appendChild(row);
  row.querySelectorAll('.fs-btn').forEach(b => { b.onclick = () => applyFontSize(b.dataset.fsize); });
  const hd = document.createElement('div');
  hd.className = 'fp-sec'; hd.textContent = '字体';
  fontPanel.appendChild(hd);
})();
FONTS.forEach(f => {
  const it = document.createElement('div');
  it.className = 'fp-item';
  it.dataset.font = f.id;
  it.style.fontFamily = f.stack;               // 每行用自身字体预览
  it.innerHTML = `<span class="fp-check"></span><span class="fp-name">${f.name}</span><span class="fp-eg">Aa 你好</span>`;
  it.onclick = () => { applyFont(f.id); fontPanel.classList.remove('show'); };
  fontPanel.appendChild(it);
});
function toggleFontPanel(btn) {
  const r = btn.getBoundingClientRect();
  fontPanel.style.left = Math.max(6, Math.min(r.left, window.innerWidth - 212)) + 'px';
  fontPanel.style.top = (r.bottom + 4) + 'px';
  fontPanel.classList.toggle('show');
}
// 面板外点击关闭（点字体按钮本身不关，交给 toggle）
document.addEventListener('click', e => {
  if (fontPanel.classList.contains('show')
      && !e.target.closest('#font-panel')
      && !e.target.closest('[data-act="font"]')) {
    fontPanel.classList.remove('show');
  }
});
applyFont((() => { try { return localStorage.getItem(FONT_KEY); } catch (_) { return null; } })() || 'classic');
applyFontSize((() => { try { return localStorage.getItem(FSIZE_KEY); } catch (_) { return null; } })() || '0');

/* ================= 权限模式切换（类似 Claude Code 的 plan / auto） ================= */
/* 权限模式是 claude 启动时的 --permission-mode。开着任务时切换会让后端重启该会话进程，
   下条消息起用新模式；没开任务时只记住选择，供下一次「新建任务/开聊」使用。 */
const PERM_MODES = [
  { id:'default',           ico:'🛡', short:'默认',   name:'default',           desc:'每步动作前询问' },
  { id:'plan',              ico:'📋', short:'计划',   name:'plan',              desc:'只做规划，不改文件' },
  { id:'acceptEdits',       ico:'✏️', short:'自动',   name:'acceptEdits',       desc:'自动接受文件编辑' },
  { id:'bypassPermissions', ico:'🚀', short:'全自动', name:'bypassPermissions', desc:'跳过所有确认，慎用' },
];
let uiPermMode = 'acceptEdits';   // 未开任务时的默认；开任务后按钮跟随该任务
const permPanel = $('#perm-panel');
const permBtn = $('#perm-btn');
function permMeta(id) { return PERM_MODES.find(m => m.id === id) || PERM_MODES[0]; }
function syncPermButton(id) {
  const m = permMeta(id);
  permBtn.innerHTML = `${m.ico} ${m.short} ▾`;
  permPanel.querySelectorAll('.pm-item').forEach(el => {
    const on = el.dataset.perm === m.id;
    el.classList.toggle('active', on);
    el.querySelector('.pm-check').textContent = on ? '✓' : '';
  });
}
PERM_MODES.forEach(m => {
  const it = document.createElement('div');
  it.className = 'pm-item'; it.dataset.perm = m.id;
  it.innerHTML = `<span class="pm-check"></span><span class="pm-ico">${m.ico}</span>`
    + `<span class="pm-name">${m.short}</span><span class="pm-desc">${m.name} · ${m.desc}</span>`;
  it.onclick = () => { choosePerm(m.id); permPanel.classList.remove('show'); };
  permPanel.appendChild(it);
});
async function choosePerm(id) {
  const t = currentTask();
  if (currentTaskId && t) {
    try {
      const res = await fetch(`/api/tasks/${currentTaskId}/permission`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ permission_mode: id }) });
      const nt = await res.json();
      if (nt.error) { alert(nt.error); return; }
      t.permission_mode = id;
    } catch (e) { alert('切换权限失败：' + e); return; }
  } else {
    uiPermMode = id;
  }
  syncPermButton(id);
}
permBtn.onclick = () => {
  const willShow = !permPanel.classList.contains('show');
  permPanel.classList.toggle('show', willShow);
  if (!willShow) return;
  const r = permBtn.getBoundingClientRect();     // 面板在按钮上方弹出（输入栏靠底部）
  permPanel.style.left = Math.max(6, Math.min(r.left, window.innerWidth - 256)) + 'px';
  permPanel.style.top = Math.max(6, r.top - permPanel.offsetHeight - 6) + 'px';
};
document.addEventListener('click', e => {
  if (permPanel.classList.contains('show')
      && !e.target.closest('#perm-panel') && !e.target.closest('#perm-btn')) {
    permPanel.classList.remove('show');
  }
});
syncPermButton(uiPermMode);

/* 时钟 */
setInterval(() => {
  const d = new Date();
  $('#clock').textContent = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
}, 1000);

/* 轮询任务状态（兜底，SSE 已覆盖大部分） */
setInterval(async () => {
  if (document.hidden) return;
  const fresh = await (await fetch('/api/tasks')).json();
  let newlyUnread = false;   // 本轮是否有会话“回复中→完成”而新标记未读
  for (const f of fresh) {
    const t = TASKS.find(x => x.id === f.id);
    if (t && t.status !== f.status) {
      const wasRunning = t.status === 'running';
      t.status = f.status;
      // 别的会话「回复中→完成」时，标记未读让它的图标晃动（点开看了在 openTask 里清除）
      if (wasRunning && f.id !== currentTaskId && (f.status === 'idle' || f.status === 'error')) {
        UNREAD.add(f.id);
        newlyUnread = true;
      }
      renderTaskList(''); renderFriends();   // 任务列表 + 好友列表都刷新（好友头像可能要开始晃）
    }
    if (!t) TASKS.unshift(f);
  }
  if (newlyUnread) notifyDing();   // 图标开始晃动的同时，来一段“滴滴滴”
}, 5000);

/* 未读提醒：只要还有“回复完成但没点开看”的会话(UNREAD 非空)，就每隔一会“滴滴滴”一次，
   一直催到你点开看了(openTask 里清 UNREAD)为止；全部看完就自动安静。 */
setInterval(() => { if (UNREAD.size > 0) notifyDing(); }, 10000);

