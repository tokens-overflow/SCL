/* =============================================================
   Werewolf · Memory Module
   ============================================================= */
/* 独立的"个人记忆"模块，从 game.js 抽离。

   设计意图（避免 prompt 上下文随对局推进而爆掉）：
   - 双层存储：
     · 原始材料  agent.memoryByDay[day]       —— 他人发言 + 自己思考全文（写盘 + 内存）
     · 压缩摘要  agent.memoryDigestByDay[day] —— LLM 生成的 ≤80 字本日记忆（喂 prompt）
   - prompt 仅注入最近 N 天的摘要，原始材料不进 prompt，保持 token 增长有界
   - LLM 失败时走规则 fallback（按 suspicion 排"信任/怀疑"名单）
   - **准确性强化**（v2 修复）：
     · 狼人版 fallback 排除已知狼队友，避免摘要里写"信任 X 号"实际是队友
     · 让 LLM 看到昨日摘要做"承接"，避免每天独立摘要的人设漂移

   依赖（最小化）：
   - 全局 window.LLM_HOOK.summarize（由 llm-adapter.js 提供，失败时降级）
   - 全局 window.MEMORY.append/reset（由 llm-adapter.js 提供，写盘到后端 /memory）
   - agent 对象：no, idx, name, role, alive, personality, suspicion,
                 thinkingLog, knownWolves, memoryByDay, memoryDigestByDay

   暴露：
   - 全局 const Memory = { flushAll, recentDigests, renderPromptBlock, _internals }
*/

const Memory = (function () {

  // ── 配置 ──
  const ROLE_CN = { wolf: "狼人", seer: "预言家", witch: "女巫", hunter: "猎人", guard: "守卫", villager: "村民" };
  const KIND_CN = { day: "白天发言", sheriff: "上警发言", pk: "PK 发言", "last-words": "遗言" };
  const DIGEST_MAX_CHARS = 120;   // LLM 摘要硬截断（防越界，留 ≤80 字 + 容差）
  const RECENT_DAYS = 3;          // prompt 注入最近多少天 digest
  const TRUST_TOP_N = 2;          // fallback 摘要列出多少个信任 / 怀疑

  // ── 纯函数：从原始数据构造本日"原材料" ──
  function buildDayRaw(agent, day, speechHistory) {
    const speechesToday = (speechHistory || []).filter(s => s.day === day);
    const otherSpeeches = speechesToday
      .filter(s => s.agentNo !== agent.no)
      .map(s => ({
        no: s.agentNo,
        publicRole: s.publicRole || null,
        kind: s.kind || "day",
        text: s.text || "",
      }));
    const myActions = (agent.thinkingLog || []).filter(t => t.day === day);
    return { otherSpeeches, myActions };
  }

  // ── 纯函数：规则版 fallback 摘要 ──
  // 准确性强化：狼人视角排除队友（不暴露上帝视角），其他角色按 suspicion 排序
  function ruleFallbackDigest(agent, day, raw, allAgents) {
    const wolvesSet = new Set(agent.role === "wolf" ? (agent.knownWolves || []) : []);
    const susWithNo = agent.suspicion
      .map((s, i) => ({ no: i + 1, idx: i, s }))
      .filter(x =>
        x.idx !== agent.idx &&
        allAgents[x.idx].alive &&
        !wolvesSet.has(x.idx)   // 狼视角不把队友算进"信任/怀疑"输出
      );
    const trusted = susWithNo.slice().sort((a, b) => a.s - b.s)
      .slice(0, TRUST_TOP_N).map(x => `${x.no}号`).join("、") || "无";
    const suspect = susWithNo.slice().sort((a, b) => b.s - a.s)
      .slice(0, TRUST_TOP_N).map(x => `${x.no}号`).join("、") || "无";
    return `第${day}天：今日有${raw.otherSpeeches.length}条他人发言、我${raw.myActions.length}个动作；信任${trusted}，怀疑${suspect}。`;
  }

  // ── 纯函数：构造 LLM 摘要的 system / user prompt ──
  // 准确性强化：把"昨日摘要"塞进 context，让 LLM 做承接而非每天漂移
  function buildDigestPrompts(agent, day, raw, prevDigest) {
    const othersText = raw.otherSpeeches.length
      ? raw.otherSpeeches.map(s => {
          const tag = s.publicRole ? `[已跳${ROLE_CN[s.publicRole] || s.publicRole}]` : "[未跳]";
          return `${s.no}号${tag}: "${s.text}"`;
        }).join("\n")
      : "（无）";
    const myText = raw.myActions.length
      ? raw.myActions.map(a => `${a.kind}：「${a.thinking}」`).join("\n")
      : "（无）";
    const prevLine = prevDigest
      ? `\n\n你昨日的记忆（用于保持人设一致，不要原样复述）：${prevDigest}`
      : "";
    // V3：digest 必须信息密度更高 —— agent 跨天回忆只能靠这 80 字，必须含决定 + 站边
    return {
      system: `你正在扮演 ${agent.no} 号 ${agent.name}（${ROLE_CN[agent.role] || agent.role} · ${agent.personality.name}）。请用第一人称写一段 ≤80 字的【本日个人记忆摘要】，作为你明天打牌的依据。\n\n**必含 4 要素**（缺一不可，按顺序）：\n① 我今天的关键决定（投了谁/守了谁/查了谁/跳没跳，连同核心理由）；\n② 我对场上局势的判断（哪个跳神位真、哪条逻辑链可信）；\n③ 1-2 个最可信玩家 + 1-2 个最可疑玩家（用号码，简述原因）；\n④ 明天的具体计划（盯谁、问什么、跟哪条线）。\n\n禁忌：不暴露上帝视角、不分点不换行、不空话（"再观察"/"等信息"无效）。≤80 字一段话。`,
      user: `第 ${day} 天\n\n他人发言：\n${othersText}\n\n我的思考动作：\n${myText}${prevLine}\n\n请输出 ≤80 字的本日记忆摘要（必须覆盖决定+判断+信任/怀疑+明天计划 4 要素）：`,
    };
  }

  // ── 纯函数：把"摘要 + 原始材料"渲染为 markdown（写盘）──
  function buildDayMarkdown(day, digest, raw) {
    const lines = [`## 第 ${day} 天`, "", `**摘要**：${digest}`, ""];
    lines.push("<details><summary>原始材料</summary>");
    lines.push("");
    lines.push("**他人发言**：");
    if (raw.otherSpeeches.length === 0) lines.push("- （无）");
    else raw.otherSpeeches.forEach(s => {
      const tag = s.publicRole ? `[已跳${ROLE_CN[s.publicRole] || s.publicRole}]` : "[未跳]";
      lines.push(`- ${s.no}号${tag}（${KIND_CN[s.kind] || s.kind}）："${s.text}"`);
    });
    lines.push("");
    lines.push("**我的行动**：");
    if (raw.myActions.length === 0) lines.push("- （无）");
    else raw.myActions.forEach(a => lines.push(`- ${a.kind}: 「${a.thinking}」`));
    lines.push("");
    lines.push("</details>");
    lines.push("");
    return lines.join("\n");
  }

  // ── 异步：生成单个 agent 的本日摘要（LLM 优先，fallback 规则）──
  async function buildDigest(agent, day, raw, allAgents) {
    const hook = (typeof window !== "undefined") ? window.LLM_HOOK : null;
    if (hook && hook.enabled && typeof hook.summarize === "function") {
      try {
        const prevDigest = agent.memoryDigestByDay[day - 1];
        const prompts = buildDigestPrompts(agent, day, raw, prevDigest);
        const text = await hook.summarize(prompts);
        if (text && text.trim()) return text.trim().slice(0, DIGEST_MAX_CHARS);
      } catch (e) {
        console.warn(`[memory.digest] LLM failed for agent ${agent.no}:`, e.message);
      }
    }
    return ruleFallbackDigest(agent, day, raw, allAgents);
  }

  // ── 异步：给一个 agent 处理本日（更新内存 + 写盘）──
  async function flushOneAgent(agent, day, speechHistory, allAgents) {
    const raw = buildDayRaw(agent, day, speechHistory);
    agent.memoryByDay[day] = raw;
    const digest = await buildDigest(agent, day, raw, allAgents);
    agent.memoryDigestByDay[day] = digest;

    const header = `Agent ${agent.no} · ${agent.name} · ${ROLE_CN[agent.role] || agent.role} · ${agent.personality.name}`;
    const content = buildDayMarkdown(day, digest, raw);
    if (typeof window !== "undefined" && window.MEMORY) {
      window.MEMORY.append({ agentNo: agent.no, header, content });
    }
  }

  // ── 异步：并发刷新所有"今天还活着或今日新死"的 agent ──
  async function flushAll(agents, day, speechHistory, history) {
    const eligible = agents.filter(agent =>
      agent.alive || (history || []).some(h => h.idx === agent.idx && h.day === day)
    );
    await Promise.all(eligible.map(a => flushOneAgent(a, day, speechHistory, agents)));
  }

  // ── 纯函数：从 agent 取最近 N 天的 digest，给 _publicContext 用 ──
  function recentDigests(memoryDigestByDay, n = RECENT_DAYS) {
    if (!memoryDigestByDay) return [];
    return Object.keys(memoryDigestByDay)
      .map(Number).sort((a, b) => a - b).slice(-n)
      .map(d => ({ day: d, digest: memoryDigestByDay[d] }));
  }

  // ── 纯函数：渲染为 prompt 段，给 llm-adapter 用 ──
  function renderPromptBlock(recentMemoryDigests) {
    if (!recentMemoryDigests || recentMemoryDigests.length === 0) return "";
    const lines = [`=== 你的个人记忆（最近 ${RECENT_DAYS} 天压缩摘要，你视角）===`];
    recentMemoryDigests.forEach(entry => {
      if (entry.digest) lines.push(`【第${entry.day}天】${entry.digest}`);
    });
    lines.push("=== 记忆结束（如需查阅当天原始发言全文，本局 memory/agent-N.md 已落盘）===");
    return lines.join("\n");
  }

  return {
    // 主入口（由 game.js / llm-adapter.js / agents.js 调用）
    flushAll,
    recentDigests,
    renderPromptBlock,

    // 暴露内部细节给 verify-memory-digest.js 单元测试
    _internals: {
      ROLE_CN, KIND_CN, DIGEST_MAX_CHARS, RECENT_DAYS, TRUST_TOP_N,
      buildDayRaw, buildDigest, ruleFallbackDigest,
      buildDigestPrompts, buildDayMarkdown, flushOneAgent,
    },
  };
})();

if (typeof window !== "undefined") window.Memory = Memory;
