/* =============================================================
   Werewolf · Event Log
   ============================================================= */
/* 统一的游戏事件流，带 isPublic + visibleTo 视角过滤。
   设计目标：
   - 所有夜间动作（狼刀提议/确认、女巫救毒、守卫守护、预言家查验）
     与白天事件（发言、投票、处决、死亡公告）统一进 game.events
   - 每个事件自带 visibleTo(role, idx) 决定哪些 agent 能在 prompt 里看到
   - 给 wolves/witch/seer 在多日决策时回放自己阵营的私密历史，避免遗忘

   游戏现有的 speechHistory / publicCheckReports 仍保留（向下兼容
   memory.js / _publicContext 旧字段），events 是新增的"权威 + 可过滤"层。
*/

const Events = (function () {

  class GameEvent {
    constructor(type, day, phase) {
      this.type = type;
      this.day = day;
      this.phase = phase;
      this.ts = Date.now();
    }
    // 默认全场可见，子类按需 override
    visibleTo(role, idx) { return true; }
    // 序列化给 prompt / replay 使用（去掉方法，保留数据字段）
    toJSON() {
      const out = {};
      for (const k of Object.keys(this)) out[k] = this[k];
      return out;
    }
  }

  // ── 公开 ──────────────────────────────────────
  class SpeakEvent extends GameEvent {
    constructor(day, phase, agentNo, publicRole, kind, text) {
      super("speak", day, phase);
      Object.assign(this, { agentNo, publicRole, kind, text });
    }
  }
  class VoteEvent extends GameEvent {
    constructor(day, fromNo, targetNo, kind) {
      super("vote", day, "day");
      Object.assign(this, { fromNo, targetNo, kind });  // kind: vote | pk-vote | sheriff-vote
    }
  }
  class ExecuteEvent extends GameEvent {
    constructor(day, targetNo, reason) {
      super("execute", day, "day");
      Object.assign(this, { targetNo, reason });  // reason: vote | pk
    }
  }
  // 死亡公告（不含死因，所有人可见）
  class DeathEvent extends GameEvent {
    constructor(day, phase, targetNo) {
      super("death", day, phase);
      this.targetNo = targetNo;
    }
  }
  class SeerReportEvent extends GameEvent {
    constructor(day, fromNo, targetNo, result) {
      super("seer-report", day, "day");
      Object.assign(this, { fromNo, targetNo, result });
    }
  }
  class WolfExplodeEvent extends GameEvent {
    constructor(day, wolfNo) {
      super("wolf-explode", day, "day");
      this.wolfNo = wolfNo;
    }
  }
  class SheriffElectedEvent extends GameEvent {
    constructor(day, sheriffNo) {
      super("sheriff-elected", day, "day");
      this.sheriffNo = sheriffNo;
    }
  }
  class BadgePassEvent extends GameEvent {
    constructor(day, fromNo, toNo) {
      super("badge-pass", day, "day");
      Object.assign(this, { fromNo, toNo });  // toNo = -1 表示撕毁
    }
  }

  // ── 仅狼可见 ──────────────────────────────────
  class WolfProposeKillEvent extends GameEvent {
    constructor(day, wolfNo, targetNo, thinking, round) {
      super("wolf-propose-kill", day, "night");
      Object.assign(this, { wolfNo, targetNo, thinking, round });
    }
    visibleTo(role) { return role === "wolf"; }
  }
  class WolfKillEvent extends GameEvent {
    // 狼人最终敲定的击杀目标（含死因，仅狼可见）
    constructor(day, targetNo) {
      super("wolf-kill", day, "night");
      this.targetNo = targetNo;
    }
    visibleTo(role) { return role === "wolf"; }
  }

  // ── 仅本人可见 ────────────────────────────────
  class SeerCheckEvent extends GameEvent {
    constructor(day, seerIdx, seerNo, targetNo, isWolf) {
      super("seer-check", day, "night");
      Object.assign(this, { seerIdx, seerNo, targetNo, isWolf });
    }
    visibleTo(role, idx) { return idx === this.seerIdx; }
  }
  class WitchSaveEvent extends GameEvent {
    constructor(day, witchIdx, targetNo) {
      super("witch-save", day, "night");
      Object.assign(this, { witchIdx, targetNo });
    }
    visibleTo(role, idx) { return idx === this.witchIdx; }
  }
  class WitchPoisonEvent extends GameEvent {
    constructor(day, witchIdx, targetNo) {
      super("witch-poison", day, "night");
      Object.assign(this, { witchIdx, targetNo });
    }
    visibleTo(role, idx) { return idx === this.witchIdx; }
  }
  class GuardProtectEvent extends GameEvent {
    constructor(day, guardIdx, targetNo) {
      super("guard-protect", day, "night");
      Object.assign(this, { guardIdx, targetNo });
    }
    visibleTo(role, idx) { return idx === this.guardIdx; }
  }

  // ── 辅助函数 ───────────────────────────────────
  function forAgent(events, agent) {
    return events.filter(e => e.visibleTo(agent.role, agent.idx));
  }

  // 渲染狼人专属夜晚日志，给 wolf prompt 使用
  function renderWolfNightLog(events, currentDay) {
    const wolfEvents = events.filter(e =>
      (e.type === "wolf-propose-kill" || e.type === "wolf-kill" || e.type === "death")
      && e.day < currentDay
    );
    if (wolfEvents.length === 0) return "";
    const byDay = {};
    wolfEvents.forEach(e => {
      if (!byDay[e.day]) byDay[e.day] = [];
      byDay[e.day].push(e);
    });
    const lines = ["=== 狼队夜晚历史（仅你阵营可见）==="];
    Object.keys(byDay).map(Number).sort((a,b)=>a-b).forEach(d => {
      const dayEvents = byDay[d];
      const kill = dayEvents.find(e => e.type === "wolf-kill");
      const props = dayEvents.filter(e => e.type === "wolf-propose-kill" && e.round === 1);
      const death = dayEvents.find(e => e.type === "death");
      lines.push(`【第${d}夜】`);
      if (props.length) lines.push("  提议: " + props.map(p => `${p.wolfNo}→${p.targetNo === -1 ? "空刀" : p.targetNo + "号"}`).join("、"));
      if (kill) lines.push(`  终选刀: ${kill.targetNo === -1 ? "空刀（不杀）" : kill.targetNo + "号"}`);
      if (death) lines.push(`  实际结果: ${death.targetNo}号死亡`);
      else if (kill && kill.targetNo !== -1) lines.push(`  实际结果: 无人死亡（被守/被救）`);
    });
    lines.push("=== 狼队夜晚历史结束 ===");
    return lines.join("\n");
  }

  return {
    GameEvent,
    SpeakEvent, VoteEvent, ExecuteEvent, DeathEvent,
    SeerReportEvent, WolfExplodeEvent, SheriffElectedEvent, BadgePassEvent,
    WolfProposeKillEvent, WolfKillEvent,
    SeerCheckEvent, WitchSaveEvent, WitchPoisonEvent, GuardProtectEvent,
    forAgent,
    renderWolfNightLog,
  };
})();

if (typeof window !== "undefined") window.Events = Events;
