/* ============================================================
 * Q-CC 前端 · views.js
 * 职责：视图切换 + My Zone 动态 + 小游戏 + skill 管理 + 插件/能力面板 + 定时任务。
 * 说明：本文件是原 app.js 的一段(按功能拆分，逻辑未改)。
 *       经典 <script> 顶层声明共享同一全局作用域，靠 index.html
 *       的引入顺序保证依赖可见——请勿单独调整加载顺序。
 * ============================================================ */
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

