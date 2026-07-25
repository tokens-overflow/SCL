/* ============================================================
 * Q-CC 前端 · shell.js
 * 职责：CLAUDE.md 编辑 + 各弹窗/按钮的 onclick/onchange 绑定 + 原生窗口(最小化/最大化/拖动) + 表情面板。
 * 说明：本文件是原 app.js 的一段(按功能拆分，逻辑未改)。
 *       经典 <script> 顶层声明共享同一全局作用域，靠 index.html
 *       的引入顺序保证依赖可见——请勿单独调整加载顺序。
 * ============================================================ */
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
  // 最大化/復元はプラットフォーム差(mac の座標系ずれ・Win の DPI)を後端に集約。
  // 後端 maximize() が Win/Linux=ネイティブ、mac=作業領域計算 を出し分ける。
  const a = pwApi(); if (!a) return;
  await a.maximize();
  winMaximized = true; $('#win-max').classList.add('restore');
}
async function restoreWin() {
  const a = pwApi(); if (!a) return;
  await a.restore();
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

