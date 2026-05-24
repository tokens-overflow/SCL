/* =============================================================
   Werewolf · 12人神局  ——  Game Engine
   ============================================================= */
// TTS (Web Speech API) 模块已抽离到 web/tts.js，作为全局 const TTS 提供。
// 见 tts.js 顶部注释了解策略、可配置项（GENDERS / VOICE_PATTERNS / PITCH_PARAMS）
// 以及暴露的纯函数（_internals.genderOrder / computeParams）。

const ROLES_12 = [
  "wolf","wolf","wolf","wolf",
  "seer","witch","hunter","guard",
  "villager","villager","villager","villager",
];

const ROLE_META = {
  wolf:     { cn: "狼人",   emoji: "🐺", cls: "wolf"     },
  seer:     { cn: "预言家", emoji: "🔮", cls: "seer"     },
  witch:    { cn: "女巫",   emoji: "🧪", cls: "witch"    },
  hunter:   { cn: "猎人",   emoji: "🏹", cls: "hunter"   },
  guard:    { cn: "守卫",   emoji: "🛡️", cls: "guard"    },
  villager: { cn: "村民",   emoji: "👨‍🌾", cls: "villager" },
};

let GAME_GENERATION = 0;

class Game {
  constructor() {
    this.gen = ++GAME_GENERATION;     // 标识本局，防止旧局尾巴污染新局 UI
    this.agents = Array.from({ length: 12 }, (_, i) => new Agent(i));
    this.day = 0;
    this.phase = "idle";       // idle | night | day | vote | end
    this.tonightKill = null;   // 狼刀目标
    this.tonightSaved = false;
    this.tonightPoison = null;
    this.tonightProtected = null;
    this.publicCheckReports = []; // 公开的预言家报查
    this.speechHistory = [];      // [{day, phase, agentNo, publicRole, kind, text}] —— 让 LLM 看到他人说了什么
    this.roundSummaries = [];     // V2.9-2 每轮复盘：[{day, factual, inference}]
    this.history = [];
    this.events = [];             // 统一事件流（含 isPublic / visibleTo），见 web/events.js
    // 三层记忆中的"公共层"(规格 §2-3)
    // Moderator 唯一写入入口,所有 agent 只读,绝不写死因
    this.publicLog = (typeof MemoryLayers !== "undefined") ? new MemoryLayers.PublicLog() : null;
    this.running = false;
    this.paused = false;
    this.speedMs = 3500;
    this.revealAll = false;
    this.winner = null;
    this._timers = new Set();   // 本局所有 setTimeout id
    this.sheriffElectionDone = false;
    this.wolfHasExploded = false;
    this.sheriffIdx = -1;       // 当前警长 idx
  }

  isCurrent() { return this === currentGame && this.running; }

  cancel() {
    this.running = false;
    this._timers.forEach(id => clearTimeout(id));
    this._timers.clear();
    TTS.stop();
  }

  /* ============ 初始化 ============ */
  reset() {
    this.day = 0;
    this.phase = "idle";
    this.tonightKill = null;
    this.tonightSaved = false;
    this.tonightPoison = null;
    this.tonightProtected = null;
    this.publicCheckReports = [];
    this.speechHistory = [];
    this.roundSummaries = [];
    this.history = [];
    this.events = [];
    this.publicLog = (typeof MemoryLayers !== "undefined") ? new MemoryLayers.PublicLog() : null;
    this.winner = null;
    this.sheriffElectionDone = false;
    this.wolfHasExploded = false;
    this.sheriffIdx = -1;
    this.agents.forEach(a => a.reset());
    this.assignRoles();
  }

  assignRoles() {
    // Fisher-Yates 洗牌
    const shuffled = ROLES_12.slice();
    for (let i = shuffled.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
    }
    this.agents.forEach((a, i) => a.role = shuffled[i]);
    // 狼人互认队友(idx 用于内部数组,no 用于 LLM-facing 视角)
    const wolves = this.agents.filter(a => a.role === "wolf").map(a => a.idx);
    // 给 4 只狼随机分配 4 个战术角色(对齐 wolf_strategy 高级战术)
    const tactics = ["悍跳狼", "冲锋狼", "倒钩狼", "深水狼"];
    for (let i = tactics.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [tactics[i], tactics[j]] = [tactics[j], tactics[i]];
    }
    wolves.forEach((idx, k) => {
      this.agents[idx].wolfTactic = tactics[k];
    });
    this.agents.forEach(a => {
      if (a.role === "wolf") a.knownWolves = wolves.slice();
      // PrivateMemory: 同步 role + teammates(座位号 no, 排除自己)
      if (a.memory) {
        const teammates = a.role === "wolf"
          ? wolves.filter(idx => idx !== a.idx).map(idx => this.agents[idx].no)
          : [];
        a.memory.setRole(a.role, teammates);
      }
    });
  }

  aliveAgents() { return this.agents.filter(a => a.alive); }
  aliveOf(role) { return this.aliveAgents().filter(a => a.role === role); }

  /* ============ 事件广播 ============ */
  fireEvent(evt) {
    this.agents.forEach(a => a.observe(evt, this));
    if (evt.type === "check-report") {
      this.publicCheckReports.push(evt);
      this.emit(new Events.SeerReportEvent(
        this.day, this.agents[evt.from].no, this.agents[evt.target].no, evt.result
      ));
      this.appendPublic({
        type: "seer-report", day: this.day, phase: "day",
        fromNo: this.agents[evt.from].no, targetNo: this.agents[evt.target].no,
        result: evt.result,    // wolf | good —— 是预言家主动跳出来报的,所以允许在公共日志
      });
    } else if (evt.type === "claim") {
      // 跳身份是公开行为(用 claimedRole 字段避免与"真实 role"歧义)
      this.appendPublic({
        type: "claim", day: this.day, phase: "day",
        fromNo: this.agents[evt.from].no, claimedRole: evt.role,
      });
    }
  }

  // 推入统一事件流（events.js Event 子类实例）
  emit(evt) { this.events.push(evt); return evt; }

  // 返回某 agent 视角可见的事件
  eventsForAgent(agent) {
    return this.events.filter(e =>
      typeof e.visibleTo === "function" ? e.visibleTo(agent.role, agent.idx) : true
    );
  }

  // PublicLog 唯一写入入口(Moderator 角色)。entry 必须只含公开信息,
  // 严禁包含 cause/reason 等死因或私密字段(PublicLog.append 会防御性 throw)
  appendPublic(entry) {
    if (!this.publicLog) return;
    try {
      this.publicLog.append(entry);
    } catch (e) {
      // 严重违规:把开发者吼醒,但不让游戏崩
      console.error("[PublicLog VIOLATION]", e.message, entry);
    }
  }

  // 给某 agent 的 PrivateMemory 追加私密事实(seer-check / wolf-kill / 自己用药等)
  appendFactTo(agentIdx, fact) {
    const a = this.agents[agentIdx];
    if (a && a.memory) a.memory.appendFact(fact);
  }

  /* ============ 异步流程控制 ============ */
  wait(ms) {
    return new Promise(resolve => {
      const t = ms ?? this.speedMs;
      const start = performance.now();
      const tick = () => {
        if (!this.isCurrent()) { resolve(); return; }
        if (this.paused) { requestAnimationFrame(tick); return; }
        if (performance.now() - start >= t) { resolve(); return; }
        requestAnimationFrame(tick);
      };
      tick();
    });
  }

  // 受本局生命周期管理的 setTimeout，重开时自动取消
  later(fn, ms) {
    const id = setTimeout(() => {
      this._timers.delete(id);
      if (this === currentGame) fn();
    }, ms);
    this._timers.add(id);
    return id;
  }

  /* ============ 主流程 ============ */
  async run() {
    this.running = true;
    this._startedAt = new Date().toISOString();
    UI.bindGame(this);
    UI.renderSeats(this);
    UI.refreshAll(this);

    while (this.running && !this.winner) {
      this.day += 1;
      await this.nightPhase();
      if (this.winner) break;
      await this.dayPhase();
      if (this.winner) break;
    }

    this.phase = "end";
    UI.refreshAll(this);
    UI.showResult(this);
    this.running = false;
    // 自动落盘 replay JSON（事件流 + 玩家信息 + 胜负），供事后复盘 / 排错
    this.saveReplay().catch(err => console.warn("[game] saveReplay failed:", err.message));
    // LLM 法官：异步给出本局解说，到位后注入 result overlay
    this.runJudge().catch(err => console.warn("[game] judge failed:", err.message));
    // 游戏结束 → 仅在自己仍是"最新一局"时才解锁按钮
    // 否则旧 run() 退出时的 cleanup 会误删新一局刚设置的锁
    if (this.gen === GAME_GENERATION) {
      const btn = document.getElementById("btnStart");
      if (btn) delete btn.dataset.gameRunning;
      if (typeof window !== "undefined" && typeof window.probeProxy === "function") {
        window.probeProxy();
      }
    }
  }

  /* ============ 夜晚 ============ */
  async nightPhase() {
    this.phase = "night";
    document.body.classList.remove("is-day");
    UI.setPhase("night", this.day, "天黑请闭眼…");
    UI.log("night", `🌙 第 ${this.day} 天 · 夜晚降临`);
    await this.wait();

    this.tonightKill = null;
    this.tonightSaved = false;
    this.tonightPoison = null;
    this.tonightProtected = null;

    // ===== 守卫 =====
    const guard = this.aliveOf("guard")[0];
    if (guard) {
      UI.setPhase("night", this.day, "守卫请睁眼…");
      UI.activate(guard.idx);
      await this.wait();
      const act = await guard.nightAction(this);
      if (act && act.target !== undefined) {
        if (act.target === -1) {
          // A3：空守 —— 不守任何人；下夜可守任何人（含上夜未守的目标）
          guard.lastGuarded = null;
          this.emit(new Events.GuardProtectEvent(this.day, guard.idx, -1));
          this.appendFactTo(guard.idx, { day: this.day, type: "guard-protect", targetNo: -1, note: "空守" });
          UI.log("skill", `🛡 守卫选择空守（不守人）`, this.revealAll);
        } else {
          this.tonightProtected = act.target;
          guard.lastGuarded = act.target;
          this.emit(new Events.GuardProtectEvent(this.day, guard.idx, this.agents[act.target].no));
          this.appendFactTo(guard.idx, { day: this.day, type: "guard-protect", targetNo: this.agents[act.target].no });
          UI.drawSkillLine(guard.idx, act.target, "#4dd0a8");
          UI.markProtected(act.target);
          UI.log("skill", `🛡 守卫守护了 <span class="who">${this.agents[act.target].no}号</span>`, this.revealAll);
        }
      }
      UI.deactivate(guard.idx);
      await this.wait();
    }

    // ===== 狼人 =====
    UI.setPhase("night", this.day, "狼人请睁眼…");
    const wolves = this.aliveOf("wolf");
    wolves.forEach(w => UI.activate(w.idx));
    await this.wait();
    if (wolves.length > 0) {
      // ★ 两轮协商：第 1 轮独立提议 → 把所有提议公示 → 第 2 轮终选
      //   只剩 1 只狼时，直接走第 1 轮
      const r1Acts = await Promise.all(wolves.map(w => w._wolfKill(this, null)));
      // 记录第 1 轮提议事件（仅狼可见）；target=-1 → 空刀
      wolves.forEach((w, i) => {
        const act = r1Acts[i];
        if (act && act.target != null) {
          const targetNo = act.target === -1 ? -1 : this.agents[act.target].no;
          this.emit(new Events.WolfProposeKillEvent(
            this.day, w.no, targetNo, act.thinking || "", 1
          ));
          // PrivateMemory: 这只狼自己记下自己的提议(其他狼不通过 facts 看,通过下一轮 prompt 看)
          this.appendFactTo(w.idx, { day: this.day, type: "wolf-propose", round: 1, targetNo, thinking: act.thinking || "" });
        }
      });
      let finalActs = r1Acts;
      if (wolves.length > 1) {
        const roundOneVotes = wolves.map((w, i) => {
          const act = r1Acts[i];
          if (!act || act.target == null) return null;
          return {
            wolfNo: w.no,
            target: act.target === -1 ? "空刀" : this.agents[act.target].no,
            thinking: act.thinking || "",
          };
        }).filter(Boolean);
        if (this.revealAll && roundOneVotes.length) {
          UI.log("hint", `🐺 第 1 轮提议：${roundOneVotes.map(v => `${v.wolfNo}→${v.target}`).join("、")}`, this.revealAll);
        }
        finalActs = await Promise.all(wolves.map(w => w._wolfKill(this, roundOneVotes)));
        wolves.forEach((w, i) => {
          const act = finalActs[i];
          if (act && act.target != null) {
            const targetNo = act.target === -1 ? -1 : this.agents[act.target].no;
            this.emit(new Events.WolfProposeKillEvent(
              this.day, w.no, targetNo, act.thinking || "", 2
            ));
          }
        });
      }
      // 统计：-1 也可成为多数派 → 空刀
      const votes = {};
      finalActs.forEach(act => {
        if (act) votes[act.target] = (votes[act.target] || 0) + 1;
      });
      let target = null, max = 0;
      for (const k of Object.keys(votes)) {
        if (votes[k] > max) { max = votes[k]; target = parseInt(k); }
      }
      if (target !== null && target !== -1) {
        this.tonightKill = target;
        this.emit(new Events.WolfKillEvent(this.day, this.agents[target].no));
        UI.drawSkillLine(wolves[0].idx, target, "#ff5b6b", true);
        UI.shake(target);
        UI.log("skill", `🐺 狼人选择击杀 <span class="who">${this.agents[target].no}号</span>`, this.revealAll);
        // PrivateMemory: 每只狼记下"今晚我们刀了 X 号"(私密事实,只狼可见)
        wolves.forEach(w => this.appendFactTo(w.idx, { day: this.day, type: "wolf-kill-final", targetNo: this.agents[target].no }));
      } else {
        // A8 空刀：tonightKill 保持 null
        this.emit(new Events.WolfKillEvent(this.day, -1));
        UI.log("skill", `🐺 狼人选择空刀（不杀人）`, this.revealAll);
        wolves.forEach(w => this.appendFactTo(w.idx, { day: this.day, type: "wolf-kill-final", targetNo: -1, note: "空刀" }));
      }
    }
    wolves.forEach(w => UI.deactivate(w.idx));
    await this.wait();

    // ===== 女巫（A4：守→狼→女→预，女巫先于预言家）=====
    const witch = this.aliveOf("witch")[0];
    if (witch) {
      UI.setPhase("night", this.day, "女巫请睁眼…");
      UI.activate(witch.idx);
      await this.wait();
      const acts = await witch.nightAction(this);
      if (acts) {
        for (const act of acts) {
          if (act.type === "witch-save") {
            witch.witchHasSave = false;
            this.tonightSaved = true;
            this.emit(new Events.WitchSaveEvent(this.day, witch.idx, this.agents[act.target].no));
            this.appendFactTo(witch.idx, { day: this.day, type: "witch-save", targetNo: this.agents[act.target].no });
            UI.drawSkillLine(witch.idx, act.target, "#4dd0a8");
            UI.log("skill", `🧪 女巫使用解药救人`, this.revealAll);
          } else if (act.type === "witch-poison") {
            witch.witchHasPoison = false;
            this.tonightPoison = act.target;
            this.emit(new Events.WitchPoisonEvent(this.day, witch.idx, this.agents[act.target].no));
            this.appendFactTo(witch.idx, { day: this.day, type: "witch-poison", targetNo: this.agents[act.target].no });
            UI.drawSkillLine(witch.idx, act.target, "#b46bff");
            UI.markPoisoned(act.target);
            UI.log("skill", `🧪 女巫使用毒药 → <span class="who">${this.agents[act.target].no}号</span>`, this.revealAll);
          }
        }
      }
      UI.deactivate(witch.idx);
      await this.wait();
    }

    // ===== 预言家（最后行动，查验不影响死亡结算）=====
    const seer = this.aliveOf("seer")[0];
    if (seer) {
      UI.setPhase("night", this.day, "预言家请睁眼…");
      UI.activate(seer.idx);
      await this.wait();
      const act = await seer.nightAction(this);
      if (act && act.target !== undefined) {
        const target = this.agents[act.target];
        const isWolf = target.role === "wolf";
        seer.seerChecks.push({ target: act.target, isWolf, day: this.day });
        this.emit(new Events.SeerCheckEvent(this.day, seer.idx, seer.no, target.no, isWolf));
        this.appendFactTo(seer.idx, { day: this.day, type: "seer-check", targetNo: target.no, isWolf, result: isWolf ? "wolf" : "good" });
        UI.drawSkillLine(seer.idx, act.target, isWolf ? "#ff5b6b" : "#4ea8ff");
        UI.log("skill", `🔮 预言家查验 <span class="who">${target.no}号</span> → ${isWolf ? "🐺 狼人" : "👤 好人"}`, this.revealAll);
      }
      UI.deactivate(seer.idx);
      await this.wait();
    }

    // ===== 结算 =====
    UI.setPhase("night", this.day, "夜晚结算…");
    await this.wait(600);

    const deaths = [];
    if (this.tonightKill !== null) {
      if (this.tonightKill === this.tonightProtected && this.tonightSaved) {
        // 同守同救 → 死
        deaths.push(this.tonightKill);
      } else if (this.tonightKill === this.tonightProtected) {
        // 守住
      } else if (this.tonightSaved) {
        // 救活
      } else {
        deaths.push(this.tonightKill);
      }
    }
    if (this.tonightPoison !== null && !deaths.includes(this.tonightPoison)) {
      deaths.push(this.tonightPoison);
    }

    // 死亡（kill 现在是 async —— 内部可能 await passBadge）
    for (const idx of deaths) await this.kill(idx, "night");
    this.checkWin();
  }

  /* ============ 白天 ============ */
  async dayPhase() {
    this.phase = "day";
    document.body.classList.add("is-day");
    UI.setPhase("day", this.day, "天亮了…");
    UI.log("day", `☀️ 第 ${this.day} 天 · 白天来临`);
    await this.wait();

    // 用 try/finally 保证无论从哪条 return 路径退出，都会写入当日记忆
    // 修复：先前自爆/胜利 return 会跳过 flushAll，导致 memory/agent-N.md 缺天
    try {
      // 公布死讯
      const nightDeaths = this.history.filter(h => h.day === this.day && h.cause === "night");
      if (nightDeaths.length === 0) {
        UI.log("day", `平安夜：昨晚没有人死亡。`);
        UI.setPhase("day", this.day, "平安夜");
      } else {
        const names = nightDeaths.map(d => `${this.agents[d.idx].no}号`).join("、");
        UI.log("death", `昨晚 <span class="who">${names}</span> 死亡。`);
        UI.setPhase("day", this.day, `${names} 死亡`);
        // 遗言
        for (const d of nightDeaths) {
          await this.lastWords(d.idx);
        }
        // 猎人开枪
        for (const d of nightDeaths) {
          const a = this.agents[d.idx];
          if (a.role === "hunter" && d.cause === "night" && this.tonightPoison !== d.idx) {
            await this.hunterShoot(d.idx);
          }
        }
      }
      if (this.checkWin()) return;
      await this.wait();

      // 警长竞选（仅第一天）
      if (this.day === 1 && !this.sheriffElectionDone) {
        this.sheriffElectionDone = true;
        await this.sheriffElection();
        if (this.checkWin()) return;
        await this.wait();
      }

      // 发言阶段
      UI.setPhase("day", this.day, "依次发言…");
      const alive = this.aliveAgents();
      const startIdx = (this.day - 1) % alive.length;
      const order = alive.slice(startIdx).concat(alive.slice(0, startIdx));
      let interrupted = false;
      for (const a of order) {
        if (!a.alive) continue;
        const r = await this.speak(a);
        if (r === "self-explode") { interrupted = true; break; }
      }
      if (this.checkWin()) return;
      // 狼人自爆 → 跳过当日投票
      if (interrupted) {
        UI.log("day", `💥 狼人自爆，本日投票取消，直接进入夜晚`);
        return;
      }

      // 投票
      await this.votePhase();
      if (this.checkWin()) return;
    } finally {
      // V2.9-2：本日结束，生成复盘总结让明天 agent 学习
      // V3：移入 finally，保证自爆/胜利等所有路径都会写入记忆
      try {
        await this._buildRoundSummary();
        await Memory.flushAll(this.agents, this.day, this.speechHistory, this.history);
      } catch (e) {
        console.warn(`[dayPhase] flush day ${this.day} memory failed:`, e?.message || e);
      }
    }
  }

  /* V2.9-2 ============ 每轮复盘（事实 + LLM 推断）============ */
  async _buildRoundSummary() {
    const day = this.day;
    const parts = [];

    // 1) 昨夜死亡
    const nightDeaths = this.history.filter(h => h.day === day && h.cause === "night");
    if (nightDeaths.length === 0) parts.push("平安夜");
    else parts.push(`夜死：${nightDeaths.map(d => `${this.agents[d.idx].no}号${this.agents[d.idx].publicRole ? `(${ROLE_META[this.agents[d.idx].publicRole]?.cn || ""})` : "(未跳)"}`).join("、")}`);

    // 2) 本日新跳身份（按发言顺序首次出现的 publicRole）
    const claimsToday = new Map();
    this.speechHistory.filter(s => s.day === day && s.publicRole).forEach(s => {
      if (!claimsToday.has(s.agentNo)) claimsToday.set(s.agentNo, s.publicRole);
    });
    if (claimsToday.size) {
      parts.push(`跳身份：${[...claimsToday].map(([no, r]) => `${no}号→${ROLE_META[r]?.cn || r}`).join("、")}`);
    }

    // 3) 本日新增公开查验（粗略：取所有 publicCheckReports 的最近 N 条；理想是按 day 标记，简化处理）
    const allReports = this.publicCheckReports || [];
    if (allReports.length) {
      const recent = allReports.slice(-5);
      parts.push(`查验：${recent.map(r => `${this.agents[r.from].no}号验${this.agents[r.target].no}号=${r.result === "wolf" ? "狼" : "好人"}`).join("；")}`);
    }

    // 4) 本日出局（票出/枪杀/自爆）
    const dayDeaths = this.history.filter(h => h.day === day && h.cause !== "night");
    if (dayDeaths.length) {
      const causeCn = { voted: "被票出", shot: "被枪杀", explode: "自爆" };
      parts.push(`本日出局：${dayDeaths.map(d => `${this.agents[d.idx].no}号${this.agents[d.idx].publicRole ? `(${ROLE_META[this.agents[d.idx].publicRole]?.cn})` : ""}${causeCn[d.cause] || d.cause}`).join("、")}`);
    }

    const factual = parts.join("；") || "（无重要事件）";

    // 5) LLM 推断（一次调用，不带 tools）
    let inference = "（无推断）";
    const hook = (typeof window !== "undefined") ? window.LLM_HOOK : null;
    if (hook && hook.enabled && typeof hook.summarize === "function") {
      try {
        const alive = this.aliveAgents();
        const aliveStr = alive.map(a => `${a.no}号${a.publicRole ? `[${ROLE_META[a.publicRole]?.cn}]` : ""}`).join("、");
        const text = await hook.summarize({
          system: "你是狼人杀复盘解说员。基于事实做 30-60 字的精简推断，不要透露上帝视角，不要换行。",
          user: `第 ${day} 天复盘事实：${factual}\n场上存活：${aliveStr}\n请输出 30-60 字推断（谁可信、谁可疑、好人/狼人形势）：`,
        });
        if (text) inference = text.trim();
      } catch (e) {
        console.warn("[summary] LLM inference failed:", e.message);
      }
    }

    this.roundSummaries.push({ day, factual, inference });
    UI.log("day", `📜 第${day}天复盘：${factual}｜${inference}`);
  }

  async speak(agent) {
    // 狼人决策：是否自爆
    if (agent.role === "wolf" && await agent.decideSelfExplode(this)) {
      return await this.wolfSelfExplode(agent.idx);
    }
    UI.setSpeaking(agent.idx);
    UI.setPhase("day", this.day, `${agent.no}号 ${agent.name} 发言中…`);
    const text = await agent.generateSpeech(this);
    UI.bubble(agent.idx, agent.name, text);
    UI.setLatestSpeech(agent, text);
    UI.log("day", `<span class="who">${agent.no}号 ${agent.name}</span>：${text}`);
    this.speechHistory.push({ day: this.day, phase: this.phase, agentNo: agent.no, publicRole: agent.publicRole, kind: "day", text });
    this.emit(new Events.SpeakEvent(this.day, this.phase, agent.no, agent.publicRole, "day", text));
    this.appendPublic({ type: "speak", day: this.day, phase: "day", agentNo: agent.no, publicRole: agent.publicRole, kind: "day", text });
    // 语音朗读（开启时等其播完，关闭时立即 resolve）
    await TTS.speak(text, agent);
    // 阅读时间：未开 TTS 时给充足阅读节奏；开了 TTS 时仅留少量缓冲
    const speakMs = TTS.enabled ? 300 : Math.max(1500, this.speedMs * 1.8);
    await this.wait(speakMs);
    UI.clearSpeaking(agent.idx);
    return "ok";
  }

  /* ============ 狼人自爆 ============ */
  async wolfSelfExplode(idx) {
    const a = this.agents[idx];
    this.wolfHasExploded = true;
    a.publicRole = "wolf";

    UI.setSpeaking(idx);
    UI.setPhase("day", this.day, `💥 ${a.no}号 自爆！`);
    UI.bubble(idx, a.name + "（自爆）", `我是狼人，自爆！`);
    UI.setLatestSpeech(a, "我是狼人，自爆！", false);
    UI.log("death", `💥 <span class="who">${a.no}号 ${a.name}</span> 自爆，亮明狼人身份！`);
    this.emit(new Events.WolfExplodeEvent(this.day, a.no));
    this.appendPublic({ type: "wolf-explode", day: this.day, phase: "day", wolfNo: a.no });
    // 全场震动效果
    for (let i = 0; i < 12; i++) UI.shake(i);
    await this.wait(Math.max(900, this.speedMs));
    UI.clearSpeaking(idx);
    await this.kill(idx, "explode");
    // 自爆不开枪（猎人除外，但猎人不可能自爆，因为只有狼能自爆）
    return "self-explode";
  }

  /* ============ 警长竞选 ============ */
  async sheriffElection() {
    UI.setPhase("day", this.day, "🎖 警长竞选…");
    UI.log("day", `🎖 警长竞选开始，玩家可上警`);
    await this.wait(600);

    const alive = this.aliveAgents();
    // ★ 所有活人并发决定是否上警
    const wantRun = await Promise.all(alive.map(a => a.decideRunForSheriff(this)));
    const runners = alive.filter((_, i) => wantRun[i]);

    if (runners.length === 0) {
      UI.log("day", `无人上警，本局无警长。`);
      return;
    }
    if (runners.length === 1) {
      const w = runners[0];
      this._setSheriff(w.idx);
      UI.log("day", `🎖 仅 <span class="who">${w.no}号 ${w.name}</span> 上警，自动当选警长。`);
      return;
    }

    UI.log("day", `🎖 上警玩家：${runners.map(r => r.no + "号").join("、")}`);

    // 上警发言
    for (const a of runners) {
      if (!a.alive) continue;
      await this._sheriffSpeech(a);
    }

    // 非上警的玩家投票（上警者不能互投，但可以退水/投自己一票通常不允许）
    // 这里简化：所有非上警的玩家投，规则中常见做法
    const voters = alive.filter(a => !runners.some(r => r.idx === a.idx));
    if (voters.length === 0) {
      UI.log("day", `所有人都上警，警长难产，警徽撕毁。`);
      return;
    }

    const tally = await this._runSheriffVote(voters, runners);
    const { winners, max } = this._findVoteWinners(tally);

    if (max === 0 || winners.length === 0) {
      UI.log("day", `警长选举全员弃投，警徽撕毁。`);
    } else if (winners.length > 1) {
      // PK 一轮
      UI.log("day", `🎖 警长选举平票：${winners.map(i => this.agents[i].no + "号").join("、")}，PK 一轮`);
      for (const w of winners) await this._pkSpeech(w, true);
      const pkRunners = winners.map(i => this.agents[i]);
      const pkVoters = alive.filter(a => !pkRunners.some(r => r.idx === a.idx));
      const tally2 = await this._runSheriffVote(pkVoters, pkRunners);
      const { winners: w2, max: max2 } = this._findVoteWinners(tally2);
      if (w2.length === 1 && max2 > 0) {
        this._setSheriff(w2[0]);
        const a = this.agents[w2[0]];
        UI.log("day", `🎖 <span class="who">${a.no}号 ${a.name}</span> 当选警长（PK ${max2} 票）`);
      } else {
        UI.log("day", `🎖 PK 仍平票，警徽撕毁，本局无警长。`);
      }
    } else {
      const w = this.agents[winners[0]];
      this._setSheriff(w.idx);
      UI.log("day", `🎖 <span class="who">${w.no}号 ${w.name}</span> 当选警长（${max} 票）`);
    }
    this.later(() => UI.clearAllVotes(), 1500);
    await this.wait(600);
  }

  _setSheriff(idx) {
    const fromIdx = this.sheriffIdx;
    if (fromIdx >= 0) {
      this.agents[fromIdx].isSheriff = false;
      UI.unmarkSheriff(fromIdx);
    }
    this.sheriffIdx = idx;
    this.agents[idx].isSheriff = true;
    UI.markSheriff(idx);
    if (fromIdx < 0) {
      this.emit(new Events.SheriffElectedEvent(this.day, this.agents[idx].no));
      this.appendPublic({ type: "sheriff-elected", day: this.day, phase: "day", sheriffNo: this.agents[idx].no });
    } else {
      this.emit(new Events.BadgePassEvent(this.day, this.agents[fromIdx].no, this.agents[idx].no));
      this.appendPublic({ type: "badge-pass", day: this.day, phase: "day", fromNo: this.agents[fromIdx].no, toNo: this.agents[idx].no });
    }
  }

  async _sheriffSpeech(agent) {
    UI.setSpeaking(agent.idx);
    UI.setPhase("day", this.day, `🎖 ${agent.no}号 竞选发言…`);
    const text = await agent.generateSheriffSpeech(this);
    UI.bubble(agent.idx, agent.name + "（上警）", text);
    UI.setLatestSpeech(agent, text);
    UI.log("day", `🎖 <span class="who">${agent.no}号 ${agent.name}</span>：${text}`);
    this.speechHistory.push({ day: this.day, phase: this.phase, agentNo: agent.no, publicRole: agent.publicRole, kind: "sheriff", text });
    this.emit(new Events.SpeakEvent(this.day, this.phase, agent.no, agent.publicRole, "sheriff", text));
    this.appendPublic({ type: "speak", day: this.day, phase: "day", agentNo: agent.no, publicRole: agent.publicRole, kind: "sheriff", text });
    await TTS.speak(text, agent);
    const ms = TTS.enabled ? 300 : Math.max(1200, this.speedMs * 1.4);
    await this.wait(ms);
    UI.clearSpeaking(agent.idx);
  }

  async _pkSpeech(idx, isSheriff) {
    const a = this.agents[idx];
    UI.setSpeaking(idx);
    UI.setPhase("day", this.day, `🆚 ${a.no}号 PK 发言…`);
    // LLM 钩子优先
    let text = await LLM.speak({
      agent: a, game: this, kind: "pk",
      context: a._publicContext(this),
    });
    if (!text) {
      if (isSheriff) {
        text = pick([
          `我警徽必须给我，对面是狼！`,
          `请大家相信我，对面在干扰好人。`,
          `我能控票，对面无法稳住场子。`,
        ]);
      } else {
        text = pick([
          `我才是真好人，对面是悍跳！`,
          `请对位投我对面，今天必须把狼带走。`,
          `信我，今天不死狼明天没希望。`,
        ]);
      }
    }
    UI.bubble(idx, a.name + "（PK）", text);
    UI.setLatestSpeech(a, text);
    UI.log("vote", `🆚 <span class="who">${a.no}号 ${a.name}</span>：${text}`);
    this.speechHistory.push({ day: this.day, phase: this.phase, agentNo: a.no, publicRole: a.publicRole, kind: "pk", text });
    this.emit(new Events.SpeakEvent(this.day, this.phase, a.no, a.publicRole, "pk", text));
    this.appendPublic({ type: "speak", day: this.day, phase: "day", agentNo: a.no, publicRole: a.publicRole, kind: "pk", text });
    await TTS.speak(text, a);
    await this.wait(TTS.enabled ? 300 : Math.max(1200, this.speedMs * 1.4));
    UI.clearSpeaking(idx);
  }

  async _runSheriffVote(voters, runners) {
    const tally = {};
    runners.forEach(r => tally[r.idx] = 0);
    UI.showVoteBanner("警长选举投票中…");
    // ★ 所有 voter 并发问 LLM 拿投票决定
    const decisions = await Promise.all(voters.map(v => v.sheriffVote(this, runners)));
    // 再按顺序渲染（保留动画节奏）
    for (let i = 0; i < voters.length; i++) {
      const v = voters[i];
      const t = decisions[i];
      if (t >= 0 && tally[t] !== undefined) {
        tally[t] = (tally[t] || 0) + 1;
        UI.showVoteOn(t, tally[t]);
        UI.drawSkillLine(v.idx, t, "#ffd97a");
        UI.log("vote", `<span class="who">${v.no}号</span> 选 ${this.agents[t].no}号`);
        this.emit(new Events.VoteEvent(this.day, v.no, this.agents[t].no, "sheriff-vote"));
        this.appendPublic({ type: "vote", day: this.day, phase: "day", fromNo: v.no, targetNo: this.agents[t].no, kind: "sheriff-vote" });
      } else {
        UI.log("vote", `<span class="who">${v.no}号</span> 弃投`);
        this.emit(new Events.VoteEvent(this.day, v.no, -1, "sheriff-vote"));
        this.appendPublic({ type: "vote", day: this.day, phase: "day", fromNo: v.no, targetNo: -1, kind: "sheriff-vote" });
      }
      await this.wait(Math.min(380, this.speedMs * 0.4));
    }
    UI.showVoteBanner("");
    return tally;
  }

  _findVoteWinners(tally) {
    let max = 0, winners = [];
    const EPS = 1e-9;
    for (const k of Object.keys(tally)) {
      const v = tally[k], idx = parseInt(k);
      if (v > max + EPS) { max = v; winners = [idx]; }
      else if (Math.abs(v - max) < EPS && v > EPS) winners.push(idx);
    }
    return { winners, max };
  }

  async lastWords(idx) {
    const agent = this.agents[idx];
    UI.markLastWords(idx);
    UI.setPhase("day", this.day, `${agent.no}号 ${agent.name} 遗言…`);

    // 1) 先把"必然发生"的副作用执行掉（暴露身份 + 必须的事件），与发言文本解耦
    let template;
    if (agent.role === "seer") {
      const checks = agent.seerChecks;
      if (checks.length > 0) {
        const last = checks[checks.length - 1];
        template = `我是预言家！${this.agents[last.target].no}号是${last.isWolf ? "🐺狼人" : "👤好人"}，请好人对位投票！`;
        if (!agent.hasClaimed) {
          this.fireEvent({ type: "claim", from: idx, role: "seer" });
          this.fireEvent({ type: "check-report", from: idx, target: last.target, result: last.isWolf ? "wolf" : "good" });
        }
      } else {
        template = `我是预言家，可惜没来得及验人，好人小心！`;
        this.fireEvent({ type: "claim", from: idx, role: "seer" });
      }
      agent.publicRole = "seer";
    } else if (agent.role === "witch") {
      template = `我是女巫，没机会再帮你们了，跟好节奏！`;
      agent.publicRole = "witch";
      this.fireEvent({ type: "claim", from: idx, role: "witch" });
    } else if (agent.role === "guard") {
      template = `我是守卫，昨晚我守了人，狼人猖狂，好人加油！`;
      agent.publicRole = "guard";
      this.fireEvent({ type: "claim", from: idx, role: "guard" });
    } else if (agent.role === "hunter") {
      template = `我是猎人 — 准备好接受我的子弹！`;
      agent.publicRole = "hunter";
      this.fireEvent({ type: "claim", from: idx, role: "hunter" });
    } else if (agent.role === "wolf") {
      template = pick([`我承认我是狼，今天就这样吧。`, `没什么好说的，狼人继续努力。`, `好人多看场上信息，不要被带偏。`]);
      agent.publicRole = "wolf";
    } else {
      template = pick([`我就是个老百姓，可惜了。`, `跟好预言家，别被狼带偏！`, `不亏，民没什么遗憾。`]);
      agent.publicRole = "villager";
    }

    // 2) 文本走 LLM，失败回退模板
    const text = await LLM.speak({
      agent, game: this, kind: "last-words",
      context: agent._publicContext(this),
    }) || template;
    UI.bubble(idx, agent.name + "（遗言）", text);
    UI.setLatestSpeech(agent, text, true);
    UI.log("death", `<span class="who">${agent.no}号 ${agent.name} 遗言</span>：${text}`);
    this.speechHistory.push({ day: this.day, phase: this.phase, agentNo: agent.no, publicRole: agent.publicRole, kind: "last-words", text });
    this.emit(new Events.SpeakEvent(this.day, this.phase, agent.no, agent.publicRole, "last-words", text));
    this.appendPublic({ type: "speak", day: this.day, phase: "day", agentNo: agent.no, publicRole: agent.publicRole, kind: "last-words", text });
    await TTS.speak(text, agent);
    await this.wait(TTS.enabled ? 300 : Math.max(1500, this.speedMs * 1.8));

    // 警长在遗言里决定警徽流转（此后 a.isSheriff=false，避免 kill() 再处理）
    if (agent.isSheriff) {
      const target = await agent.passBadge(this);
      agent.isSheriff = false;
      UI.unmarkSheriff(idx);
      if (this.sheriffIdx === idx) this.sheriffIdx = -1;
      if (target >= 0 && this.agents[target].alive && target !== idx) {
        this._setSheriff(target);
        UI.log("skill", `🎖 <span class="who">${agent.no}号</span> 把警徽传给 <span class="who">${this.agents[target].no}号 ${this.agents[target].name}</span>`);
        UI.bubble(idx, agent.name + "（遗言）", `警徽传给 ${this.agents[target].no} 号！`);
      } else {
        UI.log("skill", `🎖 <span class="who">${agent.no}号</span> 撕毁警徽！`);
      }
      await this.wait(Math.max(300, this.speedMs * 0.8));
    }

    UI.unmarkLastWords(idx);
  }

  async hunterShoot(idx) {
    const hunter = this.agents[idx];
    // 选择最高怀疑度的活人
    const candidates = this.aliveAgents().filter(a => a.idx !== idx);
    if (candidates.length === 0) return;
    const target = candidates.reduce((best, a) =>
      hunter.suspicion[a.idx] > hunter.suspicion[best.idx] ? a : best, candidates[0]);
    UI.log("skill", `🏹 猎人开枪 → <span class="who">${target.no}号</span>`);
    UI.drawSkillLine(idx, target.idx, "#ff8b3d", true);
    UI.shake(target.idx);
    await this.wait(800);
    // PublicLog: 猎人开枪是公开事件
    this.appendPublic({ type: "hunter-shot", day: this.day, phase: this.phase, hunterNo: hunter.no, targetNo: target.no });
    await this.kill(target.idx, "shot");
    // 被开枪的若是猎人也开枪（链式）
    if (target.role === "hunter") {
      await this.hunterShoot(target.idx);
    }
  }

  /* ============ 投票 ============ */
  async votePhase() {
    UI.setPhase("vote", this.day, "投票阶段…");
    UI.log("vote", `🗳 进入投票阶段${this.sheriffIdx >= 0 ? "（警长票权 1.5）" : ""}`);
    const candidates = this.aliveAgents();
    const tally = await this._runDailyVote(candidates, candidates);

    let { winners, max } = this._findVoteWinners(tally);

    // 调参：被高票的玩家在所有人眼里更可疑（票型反推）
    this._updateSuspicionFromTally(tally);

    let executed = -1;
    if (winners.length === 0 || max === 0) {
      UI.log("vote", `全员弃票，今天无人放逐。`);
    } else if (winners.length > 1) {
      // 平票 PK
      UI.log("vote", `🆚 平票于 ${winners.map(i => this.agents[i].no + "号").join("、")}，进入 PK 发言`);
      const pkRunners = winners.map(i => this.agents[i]);
      for (const a of pkRunners) {
        if (!a.alive) continue;
        await this._pkSpeech(a.idx, false);
      }
      // PK 投票：PK 双方不参与
      const pkVoters = candidates.filter(c => !pkRunners.some(r => r.idx === c.idx));
      if (pkVoters.length === 0) {
        UI.log("vote", `场上仅剩 PK 双方，无人可投，警徽决定不出，本日无放逐。`);
      } else {
        const tally2 = await this._runDailyVote(pkVoters, pkRunners);
        const { winners: w2, max: max2 } = this._findVoteWinners(tally2);
        if (w2.length === 1 && max2 > 0) {
          executed = w2[0];
          const a = this.agents[executed];
          UI.log("vote", `<span class="who">${a.no}号 ${a.name}</span> PK 后被放逐（${max2.toFixed(1)} 票）`);
        } else {
          UI.log("vote", `PK 仍平票，今天无人放逐。`);
        }
      }
    } else {
      executed = winners[0];
      const a = this.agents[executed];
      UI.log("vote", `<span class="who">${a.no}号 ${a.name}</span> 被放逐（${max.toFixed(1)} 票）`);
    }

    if (executed >= 0) {
      this.emit(new Events.ExecuteEvent(this.day, this.agents[executed].no, "vote"));
      // PublicLog: 处决是公开事件,但 reason 字段(vote/pk)只对外说"投票")
      this.appendPublic({ type: "execute", day: this.day, phase: "day", targetNo: this.agents[executed].no, kind: "vote" });
      await this.lastWords(executed);
      await this.kill(executed, "voted");
      const a = this.agents[executed];
      if (a.role === "hunter") {
        await this.hunterShoot(executed);
      }
    }

    this.later(() => UI.clearAllVotes(), 1500);
  }

  _updateSuspicionFromTally(tally) {
    const entries = Object.entries(tally)
      .map(([idx, votes]) => ({ idx: +idx, votes }))
      .filter(e => e.votes > 0)
      .sort((a, b) => b.votes - a.votes);
    if (entries.length === 0) return;
    const top = entries[0];
    const second = entries[1] && entries[1].votes >= top.votes * 0.7 ? entries[1] : null;
    this.aliveAgents().forEach(a => {
      if (a.idx !== top.idx) a.suspicion[top.idx] = Math.min(1, a.suspicion[top.idx] + 0.15);
      if (second && a.idx !== second.idx) {
        a.suspicion[second.idx] = Math.min(1, a.suspicion[second.idx] + 0.08);
      }
    });
  }

  async _runDailyVote(voters, candidates) {
    const tally = {};
    candidates.forEach(c => tally[c.idx] = 0);
    UI.showVoteBanner("投票中…");
    // ★ 12 个 voter 并发问 LLM 拿决策（用 -2 标记死人跳过）
    const decisions = await Promise.all(voters.map(v =>
      v.alive ? v.voteTarget(this, candidates) : Promise.resolve(-2)
    ));
    // 再按顺序渲染（保留动画节奏）
    for (let i = 0; i < voters.length; i++) {
      const voter = voters[i];
      const targetIdx = decisions[i];
      if (targetIdx === -2) continue;
      const weight = voter.isSheriff ? 1.5 : 1;
      if (targetIdx === -1 || tally[targetIdx] === undefined) {
        UI.log("vote", `<span class="who">${voter.no}号</span>${voter.isSheriff ? " 🎖" : ""} 弃票`);
        this.emit(new Events.VoteEvent(this.day, voter.no, -1, "vote"));
        this.appendPublic({ type: "vote", day: this.day, phase: "day", fromNo: voter.no, targetNo: -1, kind: "vote" });
      } else {
        tally[targetIdx] = (tally[targetIdx] || 0) + weight;
        UI.showVoteOn(targetIdx, tally[targetIdx]);
        UI.drawSkillLine(voter.idx, targetIdx, "#ffcf6b");
        UI.log("vote", `<span class="who">${voter.no}号</span>${voter.isSheriff ? " 🎖" : ""} → ${this.agents[targetIdx].no}号${voter.isSheriff ? "（1.5）" : ""}`);
        this.fireEvent({ type: "vote", from: voter.idx, target: targetIdx });
        this.emit(new Events.VoteEvent(this.day, voter.no, this.agents[targetIdx].no, "vote"));
        this.appendPublic({ type: "vote", day: this.day, phase: "day", fromNo: voter.no, targetNo: this.agents[targetIdx].no, kind: "vote" });
      }
      await this.wait(Math.min(380, this.speedMs * 0.4));
    }
    UI.showVoteBanner("");
    return tally;
  }

  /* ============ 死亡 ============ */
  async kill(idx, cause) {
    const a = this.agents[idx];
    if (!a.alive) return;
    a.alive = false;
    this.history.push({ idx, day: this.day, cause });
    this.emit(new Events.DeathEvent(this.day, this.phase, a.no));
    UI.markDead(idx);
    // A6/A8：夜晚死讯不报死因（狼刀 / 毒 / 同守同救对外不可分辨），
    //        白天事件（shot/voted/explode）本就公开，可显示。
    //        god 视角 (revealAll) 始终显示完整 cause。
    const causeLabel =
      cause === "night" ? "夜晚" :
      cause === "shot" ? "枪杀" :
      cause === "voted" ? "放逐" :
      cause === "explode" ? "自爆" :
      "死亡";
    const hidesCause = cause === "night" && !this.revealAll;
    UI.log("death", `💀 ${a.no}号 ${a.name} 倒下${hidesCause ? "" : `（${causeLabel}）`}`);
    UI.updateAliveCounter(this);
    // PublicLog: 死讯只含座位号 + 阶段(night/day),绝不含 cause(规格核心红线)
    this.appendPublic({ type: "death", day: this.day, phase: this.phase, targetNo: a.no });

    // 警徽流转：
    //   night / voted —— 死者会在 lastWords 中亲自传徽，这里不动
    //   shot / explode —— 没遗言机会，由 kill 走 AI 自动决策
    if (a.isSheriff && (cause === "shot" || cause === "explode")) {
      a.isSheriff = false;
      UI.unmarkSheriff(idx);
      if (this.sheriffIdx === idx) this.sheriffIdx = -1;
      const target = await a.passBadge(this);
      if (target >= 0 && this.agents[target].alive) {
        this._setSheriff(target);
        UI.log("skill", `🎖 警徽流向 <span class="who">${this.agents[target].no}号 ${this.agents[target].name}</span>`);
      } else {
        UI.log("skill", `🎖 警徽撕毁`);
      }
    }
  }

  /* ============ 胜负 ============ */
  /* ============ LLM 法官 ============ */
  async runJudge() {
    const hook = (typeof window !== "undefined") ? window.LLM_HOOK : null;
    if (!hook || !hook.enabled || typeof hook.judge !== "function") return;
    UI.setJudgeText("📜 法官正在解说本局…");
    const text = await hook.judge({
      winner: this.winner,
      players: this.agents.map(a => ({
        no: a.no, name: a.name, role: a.role,
        personality: a.personality.name,
        alive: a.alive, isSheriff: a.isSheriff,
      })),
      events: (this.events || []).map(e => typeof e.toJSON === "function" ? e.toJSON() : { ...e }),
      history: this.history,
    });
    if (text && text.trim()) {
      UI.setJudgeText("📜 " + text.trim());
    } else {
      UI.setJudgeText("");
    }
  }

  /* ============ Replay 落盘 ============ */
  async saveReplay() {
    const base = (() => {
      if (typeof window === "undefined") return "http://127.0.0.1:3001";
      if (window.electron?.proxyPort) return `http://127.0.0.1:${window.electron.proxyPort}`;
      const p = new URLSearchParams(location.search).get("port");
      return `http://127.0.0.1:${p ? Number(p) : 3001}`;
    })();
    const body = {
      gen: this.gen,
      startedAt: this._startedAt || null,
      endedAt: new Date().toISOString(),
      day: this.day,
      winner: this.winner,
      sheriffIdx: this.sheriffIdx,
      wolfHasExploded: this.wolfHasExploded,
      players: this.agents.map(a => ({
        idx: a.idx, no: a.no, name: a.name,
        role: a.role,
        personality: a.personality.name,
        alive: a.alive,
        publicRole: a.publicRole,
        isSheriff: a.isSheriff,
      })),
      events: (this.events || []).map(e => typeof e.toJSON === "function" ? e.toJSON() : { ...e }),
      history: this.history,
      roundSummaries: this.roundSummaries,
      publicCheckReports: this.publicCheckReports,
    };
    const resp = await fetch(`${base}/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`replay save ${resp.status}: ${await resp.text()}`);
    const data = await resp.json();
    console.log(`[game] replay 已保存：logs/${data.filename}`);
    return data.filename;
  }

  checkWin() {
    const aliveWolves = this.aliveOf("wolf").length;
    const aliveGods = this.aliveAgents().filter(a => ["seer","witch","hunter","guard"].includes(a.role)).length;
    const aliveVillagers = this.aliveOf("villager").length;
    if (aliveWolves === 0) {
      this.winner = "good";
      UI.log("win", `🎉 好人阵营胜利！`);
      return true;
    }
    if (aliveGods === 0 || aliveVillagers === 0) {
      this.winner = "wolf";
      UI.log("win", `🐺 狼人阵营胜利！（${aliveGods === 0 ? "屠神" : "屠民"}）`);
      return true;
    }
    return false;
  }
}

/* =============================================================
   UI · 渲染层
   ============================================================= */
const UI = {
  seatsEl: null, logEl: null, fxLayer: null, bubbleLayer: null,
  centerEmoji: null, centerDay: null, centerStatus: null,
  phaseIcon: null, phaseText: null, phaseDay: null,
  voteBanner: null, latestSpeech: null,
  game: null,

  init() {
    this.seatsEl = document.getElementById("seats");
    this.logEl = document.getElementById("log");
    this.fxLayer = document.getElementById("fxLayer");
    this.bubbleLayer = document.getElementById("bubbleLayer");
    this.centerEmoji = document.getElementById("centerEmoji");
    this.centerDay = document.getElementById("centerDay");
    this.centerStatus = document.getElementById("centerStatus");
    this.phaseIcon = document.getElementById("phaseIcon");
    this.phaseText = document.getElementById("phaseText");
    this.phaseDay = document.getElementById("phaseDay");
    this.voteBanner = document.getElementById("voteBanner");
    this.latestSpeech = document.getElementById("latestSpeech");
  },

  bindGame(g) { this.game = g; },

  /* ============ 座位渲染 ============ */
  renderSeats(game) {
    this.seatsEl.innerHTML = "";
    // 清空残留的特效层与气泡层，避免坐标错位
    if (this.fxLayer) this.fxLayer.innerHTML = "";
    if (this.bubbleLayer) this.bubbleLayer.innerHTML = "";

    const N = 12;
    const wrap = document.querySelector(".table-wrap");
    const rect = wrap.getBoundingClientRect();
    const cx = rect.width / 2, cy = rect.height / 2;
    // 半径根据中央光环 (120px) 自适应，避免座位贴中央
    const minDim = Math.min(rect.width, rect.height);
    const rx = Math.max(minDim * 0.40, 160);
    const ry = Math.max(minDim * 0.36, 150);

    game.agents.forEach((a, i) => {
      // 顶部为1号，顺时针
      const angle = (-90 + (360 / N) * i) * Math.PI / 180;
      const x = cx + rx * Math.cos(angle);
      const y = cy + ry * Math.sin(angle);
      const seat = document.createElement("div");
      seat.className = "seat";
      seat.id = `seat-${a.idx}`;
      seat.style.left = x + "px";
      seat.style.top = y + "px";
      seat.innerHTML = `
        <div class="avatar">
          <span class="seat-no">${a.no}</span>
          <span class="emoji">${a.avatar}</span>
          <div class="badges"></div>
          <div class="votes" id="vote-${a.idx}">0</div>
        </div>
        <div class="name">${a.name}</div>
        <div class="role-tag" id="role-${a.idx}">${a.personality.name}</div>
      `;
      this.seatsEl.appendChild(seat);

      // 还原状态（应对窗口缩放重新渲染）
      if (!a.alive) {
        seat.classList.add("dead");
        this.revealRole(a);
      } else if (game.revealAll) {
        this.revealRole(a);
      }
      if (a.isSheriff) this.markSheriff(a.idx);
    });
    // FX layer 设置尺寸
    this.fxLayer.setAttribute("viewBox", `0 0 ${rect.width} ${rect.height}`);
    this.fxLayer.setAttribute("width", rect.width);
    this.fxLayer.setAttribute("height", rect.height);
  },

  refreshAll(game) {
    this.updateAliveCounter(game);
    if (game.revealAll || game.phase === "end") {
      game.agents.forEach(a => this.revealRole(a));
    }
  },

  revealRole(a) {
    const el = document.getElementById(`role-${a.idx}`);
    if (!el || !a.role) return;
    const meta = ROLE_META[a.role];
    el.textContent = meta.emoji + " " + meta.cn;
    el.className = "role-tag show " + meta.cls;
  },

  showPersonality(a) {
    const el = document.getElementById(`role-${a.idx}`);
    if (!el) return;
    el.textContent = a.personality.name;
    el.className = "role-tag";
  },

  /* ============ 阶段提示 ============ */
  setPhase(phase, day, status) {
    const map = {
      idle: ["🌙", "准备开始"],
      night: ["🌙", "夜晚"],
      day: ["☀️", "白天"],
      vote: ["🗳", "投票"],
      end: ["🏁", "结束"],
    };
    const [icon, text] = map[phase] || map.idle;
    this.phaseIcon.textContent = icon;
    this.phaseText.textContent = text;
    this.phaseDay.textContent = `第 ${day} 天`;
    this.centerEmoji.textContent = icon;
    this.centerDay.textContent = `第 ${day} 天`;
    this.centerStatus.textContent = status || "";
  },

  /* ============ 座位状态 ============ */
  activate(idx) { document.getElementById(`seat-${idx}`)?.classList.add("active"); },
  deactivate(idx) { document.getElementById(`seat-${idx}`)?.classList.remove("active"); },
  setSpeaking(idx) {
    document.querySelectorAll(".seat.speaking").forEach(el => el.classList.remove("speaking"));
    document.getElementById(`seat-${idx}`)?.classList.add("speaking");
  },
  clearSpeaking(idx) { document.getElementById(`seat-${idx}`)?.classList.remove("speaking"); },
  shake(idx) {
    const el = document.getElementById(`seat-${idx}`);
    if (!el) return;
    el.classList.add("targeted");
    this.game.later(() => el.classList.remove("targeted"), 1200);
  },
  markProtected(idx) {
    const el = document.getElementById(`seat-${idx}`);
    if (!el) return;
    el.classList.add("protected");
    this.game.later(() => el.classList.remove("protected"), 1500);
  },
  markPoisoned(idx) {
    const el = document.getElementById(`seat-${idx}`);
    if (!el) return;
    el.classList.add("poisoned");
    this.game.later(() => el.classList.remove("poisoned"), 1500);
  },
  markLastWords(idx) { document.getElementById(`seat-${idx}`)?.classList.add("last-words"); },
  unmarkLastWords(idx) { document.getElementById(`seat-${idx}`)?.classList.remove("last-words"); },
  markSheriff(idx) {
    const seat = document.getElementById(`seat-${idx}`);
    if (!seat) return;
    seat.classList.add("sheriff");
    const badges = seat.querySelector(".badges");
    if (!badges) return;
    if (badges.querySelector(".badge.sheriff")) return;
    const b = document.createElement("span");
    b.className = "badge sheriff";
    b.textContent = "🎖";
    b.title = "警长";
    badges.appendChild(b);
  },
  unmarkSheriff(idx) {
    const seat = document.getElementById(`seat-${idx}`);
    if (!seat) return;
    seat.classList.remove("sheriff");
    const b = seat.querySelector(".badge.sheriff");
    if (b) b.remove();
  },
  markDead(idx) {
    const el = document.getElementById(`seat-${idx}`);
    if (!el) return;
    el.classList.add("dead");
    el.classList.remove("speaking","active","last-words","protected","poisoned","targeted");
    // 暴露身份
    const a = this.game.agents[idx];
    this.revealRole(a);
  },

  /* ============ 技能特效线 ============ */
  drawSkillLine(fromIdx, toIdx, color, withArrow = false) {
    const wrap = document.querySelector(".table-wrap");
    const rect = wrap.getBoundingClientRect();
    const a = document.querySelector(`#seat-${fromIdx} .avatar`);
    const b = document.querySelector(`#seat-${toIdx} .avatar`);
    if (!a || !b) return;
    const ar = a.getBoundingClientRect();
    const br = b.getBoundingClientRect();
    const x1 = ar.left + ar.width / 2 - rect.left;
    const y1 = ar.top  + ar.height / 2 - rect.top;
    const x2 = br.left + br.width / 2 - rect.left;
    const y2 = br.top  + br.height / 2 - rect.top;

    const ns = "http://www.w3.org/2000/svg";
    const line = document.createElementNS(ns, "line");
    line.setAttribute("x1", x1); line.setAttribute("y1", y1);
    line.setAttribute("x2", x2); line.setAttribute("y2", y2);
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", "3");
    line.setAttribute("opacity", "0.9");
    line.setAttribute("class", "fx-line");
    line.style.filter = `drop-shadow(0 0 6px ${color})`;
    this.fxLayer.appendChild(line);
    this.game.later(() => line.remove(), 1500);

    // 端点亮点
    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("cx", x2); dot.setAttribute("cy", y2);
    dot.setAttribute("r", "8"); dot.setAttribute("fill", color);
    dot.setAttribute("opacity", "0.6");
    dot.style.filter = `drop-shadow(0 0 8px ${color})`;
    this.fxLayer.appendChild(dot);
    this.game.later(() => dot.remove(), 1500);
  },

  /* ============ 发言气泡 ============ */
  bubble(idx, who, text) {
    const wrap = document.querySelector(".table-wrap");
    const rect = wrap.getBoundingClientRect();
    const a = document.querySelector(`#seat-${idx} .avatar`);
    if (!a) return;
    const ar = a.getBoundingClientRect();
    const cx = ar.left + ar.width / 2 - rect.left;
    // 圆桌下半的座位，气泡向下显示，避免被中央光环挡住
    const seatTop = ar.top - rect.top;
    const isLower = seatTop > rect.height * 0.55;
    const topY = isLower ? (ar.bottom - rect.top + 16) : (seatTop - 100);

    const el = document.createElement("div");
    el.className = "bubble" + (isLower ? " bubble-below" : "");
    el.innerHTML = `<div class="who">${who}</div>${text}`;
    this.bubbleLayer.appendChild(el);
    // 测量后 clamp 到容器内
    const bw = 220, bh = 90;
    let left = cx - bw / 2;
    left = Math.max(8, Math.min(rect.width - bw - 8, left));
    let top  = Math.max(8, Math.min(rect.height - bh - 8, topY));
    el.style.left = left + "px";
    el.style.top  = top  + "px";

    this.game.later(() => {
      el.style.transition = "opacity 0.5s, transform 0.5s";
      el.style.opacity = "0";
      el.style.transform = "translateY(-10px)";
    }, 3200);
    this.game.later(() => el.remove(), 3800);
  },

  setLatestSpeech(agent, text, isLast = false) {
    this.latestSpeech.innerHTML = `<span class="speaker">${agent.no}号 ${agent.name}${isLast ? "（遗言）" : ""}：</span>${text}`;
  },

  /* ============ 投票 ============ */
  showVoteBanner(text) {
    if (!text) { this.voteBanner.classList.remove("show"); return; }
    this.voteBanner.textContent = text;
    this.voteBanner.classList.add("show");
  },
  showVoteOn(idx, count) {
    const el = document.getElementById(`vote-${idx}`);
    if (!el) return;
    el.textContent = count;
    el.classList.add("show");
  },
  clearAllVotes() {
    document.querySelectorAll(".votes").forEach(el => {
      el.classList.remove("show");
      el.textContent = "0";
    });
  },

  /* ============ 日志 ============ */
  log(type, html, alsoCenter = false) {
    const e = document.createElement("div");
    e.className = "log-entry " + type;
    const tagMap = {
      night: "夜", day: "昼", death: "亡", skill: "技", vote: "投", win: "胜",
    };
    e.innerHTML = `<span class="tag">${tagMap[type] || ""}</span>${html}`;
    this.logEl.appendChild(e);
    this.logEl.scrollTop = this.logEl.scrollHeight;
    if (alsoCenter) this.centerStatus.textContent = html.replace(/<[^>]+>/g, "");
  },

  /* ============ 计数 ============ */
  updateAliveCounter(game) {
    const wolves = game.aliveOf("wolf").length;
    const good = game.aliveAgents().length - wolves;
    document.getElementById("aliveGood").textContent = good;
    document.getElementById("aliveBad").textContent = wolves;
  },

  /* ============ 结算面板 ============ */
  setJudgeText(text) {
    const el = document.getElementById("resultJudge");
    if (el) el.textContent = text || "";
  },

  showResult(game) {
    const overlay = document.getElementById("resultOverlay");
    const emoji = document.getElementById("resultEmoji");
    const title = document.getElementById("resultTitle");
    const subtitle = document.getElementById("resultSubtitle");
    const roles = document.getElementById("resultRoles");
    const judge = document.getElementById("resultJudge");

    if (game.winner === "good") {
      emoji.textContent = "🏆";
      title.textContent = "好人阵营胜利";
      subtitle.textContent = "真相大白，狼患平定";
    } else {
      emoji.textContent = "🐺";
      title.textContent = "狼人阵营胜利";
      subtitle.textContent = "屠刀已悬，村庄沦陷";
    }
    if (judge) judge.textContent = "";
    roles.innerHTML = "";
    game.agents.forEach(a => {
      const m = ROLE_META[a.role];
      const d = document.createElement("div");
      d.className = "rrow";
      d.innerHTML = `<span class="rno">${a.no}号</span> ${a.name} ${m.emoji}<span class="rrl">${m.cn}${a.alive ? "" : " · 已亡"}</span>`;
      roles.appendChild(d);
    });
    overlay.classList.remove("hidden");
  },

  hideResult() {
    document.getElementById("resultOverlay").classList.add("hidden");
  },
};

/* =============================================================
   启动绑定
   ============================================================= */
let currentGame = null;

function startNewGame() {
  UI.hideResult();
  if (currentGame) currentGame.cancel();
  // 清空上一局的 memory/agent-N.md（fire-and-forget）
  if (typeof window !== "undefined" && window.MEMORY) window.MEMORY.reset();
  // 锁定开始按钮（游戏进行中）
  const btnStart = document.getElementById("btnStart");
  if (btnStart) {
    btnStart.disabled = true;
    btnStart.title = "游戏进行中…";
    btnStart.dataset.gameRunning = "1";   // 标记给 probeProxy 看，避免被解锁
  }
  // 复位暂停按钮文字
  const btnPause = document.getElementById("btnPause");
  if (btnPause) btnPause.textContent = "⏸ 暂停";
  // 复位昼夜背景
  document.body.classList.remove("is-day");

  currentGame = new Game();
  currentGame.reset();
  currentGame.speedMs = parseInt(document.getElementById("speedSel").value);
  currentGame.revealAll = document.getElementById("revealRoles").checked;
  UI.bindGame(currentGame);
  UI.renderSeats(currentGame);
  UI.refreshAll(currentGame);
  UI.clearAllVotes();
  // 初始日志
  document.getElementById("log").innerHTML = "";
  document.getElementById("latestSpeech").innerHTML = "—— 等待开局 ——";
  UI.log("day", "—— 新的一局开始，12位玩家入场 ——");
  currentGame.run();
}

window.addEventListener("DOMContentLoaded", () => {
  UI.init();

  // 占位渲染
  const placeholder = new Game();
  placeholder.assignRoles();
  UI.bindGame(placeholder);
  UI.renderSeats(placeholder);

  // TTS 初始化（中文 voice 异步加载，可能要等 voiceschanged 事件）
  TTS.init();
  const ttsBox = document.getElementById("ttsToggle");
  if (ttsBox) {
    ttsBox.addEventListener("change", e => {
      TTS.enabled = e.target.checked;
      if (!TTS.enabled) TTS.stop();
    });
  }

  document.getElementById("btnStart").addEventListener("click", () => {
    TTS.stop();
    startNewGame();
  });
  document.getElementById("btnPause").addEventListener("click", e => {
    if (!currentGame || !currentGame.running) return;
    currentGame.paused = !currentGame.paused;
    if (currentGame.paused) TTS.pause(); else TTS.resume();
    e.target.textContent = currentGame.paused ? "▶ 继续" : "⏸ 暂停";
  });
  document.getElementById("btnReset").addEventListener("click", () => {
    TTS.stop();
    startNewGame();
  });
  document.getElementById("btnPlayAgain").addEventListener("click", () => {
    TTS.stop();
    startNewGame();
  });
  document.getElementById("speedSel").addEventListener("change", e => {
    if (currentGame) currentGame.speedMs = parseInt(e.target.value);
  });
  document.getElementById("revealRoles").addEventListener("change", e => {
    if (currentGame) {
      currentGame.revealAll = e.target.checked;
      if (e.target.checked) currentGame.agents.forEach(a => UI.revealRole(a));
      else currentGame.agents.forEach(a => { if (a.alive) UI.showPersonality(a); });
    }
  });
  window.addEventListener("resize", () => {
    if (currentGame) UI.renderSeats(currentGame);
    else { UI.renderSeats(placeholder); }
  });
});
