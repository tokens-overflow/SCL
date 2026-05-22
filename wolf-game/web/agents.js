/* =============================================================
   Werewolf · Agent AI
   每个 agent 拥有：性格、角色、记忆、目标推断、发言生成器
   ============================================================= */

// LLM 钩子：llm-adapter.js 会把 { enabled, speak, decide, summarize } 挂到 window.LLM_HOOK。
// 任一调用失败/返回空，自动回退到本文件下方的规则版策略。
const LLM = {
  get hook() { return (typeof window !== "undefined" ? window.LLM_HOOK : null) || null; },
  enabled() { return !!(this.hook && this.hook.enabled); },
  async speak(payload) {
    if (!this.enabled()) return null;
    try {
      const t = await this.hook.speak(payload);
      return (typeof t === "string" && t.trim()) ? t.trim() : null;
    } catch (e) { console.warn("[LLM] speak failed, fallback:", e); return null; }
  },
  async decide(payload) {
    if (!this.enabled() || typeof this.hook.decide !== "function") return null;
    try {
      const r = await this.hook.decide(payload);
      return (r === undefined) ? null : r;
    } catch (e) { console.warn("[LLM] decide failed, fallback:", e); return null; }
  },
};

const PERSONALITIES = [
  { name: "稳健", aggro: 0.3, deception: 0.5, talkative: 0.6 },
  { name: "凶猛", aggro: 0.9, deception: 0.6, talkative: 0.9 },
  { name: "狡诈", aggro: 0.5, deception: 0.95, talkative: 0.8 },
  { name: "天真", aggro: 0.2, deception: 0.1, talkative: 0.5 },
  { name: "缜密", aggro: 0.4, deception: 0.4, talkative: 0.8 },
  { name: "锋利", aggro: 0.85, deception: 0.4, talkative: 0.85 },
  { name: "沉稳", aggro: 0.35, deception: 0.5, talkative: 0.5 },
  { name: "毒舌", aggro: 0.95, deception: 0.5, talkative: 1.0 },
  { name: "圆滑", aggro: 0.4, deception: 0.7, talkative: 0.75 },
  { name: "孤勇", aggro: 0.7, deception: 0.3, talkative: 0.7 },
  { name: "理性", aggro: 0.5, deception: 0.5, talkative: 0.75 },
  { name: "浮躁", aggro: 0.75, deception: 0.4, talkative: 0.95 },
];

const NAMES = ["阿狸","紫霞","白起","花木兰","李白","貂蝉","荆轲","韩信","程咬金","虞姬","小乔","典韦"];

const AVATARS = ["🦊","🌸","⚔️","🏹","🍶","🪭","🗡️","🐎","🪓","🌙","🎐","🛡️"];

/* ===== Agent 类 ===== */
class Agent {
  constructor(idx) {
    this.idx = idx;                  // 0..11
    this.no = idx + 1;               // 座位号 1..12
    this.name = NAMES[idx];
    this.avatar = AVATARS[idx];
    this.personality = PERSONALITIES[idx];
    this.role = null;                // wolf | seer | witch | hunter | guard | villager
    this.alive = true;

    // 角色私人信息
    this.knownWolves = [];           // 狼人队友
    this.seerChecks = [];            // [{target, isWolf, day}]
    this.witchHasSave = true;
    this.witchHasPoison = true;
    this.lastGuarded = null;         // 守卫上一晚保护对象

    // 推理与公开状态
    this.suspicion = new Array(12).fill(0); // 0~1, 高=像狼
    this.claims = {};                       // idx -> claim (例: 预言家)
    this.checkReports = [];                 // 公开声明的查验
    this.hasClaimed = false;                // 是否跳过身份
    this.publicRole = null;                 // 自己对外声称的身份
    this.isSheriff = false;
    this._intendsToRunSheriff = false;
    // 私有思考日记（不公开给其他 agent，仅用于自己跨轮策略一致）
    this.thinkingLog = [];                  // [{day, kind, thinking}]
    // 本局持久化记忆：原始数据（他人发言 + 自己行动），写盘到 memory/agent-N.md 供人工 review
    this.memoryByDay = {};                  // { day: { otherSpeeches: [...], myActions: [...] } }
    // 本局压缩记忆：每天 ≤80 字摘要，是 prompt 实际用到的"激活记忆"，避免上下文爆掉
    this.memoryDigestByDay = {};            // { day: "今日摘要..." }
  }

  reset() {
    this.alive = true;
    this.knownWolves = [];
    this.seerChecks = [];
    this.witchHasSave = true;
    this.witchHasPoison = true;
    this.lastGuarded = null;
    this.suspicion = new Array(12).fill(0);
    this.claims = {};
    this.checkReports = [];
    this.hasClaimed = false;
    this.publicRole = null;
    this.isSheriff = false;
    this._intendsToRunSheriff = false;
    this.thinkingLog = [];
    this.memoryByDay = {};
    this.memoryDigestByDay = {};
  }

  /* ============ 推理 ============ */
  observe(event, game) {
    // 根据事件更新怀疑度
    switch (event.type) {
      case "check-report": {
        const reporter = game.agents[event.from];
        const target = event.target;
        const result = event.result; // 'wolf' | 'good'
        if (this.role === "wolf" && this.knownWolves.includes(event.from)) break;
        if (this.role === "seer" && this.idx === event.from) break;

        if (this.role === "wolf") {
          // 真预言家对自己阵营查杀=对方真预言家
          if (result === "wolf" && this.knownWolves.includes(target)) {
            this.suspicion[event.from] = Math.max(this.suspicion[event.from], 0.95);
          }
        } else {
          // 好人视角
          const seerClaimers = Object.entries(this.claims)
            .filter(([k, v]) => v === "seer").map(([k]) => parseInt(k));
          if (seerClaimers.length <= 1) {
            // 单跳预言家 → 高度信任
            if (result === "wolf") {
              this.suspicion[target] = Math.min(1, this.suspicion[target] + 0.7);
            } else {
              this.suspicion[target] = Math.max(0, this.suspicion[target] - 0.4);
            }
          } else {
            // 双跳：跟随自己更信任的那个，对另一个加怀疑
            if (result === "wolf") {
              this.suspicion[target] = Math.min(1, this.suspicion[target] + 0.35);
            } else {
              this.suspicion[target] = Math.max(0, this.suspicion[target] - 0.2);
            }
            // 两个跳预言家中必有一狼，对没说话过我相信的一方加怀疑度
            const otherClaimer = seerClaimers.find(k => k !== event.from);
            if (otherClaimer !== undefined) {
              this.suspicion[otherClaimer] = Math.min(1, this.suspicion[otherClaimer] + 0.18);
            }
            this.suspicion[event.from] = Math.min(1, this.suspicion[event.from] + 0.1);
          }
        }
        break;
      }
      case "claim": {
        // 跳预言家 → 标记
        const c = event.role;
        const prev = this.claims[event.from];
        if (prev && prev !== c) {
          // 改口 → 增加怀疑
          this.suspicion[event.from] = Math.min(1, this.suspicion[event.from] + 0.2);
        }
        this.claims[event.from] = c;
        break;
      }
      case "speech-suspect": {
        if (event.from === this.idx) break;
        // 别人指控的方向
        if (this.role === "wolf" && this.knownWolves.includes(event.target)) {
          this.suspicion[event.from] = Math.min(1, this.suspicion[event.from] + 0.05);
        } else {
          this.suspicion[event.target] = Math.min(1, this.suspicion[event.target] + 0.08);
        }
        break;
      }
      case "vote": {
        // 票型分析（简单）
        if (this.role === "wolf") break;
        const targetIsClaimedGod = ["seer","witch","guard","hunter"].includes(this.claims[event.target]);
        if (targetIsClaimedGod) {
          this.suspicion[event.from] = Math.min(1, this.suspicion[event.from] + 0.1);
        }
        break;
      }
      case "death-night": {
        // 夜里被刀的多半是神/预言家
        if (this.claims[event.target]) {
          // 被刀的人之前自爆身份，证实其身份概率
        }
        break;
      }
    }
  }

  /* ============ 夜晚行动 ============ */
  async nightAction(game) {
    if (!this.alive) return null;
    switch (this.role) {
      case "wolf":  return await this._wolfKill(game);
      case "seer":  return await this._seerCheck(game);
      case "witch": return await this._witchAct(game);
      case "guard": return await this._guardProtect(game);
      default: return null;
    }
  }

  async _wolfKill(game) {
    // ── LLM 决策优先 ──
    const llmRes = await LLM.decide({
      agent: this, game, kind: "wolf-kill",
      context: this._publicContext(game),
    });
    if (llmRes && Number.isInteger(llmRes.target)) {
      const idx = llmRes.target - 1;  // 座位号 1-12 → idx 0-11
      if (idx >= 0 && idx < 12 &&
          game.agents[idx].alive &&
          !this.knownWolves.includes(idx) &&
          idx !== this.idx) {
        return { type: "wolf-kill", target: idx };
      }
    }
    // ── 规则版回退 ──
    return this._ruleWolfKill(game);
  }

  _ruleWolfKill(game) {
    // 狼队优先刀神，其次刀疑似预言家、强发言者
    const alive = game.aliveAgents().filter(a => !this.knownWolves.includes(a.idx) && a.idx !== this.idx);
    if (alive.length === 0) return null;  // 边界：场上只剩本狼
    let scored = alive.map(a => {
      let s = 0;
      if (a.publicRole === "seer") s += 100;
      if (a.publicRole === "witch") s += 40;
      if (a.publicRole === "guard") s += 35;
      if (a.publicRole === "hunter") s -= 10; // 猎人反伤，斟酌
      if (a.isSheriff) s += 30;
      s += a.personality.talkative * 15;
      s += (Math.random() - 0.5) * 10;
      return { a, s };
    });
    scored.sort((x,y) => y.s - x.s);
    return { type: "wolf-kill", target: scored[0].a.idx };
  }

  async _seerCheck(game) {
    const checked = new Set(this.seerChecks.map(c => c.target));
    // ── LLM 决策优先 ──
    const llmRes = await LLM.decide({
      agent: this, game, kind: "seer-check",
      context: this._publicContext(game),
    });
    if (llmRes && Number.isInteger(llmRes.target)) {
      const idx = llmRes.target - 1;
      if (idx >= 0 && idx < 12 &&
          game.agents[idx].alive &&
          idx !== this.idx &&
          !checked.has(idx)) {
        return { type: "seer-check", target: idx };
      }
    }
    return this._ruleSeerCheck(game);
  }

  _ruleSeerCheck(game) {
    const candidates = game.aliveAgents().filter(a => a.idx !== this.idx && !this.seerChecks.some(c => c.target === a.idx));
    if (candidates.length === 0) return null;
    // 选发言激进 / 怀疑度高的
    let scored = candidates.map(a => ({
      a,
      s: this.suspicion[a.idx] * 100 + a.personality.aggro * 20 + (Math.random() - 0.5) * 30
    }));
    scored.sort((x,y) => y.s - x.s);
    return { type: "seer-check", target: scored[0].a.idx };
  }

  async _witchAct(game) {
    const actions = [];

    // ── 救：先问 LLM，无效或异常则用规则 ──
    if (this.witchHasSave && game.tonightKill !== null) {
      const killed = game.tonightKill;
      const isSelf = (killed === this.idx);
      let save = null;
      const llmSave = await LLM.decide({
        agent: this, game, kind: "witch-save",
        context: this._publicContext(game),
        options: { killedNo: game.agents[killed].no, isSelf, day: game.day },
      });
      if (llmSave && typeof llmSave.save === "boolean") {
        save = llmSave.save;
        // 第 2 夜起不能自救
        if (save && isSelf && game.day > 1) save = false;
      }
      if (save === null) {
        // 规则回退
        if (game.day === 1) save = true;
        else if (!isSelf && this._isProbablyGod(killed, game)) save = true;
        else save = false;
      }
      if (save) actions.push({ type: "witch-save", target: killed });
    }

    // ── 毒：第 2 夜起 ──
    if (this.witchHasPoison && game.day >= 2) {
      const alive = game.aliveAgents().filter(a => a.idx !== this.idx);
      let poisonTarget = null;
      let llmHandled = false;   // LLM 是否给出了有效决策（含"明确不毒"）
      const llmPoison = await LLM.decide({
        agent: this, game, kind: "witch-poison",
        context: this._publicContext(game),
      });
      if (llmPoison && Number.isInteger(llmPoison.target)) {
        if (llmPoison.target === -1) {
          llmHandled = true;   // 明确不毒
        } else if (llmPoison.target > 0) {
          const idx = llmPoison.target - 1;
          if (idx >= 0 && idx < 12 && game.agents[idx].alive && idx !== this.idx) {
            poisonTarget = idx;
            llmHandled = true;
          }
          // 否则视为 LLM 异常（目标已死/越界），不算 handled → 走规则
        }
      }
      if (!llmHandled) {
        // 规则回退
        const seerClaims = alive.filter(a => a.publicRole === "seer");
        if (seerClaims.length >= 2) {
          const t = seerClaims.reduce((best, a) =>
            this.suspicion[a.idx] > this.suspicion[best.idx] ? a : best, seerClaims[0]);
          poisonTarget = t.idx;
        } else {
          const t = alive.reduce((best, a) =>
            this.suspicion[a.idx] > this.suspicion[best.idx] ? a : best, alive[0]);
          if (this.suspicion[t.idx] > 0.45) poisonTarget = t.idx;
        }
      }
      if (poisonTarget !== null) {
        actions.push({ type: "witch-poison", target: poisonTarget });
      }
    }
    return actions.length ? actions : null;
  }

  _isProbablyGod(idx, game) {
    const c = game.agents[idx].publicRole;
    if (c === "seer") return true;
    if (c === "witch" || c === "guard" || c === "hunter") return true;
    // 没跳身份但发言激进 也可能是神
    return false;
  }

  async _guardProtect(game) {
    // ── LLM 决策优先 ──
    const ctx = this._publicContext(game);
    if (this.lastGuarded !== null) ctx.me.lastGuardedNo = game.agents[this.lastGuarded].no;
    const llmRes = await LLM.decide({
      agent: this, game, kind: "guard-protect",
      context: ctx,
    });
    if (llmRes && Number.isInteger(llmRes.target)) {
      const idx = llmRes.target - 1;
      if (idx >= 0 && idx < 12 &&
          game.agents[idx].alive &&
          idx !== this.lastGuarded) {
        return { type: "guard-protect", target: idx };
      }
    }
    return this._ruleGuardProtect(game);
  }

  _ruleGuardProtect(game) {
    // 优先守预言家、其次守自己（但不能连守）
    const alive = game.aliveAgents();
    // 先尝试守已跳预言家
    const seer = alive.find(a => a.publicRole === "seer" && a.idx !== this.lastGuarded);
    if (seer && Math.random() < 0.75) return { type: "guard-protect", target: seer.idx };

    // 守自己（不能连守）
    if (this.lastGuarded !== this.idx && Math.random() < 0.55) {
      return { type: "guard-protect", target: this.idx };
    }
    // 守怀疑度低的发言者
    const candidates = alive.filter(a => a.idx !== this.lastGuarded);
    const target = candidates.reduce((best, a) =>
      (1 - this.suspicion[a.idx]) + a.personality.talkative > (1 - this.suspicion[best.idx]) + best.personality.talkative
        ? a : best, candidates[0]);
    return { type: "guard-protect", target: target.idx };
  }

  /* ============ 白天发言 ============ */
  async generateSpeech(game) {
    // LLM 钩子优先
    const llmText = await LLM.speak({
      agent: this, game, kind: "day",
      context: this._publicContext(game),
    });
    if (llmText) return llmText;
    return this._ruleSpeech(game);
  }

  _ruleSpeech(game) {
    const day = game.day;
    const alive = game.aliveAgents();
    const lines = [];

    // 1) 角色策略：是否跳身份
    let intent = "stay";  // stay/claim/counter-claim/vote
    let claimRole = null;
    let chartedSuspect = null;

    if (this.role === "seer" && this.seerChecks.length > 0) {
      // 默认每天复述查验
      intent = "claim"; claimRole = "seer";
    } else if (this.role === "wolf") {
      // 狼人策略：场上无任何预言家声称、且本狼队尚无悍跳时才考虑
      const seerClaimed = game.agents.some(a => a.alive && a.publicRole === "seer");
      const teamAlreadyClaimed = this.knownWolves.some(w => game.agents[w].publicRole === "seer");
      if (day === 1 && !seerClaimed && !teamAlreadyClaimed && Math.random() < 0.45 * this.personality.deception) {
        intent = "claim"; claimRole = "seer";
      }
    } else if (this.role === "witch" && this.publicRole === null) {
      if (day >= 2 && Math.random() < 0.25) {
        intent = "claim"; claimRole = "witch";
      }
    } else if (this.role === "guard" && this.publicRole === null) {
      if (day >= 3 && Math.random() < 0.2) {
        intent = "claim"; claimRole = "guard";
      }
    } else if (this.role === "hunter" && this.publicRole === null) {
      if (day >= 3 && Math.random() < 0.25) {
        intent = "claim"; claimRole = "hunter";
      }
    }

    // 2) 找出怀疑对象
    const candidates = alive.filter(a => a.idx !== this.idx);
    const sorted = candidates.slice().sort((a,b) => this.suspicion[b.idx] - this.suspicion[a.idx]);
    chartedSuspect = sorted[0];

    // 狼人会指向好人（远离队友）
    if (this.role === "wolf") {
      const targets = candidates.filter(a => !this.knownWolves.includes(a.idx));
      // 优先指向跳预言家但不是真预言家的（即对方真预言家）
      const seerClaims = targets.filter(a => a.publicRole === "seer");
      if (seerClaims.length > 0) {
        chartedSuspect = seerClaims[Math.floor(Math.random() * seerClaims.length)];
      } else {
        chartedSuspect = targets[Math.floor(Math.random() * targets.length)];
      }
    }

    // 3) 拼接发言
    // 开场
    lines.push(pick([
      `各位，我是${this.no}号。`,
      `好，${this.no}号发言。`,
      `${this.no}号在此，听我一言。`,
      `轮到我了，简单说。`,
      `${this.no}号，整理一下场上信息。`,
    ]));

    // 跳身份
    if (intent === "claim") {
      this.hasClaimed = true;
      this.publicRole = claimRole;
      if (claimRole === "seer") {
        // 真假预言家都会报查验
        let checkTarget, result;
        if (this.role === "seer" && this.seerChecks.length > 0) {
          const lastCheck = this.seerChecks[this.seerChecks.length - 1];
          checkTarget = lastCheck.target;
          result = lastCheck.isWolf ? "wolf" : "good";
        } else {
          // 狼人悍跳：随机一个查杀好人
          const goodCands = candidates.filter(a => !this.knownWolves.includes(a.idx));
          checkTarget = goodCands[Math.floor(Math.random() * goodCands.length)].idx;
          result = "wolf";
        }
        const targetAgent = game.agents[checkTarget];
        if (result === "wolf") {
          lines.push(pick([
            `我是预言家，昨晚验的是 ${targetAgent.no} 号 — 查杀！`,
            `不藏了，预言家在此。${targetAgent.no} 号，狼人，明牌。`,
            `我跳预言家，${targetAgent.no} 号查杀，请大家跟我对位。`,
          ]));
        } else {
          lines.push(pick([
            `我是预言家，验的 ${targetAgent.no} 号 — 金水。`,
            `预言家报告：${targetAgent.no} 号是好人，请好人围拢。`,
          ]));
        }
        game.fireEvent({ type: "claim", from: this.idx, role: "seer" });
        game.fireEvent({ type: "check-report", from: this.idx, target: checkTarget, result });
      } else if (claimRole === "witch") {
        lines.push(pick([
          `我女巫站出来了，昨夜情况我清楚。`,
          `我是女巫，今天必须给信息了。`,
        ]));
        game.fireEvent({ type: "claim", from: this.idx, role: "witch" });
      } else if (claimRole === "guard") {
        lines.push(pick([
          `我守卫站出来澄清。`,
          `守卫在此，昨晚我守过人，下面解释。`,
        ]));
        game.fireEvent({ type: "claim", from: this.idx, role: "guard" });
      } else if (claimRole === "hunter") {
        lines.push(pick([
          `我猎人，狼人别碰我。`,
          `这局猎人是我，谁想试试我的枪？`,
        ]));
        game.fireEvent({ type: "claim", from: this.idx, role: "hunter" });
      }
    }

    // 指控
    if (chartedSuspect) {
      const reason = this._suspicionReason(chartedSuspect, game);
      lines.push(pick([
        `我重点怀疑 ${chartedSuspect.no} 号，${reason}。`,
        `${chartedSuspect.no} 号给我感觉很狼，${reason}。`,
        `站边 ${chartedSuspect.no} 号是狼，理由：${reason}。`,
        `${chartedSuspect.no} 号那波操作不对，${reason}。`,
      ]));
      game.fireEvent({ type: "speech-suspect", from: this.idx, target: chartedSuspect.idx });
    }

    // 站边/补刀
    if (this.role === "seer" && this.seerChecks.length > 0) {
      const last = this.seerChecks[this.seerChecks.length - 1];
      if (last.isWolf) {
        lines.push(`${game.agents[last.target].no} 号狼坑实锤，今天必须把他抬出去。`);
      } else {
        lines.push(`${game.agents[last.target].no} 号是金水，让他帮我站边。`);
      }
    } else if (this.role === "wolf" && this.hasClaimed && this.publicRole === "seer") {
      // 强调自己是真预言家
      lines.push(pick([
        `我才是真预言家，对面是悍跳狼。`,
        `请好人对位投我对面，今天必须把狼带走。`,
      ]));
    }

    // 收尾
    if (this.personality.talkative > 0.7) {
      lines.push(pick([
        `话说完了，过。`,
        `就这些，下一位。`,
        `今天投票别飘，跟我节奏。`,
        `我说完了，谁有问题站起来怼。`,
      ]));
    } else {
      lines.push(pick([`说完。`, `过。`, `就这些。`]));
    }

    return lines.join(" ");
  }

  _suspicionReason(target, game) {
    const reasons = [
      "发言飘忽躲位置",
      "首轮过于沉默",
      "对预言家态度暧昧",
      "票型怪异",
      "频繁打压神职",
      "气势上像悍跳",
      "夜里没死还在带节奏",
      "对位逻辑混乱",
      "情绪比信息多",
    ];
    return reasons[Math.floor(Math.random() * reasons.length)];
  }

  /* ============ 投票 ============ */
  async voteTarget(game, candidates) {
    if (!this.alive) return -1;
    // ── LLM 决策优先 ──
    const llmRes = await LLM.decide({
      agent: this, game, kind: "vote",
      context: this._publicContext(game),
      options: { candidates: candidates.map(c => c.no) },
    });
    if (llmRes && Number.isInteger(llmRes.target)) {
      if (llmRes.target === -1) return -1; // 弃票
      const idx = llmRes.target - 1;
      if (idx >= 0 && idx < 12 && candidates.some(c => c.idx === idx)) {
        return idx;
      }
    }
    return this._ruleVoteTarget(game, candidates);
  }

  _ruleVoteTarget(game, candidates) {
    if (!this.alive) return -1;
    // 狼人：投跳预言家最强威胁，避免投队友
    if (this.role === "wolf") {
      const enemies = candidates.filter(a => !this.knownWolves.includes(a.idx) && a.idx !== this.idx);
      const seer = enemies.filter(a => a.publicRole === "seer");
      if (seer.length > 0) return seer[Math.floor(Math.random() * seer.length)].idx;
      const ordered = enemies.slice().sort((a,b) => b.personality.talkative - a.personality.talkative);
      return ordered[0]?.idx ?? candidates[0].idx;
    }
    // 好人：投查杀目标，或最高怀疑度
    const checkKill = this._lastCheckKillTarget(game);
    if (checkKill !== null && candidates.some(a => a.idx === checkKill)) return checkKill;

    const ordered = candidates.filter(a => a.idx !== this.idx)
      .sort((a,b) => this.suspicion[b.idx] - this.suspicion[a.idx]);
    if (ordered.length === 0) return -1;  // 弃票
    if (ordered[0] && this.suspicion[ordered[0].idx] < 0.15) {
      // 没目标 → 弃票（用-1表示）
      return Math.random() < 0.35 ? -1 : ordered[0].idx;
    }
    return ordered[0].idx;
  }

  /* ============ 警长竞选 ============ */
  async decideRunForSheriff(game) {
    if (!this.alive) return false;
    // ── LLM 决策优先 ──
    const llmRes = await LLM.decide({
      agent: this, game, kind: "run-sheriff",
      context: this._publicContext(game),
    });
    if (llmRes && typeof llmRes.run === "boolean") {
      this._intendsToRunSheriff = llmRes.run;
      return llmRes.run;
    }
    return this._ruleDecideRunForSheriff(game);
  }

  _ruleDecideRunForSheriff(game) {
    if (this.role === "seer") return true;                          // 预言家必上
    if (this.role === "wolf") {
      // 狼队：1-2 只悍跳 / 上警混入
      const wolfUpCount = this.knownWolves.filter(w => game.agents[w]._intendsToRunSheriff).length;
      const willRun = wolfUpCount < 2 && Math.random() < 0.45 * this.personality.deception;
      this._intendsToRunSheriff = willRun;
      return willRun;
    }
    if (this.role === "witch")   return Math.random() < 0.25;       // 女巫通常不上警，避免暴露
    if (this.role === "guard")   return Math.random() < 0.30;
    if (this.role === "hunter")  return Math.random() < 0.45;       // 猎人偶尔上
    return Math.random() < 0.30 * (0.5 + this.personality.talkative);
  }

  async sheriffVote(game, runners) {
    if (!this.alive) return -1;
    // ── LLM 决策优先 ──
    const llmRes = await LLM.decide({
      agent: this, game, kind: "sheriff-vote",
      context: this._publicContext(game),
      options: { candidates: runners.map(r => r.no) },
    });
    if (llmRes && Number.isInteger(llmRes.target)) {
      if (llmRes.target === -1) return -1;
      const idx = llmRes.target - 1;
      if (idx >= 0 && idx < 12 && runners.some(r => r.idx === idx)) {
        return idx;
      }
    }
    return this._ruleSheriffVote(game, runners);
  }

  _ruleSheriffVote(game, runners) {
    if (!this.alive) return -1;
    // 狼人优先投自己队友
    if (this.role === "wolf") {
      const teammates = runners.filter(r => this.knownWolves.includes(r.idx));
      if (teammates.length > 0) {
        return teammates[Math.floor(Math.random() * teammates.length)].idx;
      }
      // 没队友上警 → 投发言最强的好人（拉低其威望反不利，故选随机弱点）
      const goods = runners.filter(r => r.idx !== this.idx);
      return goods.length ? goods[Math.floor(Math.random() * goods.length)].idx : -1;
    }
    // 好人逻辑：跳预言家的优先投 ；其次按怀疑度（低=更可信）+ 性格强度
    const seers = runners.filter(r => r.publicRole === "seer");
    if (seers.length === 1) return seers[0].idx;
    if (seers.length > 1) {
      // 双跳预言家上警：相信怀疑度较低的
      const trusted = seers.slice().sort((a,b) => this.suspicion[a.idx] - this.suspicion[b.idx]);
      return trusted[0].idx;
    }
    // 没人跳预言家：按怀疑度+话痨度选
    const sorted = runners.filter(r => r.idx !== this.idx)
      .sort((a, b) => {
        const sa = this.suspicion[a.idx] - a.personality.talkative * 0.2;
        const sb = this.suspicion[b.idx] - b.personality.talkative * 0.2;
        return sa - sb;
      });
    return sorted[0]?.idx ?? -1;
  }

  async generateSheriffSpeech(game) {
    const llmText = await LLM.speak({
      agent: this, game, kind: "sheriff",
      context: this._publicContext(game),
    });
    if (llmText) return llmText;
    return this._ruleSheriffSpeech(game);
  }

  _ruleSheriffSpeech(game) {
    const lines = [];
    lines.push(`${this.no}号 ${this.name}，上警。`);

    if (this.role === "seer") {
      // 预言家上警必跳，并报昨夜查验
      this.hasClaimed = true;
      this.publicRole = "seer";
      const last = this.seerChecks[this.seerChecks.length - 1];
      if (last) {
        const t = game.agents[last.target];
        if (last.isWolf) {
          lines.push(`我预言家，昨晚验 ${t.no} 号 — 查杀！警徽必须给我。`);
        } else {
          lines.push(`我预言家，昨晚验 ${t.no} 号 — 金水。请好人对位。`);
        }
        game.fireEvent({ type: "claim", from: this.idx, role: "seer" });
        game.fireEvent({ type: "check-report", from: this.idx, target: last.target, result: last.isWolf ? "wolf" : "good" });
      } else {
        lines.push(`我预言家，警徽给我能稳住节奏。`);
        game.fireEvent({ type: "claim", from: this.idx, role: "seer" });
      }
    } else if (this.role === "wolf") {
      // 狼人上警：若真预言家已先发言跳明，避免再跳（被对位查杀概率大）
      const realSeerClaimed = game.agents.some(a =>
        a.alive && a.publicRole === "seer" && !this.knownWolves.includes(a.idx));
      const teamAlreadyClaimed = this.knownWolves.some(w => game.agents[w].publicRole === "seer");
      if (!teamAlreadyClaimed && !realSeerClaimed && Math.random() < 0.45 * this.personality.deception) {
        // 悍跳
        this.hasClaimed = true;
        this.publicRole = "seer";
        const goodCands = game.aliveAgents().filter(a => !this.knownWolves.includes(a.idx) && a.idx !== this.idx);
        const t = goodCands[Math.floor(Math.random() * goodCands.length)];
        lines.push(`我预言家，昨晚验 ${t.no} 号 — 查杀！请大家相信我。`);
        game.fireEvent({ type: "claim", from: this.idx, role: "seer" });
        game.fireEvent({ type: "check-report", from: this.idx, target: t.idx, result: "wolf" });
      } else {
        lines.push(pick([
          `我民牌，但我有节奏感，警徽给我。`,
          `我没特别身份，但我能控票型，相信我。`,
          `场上谁跳预言家我跟，警徽我能稳。`,
        ]));
      }
    } else if (this.role === "witch") {
      lines.push(pick([`我是好人，警徽我能站边。`, `我不亮身份，但我能稳住场子。`]));
    } else if (this.role === "guard" || this.role === "hunter") {
      lines.push(pick([`我民牌，能带节奏。`, `我比对面更敢站，警徽给我。`]));
    } else {
      lines.push(pick([
        `我是民，但我看场上清楚。`,
        `给我警徽，我能带好节奏。`,
        `相信我，不会让狼人翻盘。`,
      ]));
    }
    return lines.join(" ");
  }

  /* ============ 狼人自爆决策 ============ */
  async decideSelfExplode(game) {
    if (this.role !== "wolf") return false;
    if (!this.alive) return false;
    if (game.wolfHasExploded) return false;
    const aliveWolves = game.aliveOf("wolf").length;
    if (aliveWolves <= 1) return false;              // 最后一狼不自爆
    if (game.day === 1 && game.aliveAgents().length === 12) return false; // 平安夜首日不太可能

    // ── LLM 决策优先 ──
    const llmRes = await LLM.decide({
      agent: this, game, kind: "self-explode",
      context: this._publicContext(game),
    });
    if (llmRes && typeof llmRes.explode === "boolean") {
      return llmRes.explode;
    }
    return this._ruleDecideSelfExplode(game);
  }

  _ruleDecideSelfExplode(game) {

    // 评估场上危险度
    let danger = 0;
    // 1) 队友被预言家查杀
    const teammateChecked = game.publicCheckReports.some(r =>
      r.result === "wolf" && this.knownWolves.includes(r.target) && game.agents[r.target].alive
    );
    if (teammateChecked) danger += 0.55;
    // 2) 自己被预言家查杀
    const selfChecked = game.publicCheckReports.some(r =>
      r.result === "wolf" && r.target === this.idx
    );
    if (selfChecked) danger += 0.5;
    // 3) 真预言家明牌（非己方悍跳） — 想破坏对方节奏
    const goodSeer = game.agents.some(a =>
      a.alive && a.publicRole === "seer" && !this.knownWolves.includes(a.idx)
    );
    if (goodSeer) danger += 0.15;

    // 性格调节（每发言一次只检查一次，整局期望命中 ≈ 10-20%）
    const baseProb = Math.min(0.45, danger * (0.2 + this.personality.aggro * 0.3));
    return Math.random() < baseProb;
  }

  /* ============ 警徽传承决策 ============ */
  async passBadge(game) {
    const alive = game.aliveAgents().filter(a => a.idx !== this.idx);
    if (alive.length === 0) return -1;
    // ── LLM 决策优先 ──
    const llmRes = await LLM.decide({
      agent: this, game, kind: "pass-badge",
      context: this._publicContext(game),
      options: { candidates: alive.map(a => a.no) },
    });
    if (llmRes && Number.isInteger(llmRes.target)) {
      if (llmRes.target === -1) return -1;
      const idx = llmRes.target - 1;
      if (idx >= 0 && idx < 12 && game.agents[idx].alive && idx !== this.idx) {
        return idx;
      }
    }
    return this._rulePassBadge(game);
  }

  _rulePassBadge(game) {
    const alive = game.aliveAgents().filter(a => a.idx !== this.idx);
    if (alive.length === 0) return -1;
    if (this.role === "wolf") {
      // 狼警长被刀/被投：传给队友最优；没队友则撕毁
      const teammates = alive.filter(a => this.knownWolves.includes(a.idx));
      if (teammates.length > 0 && Math.random() < 0.7) {
        return teammates[Math.floor(Math.random() * teammates.length)].idx;
      }
      return -1; // 撕毁
    }
    // 好人警长优先级：
    //   1) 自己查验过的"金水"
    //   2) 跳预言家且自己怀疑度低的
    //   3) 怀疑度最低的活人（要排除我自己已经怀疑很高的）
    if (this.role === "seer") {
      const goldWaters = this.seerChecks
        .filter(c => !c.isWolf && game.agents[c.target].alive && c.target !== this.idx)
        .map(c => c.target);
      if (goldWaters.length > 0) {
        // 选最新的金水
        return goldWaters[goldWaters.length - 1];
      }
    }
    const claimedSeers = alive.filter(a => a.publicRole === "seer" && this.suspicion[a.idx] < 0.5);
    if (claimedSeers.length > 0) {
      const trusted = claimedSeers.sort((a, b) => this.suspicion[a.idx] - this.suspicion[b.idx])[0];
      return trusted.idx;
    }
    const trustOrder = alive.slice().sort((a,b) => this.suspicion[a.idx] - this.suspicion[b.idx]);
    if (this.suspicion[trustOrder[0].idx] > 0.5) return -1;     // 谁都不放心 → 撕牌
    if (Math.random() < 0.15) return -1;                         // 谨慎好人偶尔撕毁
    return trustOrder[0].idx;
  }

  _lastCheckKillTarget(game) {
    // 好人会跟随真预言家的查杀
    const claimedSeers = game.agents.filter(a => a.alive && a.publicRole === "seer");
    if (claimedSeers.length === 0) return null;
    // 优先选择自己更信任的那位（怀疑度较低）
    const trusted = claimedSeers.sort((a,b) => this.suspicion[a.idx] - this.suspicion[b.idx])[0];
    const reports = game.publicCheckReports.filter(r => r.from === trusted.idx);
    const lastKill = reports.filter(r => r.result === "wolf").slice(-1)[0];
    if (lastKill && game.agents[lastKill.target].alive) return lastKill.target;
    return null;
  }
}

function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

// 让 LLM 决策时可用的轻量上下文快照（不含任何 UI / DOM 引用）
Agent.prototype._publicContext = function (game) {
  // 发言历史：当天所有 + 昨天遗言（让 LLM 能基于他人发言回应）
  const recentSpeeches = (game.speechHistory || [])
    .filter(s => s.day === game.day || (s.day === game.day - 1 && s.kind === "last-words"))
    .map(s => ({
      day: s.day, no: s.agentNo, publicRole: s.publicRole,
      kind: s.kind, text: s.text,
    }));

  return {
    day: game.day,
    phase: game.phase,
    me: {
      no: this.no, name: this.name, role: this.role, personality: this.personality.name,
      alive: this.alive, isSheriff: this.isSheriff,
      hasClaimed: this.hasClaimed,           // 自己是否已跳身份
      publicRole: this.publicRole,           // 自己对外声称的身份（null = 未跳）
      knownWolves: this.knownWolves.map(i => game.agents[i].no),
      seerChecks: this.seerChecks.map(c => ({ no: game.agents[c.target].no, isWolf: c.isWolf, day: c.day })),
      witchHasSave: this.witchHasSave, witchHasPoison: this.witchHasPoison,
      // V2.8：私有思考日记（最近 5 条），让 LLM 跨轮保持策略一致
      thinkingLog: this.thinkingLog.slice(-5),
      // 压缩记忆：最近 3 天的 ≤80 字摘要，喂给 prompt（避免上下文爆掉）
      // 原始 memoryByDay 仍存活在本对象上、写盘到 memory/agent-N.md，供人工 review 或工具按需读取
      recentMemoryDigests: Object.keys(this.memoryDigestByDay)
        .map(Number).sort((a, b) => a - b).slice(-3)
        .map(d => ({ day: d, digest: this.memoryDigestByDay[d] })),
    },
    players: game.agents.map(a => ({
      no: a.no, name: a.name, alive: a.alive,
      publicRole: a.publicRole,                    // 仅公开声明的身份
      isSheriff: a.isSheriff,
      mySuspicion: +this.suspicion[a.idx].toFixed(2),
    })),
    publicCheckReports: game.publicCheckReports.map(r => ({
      from: game.agents[r.from].no,
      target: game.agents[r.target].no,
      result: r.result,
    })),
    recentSpeeches,
    // V2.9-2：上轮复盘（事实 + LLM 推断），全场共享，让 agent 学习
    roundSummaries: (game.roundSummaries || []).slice(-2),
    sheriffElectionDone: game.sheriffElectionDone,
    wolfHasExploded: game.wolfHasExploded,
  };
};
