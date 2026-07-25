/* ============================================================
 * Q-CC 前端 · netchat.js —— 网络好友（互联网真人聊天，GitHub 中转）
 * 职责：右栏「网络好友」区块 + ⚙设置弹窗 + 真人聊天收发。
 * 说明：纯新增、自初始化，不改任何现有文件。复用 core/chat/friends 的
 *       $ / esc / mdInline / fmtTime / avatarHtml / msgContainer / scrollBottom /
 *       showView / notifyDing，以及全局 es / currentTaskId（打开网络会话时需要
 *       关掉 AI 的 SSE、清空 currentTaskId，避免两种消息混渲染）。
 *       发送靠 document 捕获阶段拦截 #send-btn 点击 / #prompt 的 Ctrl+Enter：
 *       仅当处于网络会话(NET.active)时接管，否则一律放行给 AI 逻辑。
 * ============================================================ */
(() => {
  // handle = 我的 Q-CC ID（唯一身份/路由键）；nickname = 显示名（可改，广播给对方）
  const NET = { active: false, peer: null, handle: '', nickname: '', avatar: '🧑', sign: '', configured: false, es: null, peerMeta: {} };
  const peerAv = (h) => (NET.peerMeta[h] && NET.peerMeta[h].av) || '🧑';
  const peerSign = (h) => (NET.peerMeta[h] && NET.peerMeta[h].sign) || '';
  const peerNick = (h) => (NET.peerMeta[h] && NET.peerMeta[h].nick) || h;   // 没昵称就用 ID
  const NET_UNREAD = new Set();   // 已到但没点开看的网络好友（图标晃动 + 滴滴滴）

  const box = () => $('#net-friends');

  /* -------- 状态 & 好友列表 -------- */
  async function refreshState() {
    try {
      const s = await (await fetch('/api/net/state')).json();
      NET.configured = !!s.configured;
      NET.handle = s.handle || '';
      NET.nickname = s.nickname || s.handle || '';
      NET.avatar = s.avatar || '🧑';
      NET.sign = s.sign || '';
      NET.friends = s.friends || [];
      NET.peerMeta = s.peer_meta || {};
      _owner = s.owner || ''; _repo = s.repo || '';
    } catch (_) { NET.configured = false; NET.friends = []; }
    renderNetFriends();
    if (NET.configured && !NET.es) startSSE();
  }
  function renderNetFriends() {
    const el = box(); if (!el) return;
    if (!NET.configured) {
      el.innerHTML = '<div style="padding:8px 10px;color:#8398b0;font-size:13px">未连接。点右上角 ⚙ 填 GitHub 仓库 + PAT + 昵称。</div>';
      return;
    }
    let html = `<div id="net-me" title="点我编辑我的资料" style="display:flex;align-items:center;gap:6px;padding:5px 10px;cursor:pointer;color:#5a7699;font-size:12px;border-bottom:1px solid #e3ebf5">
        <span class="f-ava" style="width:24px;height:24px;font-size:16px">${avatarHtml(NET.avatar)}</span>
        <span style="flex:1;min-width:0">我：<b>${esc(NET.nickname || NET.handle)}</b> <span style="color:#8aa">(ID:${esc(NET.handle)})</span>${NET.sign ? '<br>' + esc(NET.sign) : ''}</span>
      </div>`;
    html += (NET.friends || []).map(h =>
      `<div class="friend${NET_UNREAD.has(h) ? ' notify' : ''}" data-netpeer="${esc(h)}">
         <div class="f-ava">${avatarHtml(peerAv(h))}</div>
         <div style="flex:1;min-width:0"><div class="f-name">${esc(peerNick(h))}</div>
         <div class="f-sign">${esc(peerSign(h) || ('ID: ' + h))}</div></div>
         <span class="f-del" data-netdel="${esc(h)}" title="删除真人好友">✕</span>
       </div>`).join('');
    html += `<div class="friend" id="net-add">
        <div class="f-ava">➕</div>
        <div style="flex:1;min-width:0"><div class="f-name">加真人好友</div>
        <div class="f-sign">输入对方的 Q-CC ID</div></div>
      </div>`;
    el.innerHTML = html;
    renderNetChats();
  }

  // 左侧「真人聊天」列表（和 AI 聊天信息并列）；点开=进会话，✕=删该会话聊天记录
  function renderNetChats() {
    const el = $('#net-chats'); if (!el) return;
    if (!NET.configured || !(NET.friends || []).length) { el.innerHTML = ''; return; }
    el.innerHTML = (NET.friends || []).map(h =>
      `<div class="task-item${NET_UNREAD.has(h) ? ' notify' : ''}" data-netpeer="${esc(h)}" title="${esc(peerNick(h))}">
         <span class="t-ava">${avatarHtml(peerAv(h))}</span>
         <span class="t-dot idle"></span>
         <span class="t-title">${esc(peerNick(h))}</span>
         <span class="t-acts"><span class="t-del" data-netclear="${esc(h)}" title="删除聊天记录">✕</span></span>
       </div>`).join('');
  }

  /* -------- 打开某个网络会话 -------- */
  async function openNetChat(peer) {
    NET.active = true; NET.peer = peer;
    NET_UNREAD.delete(peer);
    fetch('/api/net/ping', { method: 'POST' }).catch(() => {});   // 打开会话→后端进入快轮询，回复更快到
    // 关掉 AI 的会话流，避免混渲染
    if (typeof es !== 'undefined' && es) { try { es.close(); } catch (_) {} es = null; }
    currentTaskId = null;
    showView('chat');
    document.body.classList.add('netchat-mode');   // 真人聊天：隐藏权限/附加目录/命令/停止等 AI 专用按钮
    $('#chat-title').textContent = `💬 ${peerNick(peer)}`;
    $('#chat-avatar').innerHTML = avatarHtml(peerAv(peer));
    $('#chat-meta').textContent = (peerSign(peer) ? peerSign(peer) + ' · ' : '') + `ID:${peer} · 私密中转·阅后即焚`;
    $('#stop-btn').classList.remove('show');
    const m = msgContainer(); m.innerHTML = '';
    try {
      const hist = await (await fetch(`/api/net/history?peer=${encodeURIComponent(peer)}`)).json();
      for (const rec of hist) renderNetMsg(rec, false);
    } catch (_) {}
    scrollBottom();
    renderNetFriends();
  }

  function renderNetMsg(rec, scroll = true) {
    const mine = rec.dir === 'out';
    const div = document.createElement('div');
    div.className = 'msg ' + (mine ? 'user' : 'agent');
    const who = mine ? (NET.nickname || NET.handle || '我')
                     : (rec.nick || peerNick(rec.from || NET.peer));
    // 我的头像用 NET.avatar；对方的用消息里带的 av，退回缓存/默认
    const ava = mine ? (NET.avatar || '🧑') : (rec.av || peerAv(rec.from || NET.peer));
    const t = rec.ts ? fmtTime(rec.ts / 1000) : '';
    div.innerHTML =
      `<div class="m-head"><span class="mh-ava">${avatarHtml(ava)}</span>${esc(who)} ${t}</div>` +
      `<div class="m-body">${mdInline(rec.text || '')}</div>`;
    msgContainer().appendChild(div);
    if (scroll) scrollBottom();
  }

  /* -------- 发送（被 document 捕获拦截后调用） -------- */
  async function sendFromInput() {
    const p = $('#prompt');
    const text = (p.value || '').trim();
    if (!text || !NET.peer) return;
    p.value = '';
    try {
      const r = await (await fetch('/api/net/send', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to: NET.peer, text }),
      })).json();
      if (r && r.error) alert('发送失败：' + r.error);
      // 本地回显由后端经 SSE(net-msg dir=out) 推回渲染，这里不重复插入
    } catch (e) { alert('发送失败：' + e); }
  }

  /* -------- 实时事件（SSE） -------- */
  function startSSE() {
    NET.es = new EventSource('/api/net/events');
    NET.es.onmessage = (e) => {
      let ev; try { ev = JSON.parse(e.data); } catch (_) { return; }
      if (ev.type === 'net-msg') {
        const peer = ev.peer;
        if (ev.msg && ev.msg.dir !== 'out') {   // 记住对方头像 + 个签 + 昵称
          NET.peerMeta[peer] = { av: ev.msg.av || peerAv(peer), sign: ev.msg.sign || peerSign(peer), nick: ev.msg.nick || peerNick(peer) };
        }
        if (NET.active && NET.peer === peer) {
          renderNetMsg(ev.msg);
        } else if (ev.msg && ev.msg.dir !== 'out') {
          // 别的网络好友来消息、且没在看 → 晃动 + 滴滴滴
          NET_UNREAD.add(peer);
          renderNetFriends();
          if (typeof notifyDing === 'function') notifyDing();
        }
        if (!(NET.friends || []).includes(peer)) refreshState();
      } else if (ev.type === 'net-friends') {
        refreshState();
      } else if (ev.type === 'net-cleared') {
        if (NET.active && NET.peer === ev.peer) msgContainer().innerHTML = '';
      } else if (ev.type === 'net-error') {
        const m = msgContainer();
        if (NET.active) { const d = document.createElement('div'); d.className = 'sys-line'; d.textContent = '⚠ ' + (ev.text || '网络错误'); m.appendChild(d); scrollBottom(); }
      }
    };
    NET.es.onerror = () => { /* 浏览器自动重连 */ };
  }
  // 未读提醒：还有没点开的网络消息就每隔一会再“滴滴滴”，直到点开
  setInterval(() => { if (NET_UNREAD.size > 0 && typeof notifyDing === 'function') notifyDing(); }, 10000);

  /* -------- 设置弹窗 -------- */
  function openSetup() {
    $('#net-repo').value = (NET.configured && (_owner || _repo)) ? `${netOwner()}/${netRepo()}` : '';
    $('#net-handle').value = NET.handle || '';
    $('#net-nickname').value = NET.nickname || '';
    $('#net-sign').value = NET.sign || '';
    $('#net-token').value = '';
    // 用和“添加好友”一样的头像网格；当前若是 emoji 就填进 emoji 框，否则网格里选中它
    const isEmoji = NET.avatar && !avatarSvg(NET.avatar) && !String(NET.avatar).startsWith('img:');
    $('#net-avatar').value = isEmoji ? NET.avatar : '';
    buildAvatarGrid($('#net-avatar-grid'), isEmoji ? undefined : NET.avatar);
    $('#net-mask').classList.add('show');
    $('#net-repo').focus();
  }
  // owner/repo 只在 state 里分别存了，这里从 state 拼；简单起见重新拉一次不必要，用缓存
  let _owner = '', _repo = '';
  function netOwner() { return _owner; }
  function netRepo() { return _repo; }
  function closeSetup() { $('#net-mask').classList.remove('show'); }
  async function saveSetup() {
    const repoFull = ($('#net-repo').value || '').trim();
    const token = ($('#net-token').value || '').trim();   // 留空=沿用已存 PAT（只改资料）
    const handle = ($('#net-handle').value || '').trim();
    const nickname = ($('#net-nickname').value || '').trim();
    const sign = ($('#net-sign').value || '').trim();
    const avatar = pickedAvatar($('#net-avatar-grid'), $('#net-avatar'));   // 复用本地头像网格
    const m = repoFull.match(/^([^/\s]+)\/([^/\s]+)$/);
    if (!m) { alert('仓库格式应为 owner/repo，例如 你的GitHub用户名/qcc-chat'); return; }
    // Q-CC ID 规整成安全字符（和后端一致）
    const safeHandle = handle.replace(/[^A-Za-z0-9_.\-]/g, '').replace(/^[._-]+|[._-]+$/g, '');
    if (!safeHandle) { alert('Q-CC ID 只能用 字母/数字/_/-（昵称/头像/个签 可随意）'); return; }
    if (!token && !NET.configured) { alert('首次连接要填 GitHub PAT'); return; }
    // 昵称随便改，不影响身份；只有改 Q-CC ID(身份) 才警告
    if (NET.configured && NET.handle && safeHandle !== NET.handle) {
      if (!confirm(`要把你的 Q-CC ID 从「${NET.handle}」改成「${safeHandle}」吗？\nID 是你的唯一身份，改了以后别人要用新 ID 才能找到你，发给旧 ID 的消息你将收不到。\n（只想改显示名的话，改“昵称”即可，不用动 ID）`)) return;
    }
    const r = await (await fetch('/api/net/setup', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ owner: m[1], repo: m[2], token, handle, nickname, avatar, sign }),
    })).json();
    if (!r.ok) { alert('连接失败：' + (r.error || '未知错误')); return; }
    _owner = m[1]; _repo = m[2];
    closeSetup();
    refreshState();
  }

  /* -------- 事件绑定 -------- */
  document.addEventListener('DOMContentLoaded', bind);
  if (document.readyState !== 'loading') bind();
  function bind() {
    const gear = $('#net-gear'); if (gear && !gear._bound) { gear._bound = 1; gear.onclick = (e) => { e.stopPropagation(); openSetup(); }; }
    const save = $('#net-save'); if (save && !save._bound) { save._bound = 1; save.onclick = saveSetup; }
    const c1 = $('#net-close'); if (c1) c1.onclick = closeSetup;
    const c2 = $('#net-cancel'); if (c2) c2.onclick = closeSetup;
    const el = box(); if (el && !el._bound) {
      el._bound = 1;
      el.addEventListener('click', (e) => {
        if (e.target.closest('#net-me')) { openSetup(); return; }   // 点“我”这行=编辑自己资料
        const del = e.target.closest('[data-netdel]');
        if (del) {
          e.stopPropagation();
          const h = del.dataset.netdel;
          if (confirm(`删除网络好友「${h}」？（本地聊天记录保留，可重新加回）`)) {
            fetch('/api/net/delfriend', { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ handle: h }) }).then(() => refreshState());
          }
          return;
        }
        if (e.target.closest('#net-add')) {
          const h = prompt('输入对方的 Q-CC ID：');
          if (h && h.trim()) {
            fetch('/api/net/addfriend', { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ handle: h.trim() }) }).then(() => refreshState());
          }
          return;
        }
        const row = e.target.closest('[data-netpeer]');
        if (row) openNetChat(row.dataset.netpeer);
      });
    }
    const nc = $('#net-chats'); if (nc && !nc._bound) {
      nc._bound = 1;
      nc.addEventListener('click', (e) => {
        const clr = e.target.closest('[data-netclear]');
        if (clr) {
          e.stopPropagation();
          const h = clr.dataset.netclear;
          if (confirm(`删除与「${peerNick(h)}」的聊天记录？（只删你本地，对方不受影响）`)) {
            fetch('/api/net/clearhistory', { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ peer: h }) }).then(() => { if (NET.active && NET.peer === h) msgContainer().innerHTML = ''; });
          }
          return;
        }
        const row = e.target.closest('[data-netpeer]');
        if (row) openNetChat(row.dataset.netpeer);
      });
    }
    refreshState();
  }

  // 发送拦截：捕获阶段在 AI 处理之前判断；仅网络会话时接管
  document.addEventListener('click', (e) => {
    if (NET.active && e.target.closest('#send-btn')) {
      e.stopPropagation(); e.preventDefault(); sendFromInput();
    }
  }, true);
  document.addEventListener('keydown', (e) => {
    if (NET.active && e.target && e.target.id === 'prompt' && (e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.stopPropagation(); e.preventDefault(); sendFromInput();
    }
  }, true);
  // 切回 AI 会话（点任务/AI好友）时退出网络模式，把输入框还给 AI 逻辑
  document.addEventListener('click', (e) => {
    if (e.target.closest('[data-openchat]') || e.target.closest('[data-friend]')) {
      NET.active = false;
      document.body.classList.remove('netchat-mode');   // 切回 claude 会话：恢复权限/附加/命令等按钮
    }
  }, true);
})();
