/* =============================================================
   Werewolf · Memory Layers (三层记忆)
   ============================================================= */
/* 对齐《狼人杀多Agent重构规格》第 2-3 节的三层记忆契约:

   ── GroundTruth (真相层) ──
     仅 Moderator (Game) 持有。包含全部身份、每晚真实动作、药剂数等。
     实质上就是 Game 实例自身,本文件不单独建类;靠
     "agent 不直接访问 game.X 私密字段" 来实现隔离。

   ── PublicLog (公共层) ──
     所有 agent 只读,Moderator 写。
     条目 frozen 防篡改,append() 内禁字段防御性检查:
     绝不允许 "cause" / "reason" 等死因字段进入 PublicLog。
     这是规格里的核心红线。

   ── PrivateMemory (私有层) ──
     每个玩家 agent 独占,物理隔离(无共享引用)。
     facts: append-only 事实流(查验结果、刀口、自身动作…)
     beliefs: seat → {suspicion, reason} —— 玩家增量更新的推理记忆
     teammates: 仅狼非空 —— 队友座位号列表
*/

const MemoryLayers = (function () {

  // PublicLog 禁字段(防止 Moderator 不小心把 GroundTruth 信息写进来)
  const PUBLIC_LOG_BANNED_KEYS = [
    "cause",          // 死因(规格核心红线)
    "reason",         // 推断 / 死因描述 —— 公开层只放事实,不放推理
    "killReason",     // 狼刀理由
    "poisonTarget",   // 毒杀对象
    "wolfThinking",   // 狼内心
    "witchPotion",    // 女巫药剂
    "seerResult",     // 预言家查验结果(除非是 seer-report 主动跳)
    "groundTruth",
    "_game",
    "_role",          // 私密身份
    "role",           // 真实身份(只允许 claim 用 'role',但 claim 路径有专用 type)
  ];

  function assertNoBannedKey(entry) {
    for (const k of PUBLIC_LOG_BANNED_KEYS) {
      if (k in entry) {
        throw new Error(`[PublicLog] 禁字段 "${k}" 不允许出现在公共日志条目里`);
      }
    }
  }

  // 深冻结(条目里可能嵌套对象)
  function deepFreeze(obj) {
    if (obj === null || typeof obj !== "object") return obj;
    Object.values(obj).forEach(deepFreeze);
    return Object.freeze(obj);
  }

  class PublicLog {
    constructor() {
      this.entries = [];
    }

    // 唯一写入入口,只接受白名单 type
    append(entry) {
      if (!entry || typeof entry !== "object") {
        throw new Error("[PublicLog] entry 必须是对象");
      }
      assertNoBannedKey(entry);
      deepFreeze(entry);
      this.entries.push(entry);
    }

    // 只读快照,给 agent 使用
    snapshot() {
      return this.entries.slice();
    }

    // 取最近 N 天的发言(公开)
    recentSpeeches(currentDay, days = 3) {
      const from = currentDay - days + 1;
      return this.entries.filter(e =>
        e.type === "speak" && e.day >= from
      );
    }

    // 取所有公开报查(seer 跳出来报的)
    publicChecks() {
      return this.entries.filter(e => e.type === "seer-report");
    }

    // 取按阶段(N1/D1/...)分组的死亡公告
    deathsByPhase() {
      const m = {};
      this.entries.filter(e => e.type === "death").forEach(e => {
        const key = `${e.phase === "night" ? "N" : "D"}${e.day}`;
        (m[key] = m[key] || []).push(e.targetNo);
      });
      return m;
    }

    // 全部公开跳身份(seat → role 序列, 按发生顺序)
    claims() {
      return this.entries.filter(e => e.type === "claim");
    }

    // 全部投票
    votes() {
      return this.entries.filter(e => e.type === "vote");
    }

    // 全部处决
    executes() {
      return this.entries.filter(e => e.type === "execute");
    }
  }

  /* ─── PrivateMemory ─────────────────────────────────────
     每个 agent 独占。Moderator 写 facts,agent 通过 LLM 更新 beliefs。
  */
  class PrivateMemory {
    constructor(seat, role = null) {
      this.seat = seat;
      this.role = role;
      this.teammates = [];     // 狼:队友座位号(no);其他角色为空
      this.facts = [];         // append-only,frozen
      this.beliefs = {};       // seat(no) -> {suspicion: number, reason: string, updatedAt: day}
    }

    setRole(role, teammates = []) {
      this.role = role;
      this.teammates = Array.isArray(teammates) ? teammates.slice() : [];
    }

    // append-only 事实流;fact 至少含 {day, type},frozen
    appendFact(fact) {
      if (!fact || typeof fact !== "object" || !fact.type) {
        throw new Error("[PrivateMemory] fact 必须含 type 字段");
      }
      const frozen = Object.freeze({ ...fact, _ts: Date.now() });
      this.facts.push(frozen);
    }

    // beliefs 增量更新:delta = { 5: {suspicion: 0.8, reason: "刀口太准"} }
    // 不接受整体重写;每条 delta 与原值 merge
    updateBeliefs(delta, day = 0) {
      if (!delta || typeof delta !== "object") return 0;
      let touched = 0;
      for (const seatStr of Object.keys(delta)) {
        const s = Number(seatStr);
        if (!Number.isInteger(s) || s < 1 || s > 12) continue;
        const d = delta[seatStr];
        if (!d || typeof d !== "object") continue;
        const prev = this.beliefs[s] || {};
        const next = { ...prev };
        let changed = false;
        if (typeof d.suspicion === "number" && d.suspicion >= 0 && d.suspicion <= 1) {
          next.suspicion = +d.suspicion.toFixed(2);
          changed = true;
        }
        if (typeof d.reason === "string" && d.reason.trim()) {
          next.reason = d.reason.trim().slice(0, 60);
          changed = true;
        }
        if (!changed) continue;       // 全部字段无效 → 跳过,不污染 beliefs
        next.updatedAt = day;
        this.beliefs[s] = next;
        touched++;
      }
      return touched;
    }

    // 取某 type 的 facts
    factsOfType(type) {
      return this.facts.filter(f => f.type === type);
    }

    // 取最近 N 条 facts
    recentFacts(n = 10) {
      return this.facts.slice(-n);
    }

    // 取 beliefs 排序后的简述(供 LLM 看)
    topSuspicions(n = 3) {
      return Object.entries(this.beliefs)
        .filter(([_, v]) => typeof v.suspicion === "number")
        .map(([seat, v]) => ({ seat: +seat, ...v }))
        .sort((a, b) => (b.suspicion ?? 0) - (a.suspicion ?? 0))
        .slice(0, n);
    }
  }

  return { PublicLog, PrivateMemory };
})();

if (typeof window !== "undefined") window.MemoryLayers = MemoryLayers;
if (typeof module !== "undefined") module.exports = MemoryLayers;
