// LLM 适配器：把游戏内部状态打包成 prompt + tools，调本机 llm-proxy
// 入口：LLM_AGENT.use("local") → window.LLM_HOOK { speak, decide, summarize }
(function () {
  "use strict";

  const ROLE_CN = {
    wolf: "狼人", seer: "预言家", witch: "女巫",
    hunter: "猎人", guard: "守卫", villager: "村民",
  };

  // ===== 外部 prompt 缓存（prompts/*.md 可热重载覆盖以下硬编码默认值）=====
  // 启动时和每局开始前可调 refreshPrompts() 从 server /prompts 拉取
  let EXTERNAL_PROMPTS = {};
  async function refreshPrompts() {
    if (typeof window === "undefined") return;
    const base = (() => {
      if (window.electron?.proxyPort) return `http://127.0.0.1:${window.electron.proxyPort}`;
      const p = new URLSearchParams(location.search).get("port");
      return `http://127.0.0.1:${p ? Number(p) : 3001}`;
    })();
    try {
      const r = await fetch(`${base}/prompts`, { cache: "no-store" });
      if (r.ok) {
        EXTERNAL_PROMPTS = await r.json();
        console.log(`[LLM_AGENT] prompts loaded:`, Object.keys(EXTERNAL_PROMPTS));
      }
    } catch (e) {
      console.warn("[LLM_AGENT] /prompts fetch failed, fallback to hardcoded:", e.message);
    }
  }
  // 暴露给外部，让 UI 在游戏开始前刷新
  if (typeof window !== "undefined") window.refreshPrompts = refreshPrompts;

  function externalOr(key, fallback) {
    const ext = EXTERNAL_PROMPTS[key];
    return (ext && ext.trim()) ? ext.trim() : fallback;
  }

  // ===== V2.8：角色 SOP（高手玩法指南，硬编码 fallback）=====
  const ROLE_GUIDES = {
    seer: [
      "【你是预言家 —— 好人核心信息源，必须主动表达】",
      "- 第 1 天必上警 + 跳明身份 + 报首夜查验。",
      "- 每个夜晚验一个新的人，第二天**必报**新查验（这是好人最重要信息）。",
      "- 验人优先级：高威胁发言者 > 跳神的 > 帮狼带节奏的玩家。",
      "- 对位双跳预言家：**坚定不退**，让好人对位投自己查杀。",
      "- 临死遗言：把所有未报查验补全，推选信任的好人接节奏。",
    ].join("\n"),
    witch: [
      "【你是女巫 —— 好人关键道具角色，藏到最后】",
      "- 首夜默认救（除非被刀的是低威胁玩家）；第 2 夜起只救已暴露的神。",
      "- 毒药第 2 夜起可用：优先毒双跳预言家中可疑那个 / 悍跳明显的狼。",
      "- 白天**绝对不要主动跳**；被诬陷或局势危急时才跳。",
      "- 跳身份后报药品情况：「解药在/已用」「毒药在/已用」给好人节奏。",
    ].join("\n"),
    guard: [
      "【你是守卫 —— 保护型神职，低调潜伏】",
      "- 优先守已跳预言家的好人；不能连续两夜守同一人。",
      "- 第 1 夜可守自己；之后视情况守自己或预言家。",
      "- 白天**尽量不跳**；只在被诬陷且局势危急时跳。",
      "- 遗言：报昨晚守了谁（验证真预），推选好人接节奏。",
    ].join("\n"),
    hunter: [
      "【你是猎人 —— 好人威慑型，靠存在感震慑狼方】",
      "- 装强发言者让狼忌惮不刀你；**不主动跳身份**。",
      "- 被刀夜里出局 → 临死遗言开枪打**最高怀疑度**的活人。",
      "- 被毒（女巫毒）则**无法开枪**（规则约束）—— 死前发言可暗示自己是猎人威慑女巫。",
      "- 被票出也可开枪。",
    ].join("\n"),
    villager: [
      "【你是村民 —— 好人基本盘，靠跟票和分析推进游戏】",
      "- **绝对不要报「我是民」「我是好人」**（新手致命错误，等于亮身份给狼）。",
      "- 积极发言但措辞中性；分析逻辑，紧跟真预言家的查杀票。",
      "- 双跳预言家时：凭逻辑判断对位（信息量、查杀方向、票形）。",
      "- 遗言：**不要爆民**，但可以推选信任的好人接节奏。",
    ].join("\n"),
    wolf: [
      "【你是狼人 —— 隐蔽派阵营，靠假象骗票】",
      "- 你已知所有狼队友（看隐藏信息段），夜晚和队友协商一致刀人。",
      "- 刀人优先级：真预言家 > 女巫/守卫 > 高发言好人 > 警长好人。**绝不刀队友**。",
      "- 白天**默认潜伏**，**不要主动悍跳预言家**（被对位查杀概率高）。",
      "- 队友被查杀且没人冲锋时才考虑悍跳救场。",
      "- 自爆时机：仅在最后 2 狼 + 真预暴露 + 战术翻盘机会时用。",
      "- 投票：跟同阵营队友，但要分散看起来不像团狼。",
    ].join("\n"),
  };

  // ===== V2.8：解析 LLM 的【思考】+【发言】两段输出 =====
  function parseThinkingAndSpeech(text) {
    if (!text || typeof text !== "string") return { thinking: "", speech: "" };
    const m = text.match(/【思考】([\s\S]*?)【发言】([\s\S]*)/);
    if (m) return { thinking: m[1].trim(), speech: m[2].trim() };
    // fallback：LLM 没按格式输出 → 整段当 speech
    return { thinking: "", speech: text.trim() };
  }

  // ===== V2.8：基于 _publicContext 已有数据生成局势摘要 =====
  function buildGameAnalysis(context) {
    const lines = [];
    const alive = context.players.filter(p => p.alive);
    lines.push(`第 ${context.day} 天，存活 ${alive.length} 人。`);
    const claimedSeers = alive.filter(p => p.publicRole === "seer");
    if (claimedSeers.length === 0) lines.push("- 尚无人跳预言家。");
    else if (claimedSeers.length === 1) lines.push(`- 单跳预言家：${claimedSeers[0].no} 号。`);
    else lines.push(`- 双跳预言家：${claimedSeers.map(s => s.no + "号").join(" vs ")}（**必有狼**）。`);
    const otherClaims = alive.filter(p => p.publicRole && p.publicRole !== "seer");
    if (otherClaims.length) {
      lines.push("- 已跳其他身份：" + otherClaims.map(p => `${p.no}号[${ROLE_CN[p.publicRole]}]`).join("、"));
    }
    const wolfReports = (context.publicCheckReports || []).filter(r => r.result === "wolf");
    if (wolfReports.length) {
      lines.push("- 已公开查杀：" + wolfReports.map(r => `${r.from}号→${r.target}号`).join("；"));
    }
    const sheriff = alive.find(p => p.isSheriff);
    if (sheriff) lines.push(`- 警长：${sheriff.no} 号。`);
    return lines.join("\n");
  }

  // ===== Prompt 模板 =====
  function buildSystemPrompt(agent) {
    // 系统基底（游戏规则 + 13 条硬性规则）：可被 prompts/system_base.md 覆盖
    const SYSTEM_BASE_FALLBACK = [
      "你正在扮演中文狼人杀游戏（12 人神局）里的一个 AI 玩家，需要按指定角色和性格做出符合玩家直觉的发言。",
      "",
      "## 游戏规则摘要",
      "- **牌型**：12 人 = 4 狼人 + 4 神（预言家 / 女巫 / 猎人 / 守卫）+ 4 村民。**本局无白狼王**。",
      "- **阵营与胜利**：好人（4 神 + 4 民）vs 狼人（4 狼）。好人需投出全部狼；狼人杀光所有神或所有民（屠边）即胜。",
      "- **预言家**：每晚查验一人阵营（好人 / 狼人）；第二天必须公开报查（白天好人的核心信息源）。",
      "- **女巫**：解药、毒药各 1 瓶；全程**不能自救**；同一晚最多用一瓶。首夜默认应救人。",
      "- **猎人**：出局可翻牌开枪带走 1 人；**但被女巫毒杀则不能开枪**。",
      "- **守卫**：每晚守 1 人（可自守），**不能连续两晚守同一人**；与女巫同夜同时救/守同一人会「奶穿」，目标仍死亡。首夜默认空守。",
      "- **狼人**：任意狼可在白天**非投票环节**自爆，立刻亮身份出局并跳过当日投票直接进入夜晚（不带人）。",
      "- **警长**：第 1 天开局有上警竞选环节；警长票权 1.5 倍，被票出/倒台前可移交警徽（流）。",
      "",
      "硬性规则：",
      "1. 一句话最多 50 字，整段发言不超过 3 句话，控制在 80 字以内。",
      "2. 必须用第一人称、中文口语化表达。",
      "3. 必须以「N号」自我介绍开头（N 是你的座位号）。",
      "4. 严禁泄露任何上帝视角的信息：作为狼人不能直接说出队友是谁；作为女巫不能说出今晚救/毒了谁，除非剧情驱动；作为守卫不能直接报昨晚守的人。",
      "5. **预言家专属铁律**：每个夜晚会验一个新的人。第二天发言时**必须**公开报告这个新查验。",
      "6. 不要使用任何 Markdown、表情符号以外的特殊格式，不要换行。",
      "7. 已跳过身份的人（hasClaimed=true）：不要重复自我介绍套话；直接进入分析或报新查验。",
      "8. **必须基于「本场已有发言」段做针对性回应**：认同/反驳/质疑/补充某号玩家的观点。",
      "9. 不要每句都用「我觉得 X 号像狼」这类套话；分析要具体到对方发言中的逻辑漏洞。",
      "10. **不主动暴露身份**（**预言家除外**）：默认保守发言。",
      "11. **输出格式严格遵守两段式**：【思考】60-100 字 ToM 三段（局势/预判/目的）。【发言】≤80 字。",
      "12. 参考「上轮复盘」段了解前情。",
      "13. 参考「你的个人备忘」段保持人设一致。",
    ].join("\n");
    const base = externalOr("system_base", SYSTEM_BASE_FALLBACK);
    const roleGuide = externalOr(`role_${agent.role}`, ROLE_GUIDES[agent.role] || "");
    return [
      base,
      "",
      `你的身份：${agent.no} 号 ${agent.name}，角色 = ${ROLE_CN[agent.role] || agent.role}，性格 = ${agent.personality.name}`,
      // 狼专属:战术分工(开局已分配,4 狼各自不同)
      agent.role === "wolf" && agent.wolfTactic
        ? `★ 你的狼队战术分工：【${agent.wolfTactic}】—— 严格按此分工演戏(详见下方角色 SOP)。`
        : "",
      "",
      roleGuide,
      "",
      "请严格按角色和性格说话，并遵守上述所有规则。",
    ].filter(Boolean).join("\n");
  }

  function buildUserPrompt(payload) {
    const { game, context, kind } = payload;
    const phase =
      kind === "sheriff" ? "警长竞选发言" :
      kind === "pk"      ? "PK 发言" :
      kind === "last-words" ? "遗言" :
      `第 ${game.day} 天 白天发言`;

    const aliveList = context.players.filter(p => p.alive)
      .map(p => `${p.no}号${p.publicRole ? `[已跳${ROLE_CN[p.publicRole] || p.publicRole}]` : ""}${p.isSheriff ? "🎖" : ""}`)
      .join("、");

    const reports = context.publicCheckReports.length
      ? context.publicCheckReports.map(r =>
          `${r.from}号验${r.target}号为${r.result === "wolf" ? "狼" : "好人"}`).join("；")
      : "（暂无公开报查）";

    const myInfo = [];
    if (context.me.role === "wolf") {
      myInfo.push(`我的狼队友：${context.me.knownWolves.join("、")}号`);
      if (context.me.wolfTactic) {
        myInfo.push(`我的战术分工：【${context.me.wolfTactic}】 —— 严格按 SOP 演戏`);
      }
      const team = context.me.wolfTeam || [];
      if (team.length) {
        myInfo.push("狼队战术分配：" + team.map(t =>
          `${t.no}号【${t.tactic || "?"}】${t.alive ? "" : "已亡"}`).join("、"));
      }
    }
    if (context.me.role === "seer" && context.me.seerChecks.length) {
      myInfo.push("我历次查验：" + context.me.seerChecks.map(c =>
        `第${c.day}夜 ${c.no}号=${c.isWolf ? "狼" : "好人"}`).join("；"));
      // 昨夜新验的结果：必须在今天发言里报出来
      const lastCheck = context.me.seerChecks[context.me.seerChecks.length - 1];
      if (lastCheck && lastCheck.day === context.day) {
        myInfo.push(`★★★ 你昨晚刚验 ${lastCheck.no} 号 = ${lastCheck.isWolf ? "狼（查杀！）" : "好人（金水）"}，今天发言**必须公开报告这个新结果**，例如「我跳预言家，昨晚验 ${lastCheck.no} 号 ${lastCheck.isWolf ? "是狼" : "金水"}」`);
      }
    }
    if (context.me.role === "witch") {
      myInfo.push(`我女巫：解药${context.me.witchHasSave ? "在" : "已用"}，毒药${context.me.witchHasPoison ? "在" : "已用"}`);
    }

    // 本场已有发言（让 LLM 看到他人说了什么，能针对性回应）
    const kindLabel = { "day": "白天发言", "sheriff": "上警发言", "pk": "PK 发言", "last-words": "遗言" };
    const recent = context.recentSpeeches || [];
    const speechesBlock = recent.length
      ? "本场已有发言（按顺序，请基于这些内容做针对性回应）：\n" +
        recent.map(s => {
          const roleTag = s.publicRole ? `[已跳${ROLE_CN[s.publicRole] || s.publicRole}]` : "";
          const kindTag = kindLabel[s.kind] || s.kind;
          return `  · 第${s.day}天 ${s.no}号${roleTag}(${kindTag})："${s.text}"`;
        }).join("\n")
      : "本场尚无人发言，你是第一个。";

    // 自己当前的身份状态
    const myState = context.me.hasClaimed
      ? `你已跳身份 = ${ROLE_CN[context.me.publicRole] || context.me.publicRole}（不要重复介绍自己）`
      : "你尚未跳身份（**不要主动暴露身份**，除非战术需要）";

    // 私有思考日记（让 agent 跨轮策略一致）
    // 上游 _selectKeyThinkings 已按天精选最近 4 天关键决策，这里直接消费、不再二次截断
    const myThinking = (context.me.thinkingLog || []);
    const thinkingBlock = myThinking.length
      ? "你的内心日记（私人，仅你可见，帮你跨轮策略一致）：\n" +
        myThinking.map(t => `  · 第${t.day}天[${kindLabel[t.kind] || t.kind}]："${t.thinking}"`).join("\n")
      : "你的内心日记：（首次发言，尚未有思考记录）";

    // V2.8：局势摘要
    const analysis = buildGameAnalysis(context);

    // V2.9-2：上轮复盘（全场共享）
    const summaries = context.roundSummaries || [];
    const summariesBlock = summaries.length
      ? "=== 上轮复盘（全场共享，从中学习推断方向）===\n" +
        summaries.map(s => `【第${s.day}天】事实：${s.factual}\n            推断：${s.inference}`).join("\n") +
        "\n=== 复盘结束 ==="
      : "";

    // 个人记忆摘要（你视角下的最近 3 天，每天 ≤80 字压缩摘要）
    const memoryBlock = buildMemoryBlock(context.me.recentMemoryDigests);

    // 三层记忆 · 私有层
    const privateMemBlock = renderPrivateMemoryBlock(context.me);

    return [
      `当前阶段：${phase}`,
      `场上存活：${aliveList}`,
      `公开报查：${reports}`,
      "",
      "本场局势摘要：\n" + analysis,
      "",
      summariesBlock,
      "",
      memoryBlock,
      "",
      privateMemBlock,
      "",
      myInfo.length ? "我的隐藏信息：\n" + myInfo.join("\n") : "",
      "",
      speechesBlock,
      "",
      thinkingBlock,
      "",
      `你的状态：${myState}`,
      "",
      buildSpeechQualityHint(payload, recent),
    ].filter(Boolean).join("\n");
  }

  // 动态发言质量铁律(根据前面已有发言长度+你过去思考做针对性提示)
  function buildSpeechQualityHint(payload, recent) {
    const { kind } = payload;
    const me = payload.context.me;
    const past = (me.thinkingLog || []).slice(-3).map(t => t.thinking || "").filter(Boolean);
    const lines = [];
    lines.push("━━━━━ 发言质量铁律 ━━━━━");
    lines.push("【输出格式】严格两段:");
    lines.push("  【思考】60-100 字 ToM(局势 + 预判目标玩家反应 + 我的目的)。");
    lines.push("  【发言】≤80 字。**必须满足以下硬性指标,否则就是无效发言**:");
    lines.push("    ① 点名至少 1 个具体号(N 号),引用 ta 的原话/票形/查杀做回应,不能空谈;");
    lines.push("    ② 至少 1 个明确判断 + 1 个具体理由(可推可砍可信,不要 hedging);");
    lines.push("    ③ 输出 1 个独有信息点:查杀方向 / 票形预判 / 矛盾点指认 / 关键反问 / 局势翻盘思路;");
    lines.push("    ④ **零容忍套话**:严禁「同意 X 号」「跟 X 号」「我觉得 X 像狼」「再观察」「等更多信息」;");
    lines.push("    ⑤ 句式多样,避免「X 号是狼/像狼」一刀切;换说法 ——「刀口诡异」「站边可疑」「递刀给狼」「发言空」「逻辑断层」;");
    lines.push("    ⑥ **跨轮去重**:不要重复你过去说过的论点(见下),今天必须推进一个新维度。");
    if (past.length) {
      lines.push("");
      lines.push("【你过去的思考(避免重复)】:");
      past.forEach((t, i) => lines.push(`  · "${t.slice(0, 80)}"`));
    }
    if (recent && recent.length) {
      lines.push("");
      lines.push(`【前面已有 ${recent.length} 人发言,你必须比他们多说出 1 个新东西】`);
    }
    lines.push("");
    lines.push("可选:tool 里带 beliefs_update 字段(如 {\"5\":{\"suspicion\":0.8,\"reason\":\"刀口太准\"}})写回推理表,下轮 prompt 会回放。");
    lines.push("━━━━━━━━━━━━━━━━━━━━━━━━");
    return lines.join("\n");
  }

  // ===== 你视角下的最近 3 天个人记忆（摘要式，每天 ≤80 字） =====
  // 渲染逻辑已抽离到 web/memory.js（Memory.renderPromptBlock），
  // 这里做一层薄封装让 buildUserPrompt / buildDecidePrompt 不直接耦合全局 Memory。
  function buildMemoryBlock(recentMemoryDigests) {
    return (typeof Memory !== "undefined")
      ? Memory.renderPromptBlock(recentMemoryDigests)
      : "";
  }

  // ===== 三层记忆(规格 §2-3)的私有层渲染 =====
  // facts 是 append-only 事实流(查验结果/刀口/自身动作);
  // beliefs 是玩家增量更新的"对每个座位的怀疑度+理由"。
  // 这两块组合起来让 agent 跨轮推理连贯。
  function renderPrivateMemoryBlock(me) {
    if (!me) return "";
    const lines = [];
    // facts
    const facts = me.privateFacts || [];
    if (facts.length) {
      lines.push("=== 你的私密事实流（仅你可见，append-only）===");
      facts.forEach(f => {
        const ts = `第${f.day}夜/天`;
        if (f.type === "seer-check") lines.push(`  ${ts} 我验 ${f.targetNo}号 = ${f.result === "wolf" ? "狼" : "好人"}`);
        else if (f.type === "witch-save") lines.push(`  ${ts} 我用解药救了 ${f.targetNo}号`);
        else if (f.type === "witch-poison") lines.push(`  ${ts} 我用毒药毒了 ${f.targetNo}号`);
        else if (f.type === "guard-protect") lines.push(`  ${ts} 我守了 ${f.targetNo === -1 ? "空守" : f.targetNo + "号"}`);
        else if (f.type === "wolf-propose") lines.push(`  ${ts} 我(狼)提议刀 ${f.targetNo === -1 ? "空刀" : f.targetNo + "号"}: ${f.thinking || ""}`);
        else if (f.type === "wolf-kill-final") lines.push(`  ${ts} 狼队终选刀 ${f.targetNo === -1 ? "空刀" : f.targetNo + "号"}`);
        else lines.push(`  ${ts} ${f.type}: ${JSON.stringify(f)}`);
      });
      lines.push("=== 事实流结束 ===");
    }
    // beliefs
    const top = me.topSuspicions || [];
    if (top.length) {
      lines.push("=== 你的推理表(beliefs,你可在 tool 里增量更新)===");
      top.forEach(b => {
        const sus = (b.suspicion ?? 0).toFixed(2);
        lines.push(`  ${b.seat}号 怀疑度 ${sus}${b.reason ? ` —— ${b.reason}` : ""}${b.updatedAt ? ` (D${b.updatedAt})` : ""}`);
      });
      lines.push("=== 推理表结束 ===");
    }
    return lines.join("\n");
  }

  // ===== decide 的 user prompt =====
  function buildDecidePrompt(payload) {
    const { context, kind, options = {} } = payload;
    const aliveList = context.players.filter(p => p.alive)
      .map(p => `${p.no}号${p.publicRole ? `[已跳${ROLE_CN[p.publicRole] || p.publicRole}]` : ""}${p.isSheriff ? "🎖" : ""} 怀疑度${p.mySuspicion}`)
      .join("\n  ");

    const reports = context.publicCheckReports.length
      ? context.publicCheckReports.map(r =>
          `${r.from}号验${r.target}号=${r.result === "wolf" ? "狼" : "好人"}`).join("；")
      : "（暂无）";

    const myInfo = buildHiddenInfo(context);

    // V2.8：私有思考日记。V3：上游 _selectKeyThinkings 已按天精选最近 4 天关键决策，这里不再二次截断
    const myThinking = (context.me.thinkingLog || []);
    const kindLabel = { "day": "白天", "sheriff": "上警", "pk": "PK", "last-words": "遗言",
                        "vote": "白天投票", "wolf-kill": "夜刀", "seer-check": "夜验",
                        "witch-save": "女巫救", "witch-poison": "女巫毒", "guard-protect": "守人" };
    const thinkingBlock = myThinking.length
      ? "你的内心日记（私人，仅你可见，帮你跨轮策略一致）：\n" +
        myThinking.map(t => `  · 第${t.day}天[${kindLabel[t.kind] || t.kind}]："${t.thinking}"`).join("\n")
      : "你的内心日记：（首次决策，尚无记录）";

    // V2.8：局势摘要
    const analysis = buildGameAnalysis(context);

    // V2.12-B：本场已有发言（让决策也能基于他人发言原文）
    const speechKindLabel = { "day": "白天发言", "sheriff": "上警发言", "pk": "PK 发言", "last-words": "遗言" };
    const recent = context.recentSpeeches || [];
    const speechesBlock = recent.length
      ? "本场已有发言（请基于这些做决策）：\n" +
        recent.map(s => {
          const roleTag = s.publicRole ? `[已跳${ROLE_CN[s.publicRole] || s.publicRole}]` : "";
          const kindTag = speechKindLabel[s.kind] || s.kind;
          return `  · 第${s.day}天 ${s.no}号${roleTag}(${kindTag})："${s.text}"`;
        }).join("\n")
      : "";

    // V2.12-B：上轮复盘（全场共享）
    const summaries = context.roundSummaries || [];
    const summariesBlock = summaries.length
      ? "=== 上轮复盘（全场共享）===\n" +
        summaries.map(s => `【第${s.day}天】事实：${s.factual}\n            推断：${s.inference}`).join("\n") +
        "\n=== 复盘结束 ==="
      : "";

    // 修复：字段名是 recentMemoryDigests，旧代码写成 recentMemory 导致 decide 路径记忆块永远为空
    const memoryBlock = buildMemoryBlock(context.me.recentMemoryDigests);

    // 三层记忆 · 私有层(facts + beliefs)
    const privateMemBlock = renderPrivateMemoryBlock(context.me);

    const lines = [
      `当前阶段：${decideKindLabel(kind)}`,
      `场上存活：\n  ${aliveList}`,
      `公开报查：${reports}`,
      "",
      "本场局势摘要：\n" + analysis,
      "",
      speechesBlock,
      "",
      summariesBlock,
      "",
      memoryBlock,
      "",
      privateMemBlock,
      "",
      myInfo,
      "",
      thinkingBlock,
    ].filter(Boolean);

    if (kind === "vote" || kind === "pk-vote") {
      const cands = (options.candidates || []).map(no => `${no}号`).join("、");
      lines.push(`候选放逐目标：${cands}`);
      lines.push(`提示：好人投最像狼的人；狼人投跳神/高威胁好人但避免投队友；不确定可用 -1 弃票。`);
    } else if (kind === "sheriff-vote") {
      const cands = (options.candidates || []).map(no => `${no}号`).join("、");
      lines.push(`上警玩家：${cands}`);
      lines.push(`提示：好人投跳预言家或最可信者；狼人投自家队友（如有上警）；可用 -1 弃票。`);
    } else if (kind === "wolf-kill") {
      const round = options.round || 1;
      if (context.wolfNightLog) {
        lines.push(context.wolfNightLog);
      }
      if (round === 1) {
        lines.push(`【狼人协商 · 第 1 轮 · 独立提议】`);
        lines.push(`★ 你的战术分工:【${context.me.wolfTactic || "未分配"}】 —— 与队友按分工协同。`);
        lines.push(`**首夜必刀** —— 平安夜会让好人摸排狼队的难度大幅下降,**白白浪费一晚先手就是新手行为**。`);
        lines.push(`刀人优先级:真预言家 > 女巫/守卫 > 高发言好人 > 警长好人。**绝不刀队友**(他们的座位号见隐藏信息段)。`);
        lines.push(`允许 target=-1 空刀但**强烈不推荐**:只在"全队战术明确认同制造迷局"且首夜守卫概率极高时考虑。**默认不要空刀**。`);
        lines.push(`thinking 段务必说出"为什么是这个目标",第 2 轮队友会看到你的理由。`);
      } else {
        const r1 = context.wolfRoundOne || [];
        lines.push(`【狼人协商 · 第 2 轮 · 终选】`);
        lines.push(`=== 第 1 轮所有狼队友提议 ===`);
        r1.forEach(v => {
          const tag = v.wolfNo === context.me.no ? "（你）" : "";
          lines.push(`  ${v.wolfNo}号狼${tag}: 提议刀 ${v.target}号 —— "${v.thinking || "(无理由)"}"`);
        });
        lines.push(`=== 提议结束 ===`);
        lines.push(`提示：综合队友提议给出你的最终选择。多数派优先；若理由不够强，可以坚持自己。最终所有狼第 2 轮投票，得票最多者被刀。`);
      }
    } else if (kind === "seer-check") {
      const checked = (context.me.seerChecks || []).map(c => `${c.no}号`).join("、");
      lines.push(`【预言家·查验】已验过：${checked || "（无）"}`);
      lines.push(`A3 强制规则：**不能查自己（${context.me.no}号）；不能查已死玩家；不能查已验过的人**。只允许从场上"未验过的活人"里选 1 人。`);
      lines.push(`结果只返回阵营（好人/狼人），不会显示具体神职。`);
      lines.push(`提示：优先验高怀疑度、强发言者；其次验跳神位 / 帮狼带节奏的人。`);
    } else if (kind === "witch-save") {
      lines.push(`【女巫·解药】今晚被刀目标：${options.killedNo}号${options.isSelf ? "（就是你自己！）" : ""}`);
      lines.push(`A3/A8 强制规则：女巫**仅第一晚**能看到刀口、**仅第一晚**可自救；同一晚解+毒不能并用。`);
      lines.push(`提示：首夜默认救人；其他场景按优先级保关键神职。`);
    } else if (kind === "witch-poison") {
      lines.push(`【女巫·毒药】第 ${context.day} 夜（毒药仅第 2 夜起可用）`);
      lines.push(`A3/A8 强制规则：同一晚不可解+毒并用；如果你今晚已用解药则**不会进入毒药决策**。`);
      lines.push(`提示：优先毒已暴露的狼或场上多跳预言家中最怀疑的那个。目标号 -1 表示今晚不毒。`);
    } else if (kind === "guard-protect") {
      const last = context.me.lastGuardedNo;
      lines.push(`【守卫·守人】上一晚守护对象：${last != null ? last + "号" : "（无 / 首夜）"}`);
      lines.push(`A3/A8 强制规则：不能连续两晚守同一目标（含自己）；首夜无此限制；允许"空守"。`);
      lines.push(`提示：优先守已跳预言家；其次守自己或低怀疑度发言者。目标号 -1 表示今晚空守。`);
    } else if (kind === "run-sheriff") {
      const totalAlive = (context.players || []).filter(p => p.alive).length;
      lines.push(`【警长竞选 · 是否上警】场上活人 ${totalAlive}。`);
      lines.push(`⚠ 高曝光决策:上警=主动暴露自己。绝大多数角色应该默认 run=false。`);
      if (context.me.role === "wolf" && context.me.wolfTactic) {
        lines.push(`★ 你是狼且战术分工=【${context.me.wolfTactic}】,按分工铁律决定:`);
        lines.push(`  · 悍跳狼: **必须 run=true**(冲身位铁律,要上警跳预言家,把好人目光吸到自己身上)。`);
        lines.push(`  · 冲锋狼: 通常 run=false(打头阵靠白天激进发言,不靠警长身位);除非悍跳狼缺位才考虑顶上。`);
        lines.push(`  · 倒钩狼: 可以 run=true(站好人立场更可信),但**绝不**跳预言家。`);
        lines.push(`  · 深水狼: **绝对 run=false**(藏身位,不暴露)。`);
      } else {
        lines.push(`按角色铁律:`);
        lines.push(`  · 预言家:必须 run=true(铁律,核心信息源)。`);
        lines.push(`  · 女巫 / 守卫:**几乎永不上警**(暴露神职,首夜就被狼刀)。`);
        lines.push(`  · 猎人:**默认不上**;只在性格强势想威慑狼时偶尔上。`);
        lines.push(`  · 村民:**绝对默认 run=false**。村民上警没信息只会被狼盯。`);
      }
      lines.push(`先想清楚:你是谁?上去能提供什么信息?如果不能,不上。`);
    } else if (kind === "self-explode") {
      lines.push(`提示：场上危急（队友被查杀/自己被查杀/真预言家明牌）时考虑自爆。`);
    } else if (kind === "pass-badge") {
      const cands = (options.candidates || []).map(no => `${no}号`).join("、");
      lines.push(`可传警徽的活人：${cands}`);
      lines.push(`提示：好人传给最信任的活人（金水/真预言家）；狼人传给队友或撕毁；-1 撕毁。`);
    }

    lines.push("");
    lines.push("请调用对应的 tool 给出你的决策。");
    return lines.join("\n");
  }

  function buildHiddenInfo(context) {
    const myInfo = [];
    if (context.me.role === "wolf") {
      myInfo.push(`我的狼队友：${(context.me.knownWolves || []).join("、")}号`);
      if (context.me.wolfTactic) {
        myInfo.push(`我的战术分工：【${context.me.wolfTactic}】(严格按 SOP 演戏)`);
      }
      const team = context.me.wolfTeam || [];
      if (team.length) {
        myInfo.push("狼队战术分配：" + team.map(t =>
          `${t.no}号【${t.tactic || "?"}】${t.alive ? "" : "已亡"}`).join("、")
          + " —— 协同时考虑各自分工,不要 4 狼打同一种风格");
      }
    }
    if (context.me.role === "seer" && context.me.seerChecks?.length) {
      myInfo.push("我历次查验：" + context.me.seerChecks.map(c =>
        `第${c.day}夜 ${c.no}号=${c.isWolf ? "狼" : "好人"}`).join("；"));
    }
    if (context.me.role === "witch") {
      myInfo.push(`我女巫：解药${context.me.witchHasSave ? "在" : "已用"}，毒药${context.me.witchHasPoison ? "在" : "已用"}`);
    }
    return myInfo.length ? "我的隐藏信息：\n" + myInfo.map(l => "  " + l).join("\n") : "";
  }

  function decideKindLabel(kind) {
    return {
      "vote": "白天投票放逐",
      "pk-vote": "PK 复投",
      "sheriff-vote": "警长投票",
      "wolf-kill": "狼人夜晚击杀",
      "seer-check": "预言家夜晚查验",
      "witch-save": "女巫救人决策",
      "witch-poison": "女巫毒人决策",
      "guard-protect": "守卫夜晚守护",
      "run-sheriff": "警长竞选是否上警",
      "self-explode": "狼人是否自爆",
      "pass-badge": "警徽流转",
    }[kind] || kind;
  }

  // ===== Tool schemas（让 Claude 强制结构化输出 + 每个决策带 thinking）=====
  const THINK = { type: "string", description: "60-100 字 Theory-of-Mind 三段式：①局势(我最怀疑谁+理由) ②预判(目标玩家会怎么想/怎么投) ③目的(我此决策要达成什么)" };
  // 可选字段:增量更新你的推理表(规格 §3 beliefs)
  // 形如 {"5":{"suspicion":0.8,"reason":"刀口太准"}, "9":{"suspicion":0.3,"reason":"发言中立"}}
  // 只填你这一轮 *变化* 的座位,不要整体重写。Moderator 会把它 merge 进你的 PrivateMemory,下一轮 prompt 回放。
  const BELIEFS_UPDATE = {
    type: "object",
    description: "可选:本轮你对其他玩家怀疑度的增量更新。键=座位号(字符串),值={suspicion:0~1, reason:简短理由}。只填变化项,不要整体重写。",
    additionalProperties: {
      type: "object",
      properties: {
        suspicion: { type: "number", description: "0~1,越高越像狼" },
        reason: { type: "string", description: "≤30 字理由" },
      },
    },
  };
  // 在所有 decide tool 的 properties 里注入 beliefs_update(非 required,可省略)
  function withBeliefs(props) {
    return { ...props, beliefs_update: BELIEFS_UPDATE };
  }
  const DECIDE_TOOLS = {
    "vote":          [{ name: "vote",            description: "投票放逐目标",         input_schema: { type: "object", properties: withBeliefs({ thinking: THINK, target: { type: "integer", description: "玩家座位号 1-12，或 -1 弃票" } }), required: ["thinking", "target"] } }],
    "pk-vote":       [{ name: "vote",            description: "PK 复投目标",          input_schema: { type: "object", properties: withBeliefs({ thinking: THINK, target: { type: "integer", description: "玩家座位号 1-12，或 -1 弃票" } }), required: ["thinking", "target"] } }],
    "sheriff-vote":  [{ name: "sheriff_vote",    description: "投票选警长",           input_schema: { type: "object", properties: withBeliefs({ thinking: THINK, target: { type: "integer", description: "上警玩家座位号 1-12，或 -1 弃票" } }), required: ["thinking", "target"] } }],
    "wolf-kill":     [{ name: "wolf_kill",       description: "选择今晚击杀目标（允许空刀=不杀）",     input_schema: { type: "object", properties: withBeliefs({ thinking: THINK, target: { type: "integer", description: "玩家座位号 1-12；-1 表示空刀（A8 允许）；不可填本人或队友座位号" } }), required: ["thinking", "target"] } }],
    "seer-check":    [{ name: "seer_check",      description: "选择今晚查验目标",     input_schema: { type: "object", properties: withBeliefs({ thinking: THINK, target: { type: "integer", description: "玩家座位号 1-12" } }), required: ["thinking", "target"] } }],
    "witch-save":    [{ name: "witch_save",      description: "决定是否使用解药救人", input_schema: { type: "object", properties: withBeliefs({ thinking: THINK, save: { type: "boolean", description: "true=使用解药救人，false=不救" } }), required: ["thinking", "save"] } }],
    "witch-poison":  [{ name: "witch_poison",    description: "决定是否使用毒药及目标", input_schema: { type: "object", properties: withBeliefs({ thinking: THINK, target: { type: "integer", description: "毒杀目标座位号 1-12，-1 表示今晚不毒" } }), required: ["thinking", "target"] } }],
    "guard-protect": [{ name: "guard_protect",   description: "选择今晚守护目标（允许空守=不守）", input_schema: { type: "object", properties: withBeliefs({ thinking: THINK, target: { type: "integer", description: "玩家座位号 1-12（可守自己）；-1 表示空守。A3：不能连续两晚守同一目标，首夜无此限制。" } }), required: ["thinking", "target"] } }],
    "run-sheriff":   [{ name: "run_for_sheriff", description: "决定是否上警",         input_schema: { type: "object", properties: withBeliefs({ thinking: THINK, run: { type: "boolean", description: "true=上警，false=不上" } }), required: ["thinking", "run"] } }],
    "self-explode":  [{ name: "self_explode",    description: "决定是否立即自爆",     input_schema: { type: "object", properties: withBeliefs({ thinking: THINK, explode: { type: "boolean", description: "true=自爆，false=不爆" } }), required: ["thinking", "explode"] } }],
    "pass-badge":    [{ name: "pass_badge",      description: "决定警徽流转目标",     input_schema: { type: "object", properties: withBeliefs({ thinking: THINK, target: { type: "integer", description: "接徽玩家座位号 1-12，-1 撕毁" } }), required: ["thinking", "target"] } }],
  };

  // ===== Provider 实现 =====
  // local：走本机 llm-proxy.js。proxy 根据 body.provider 路由到 claude / deepseek / openai。
  // window.CURRENT_PROVIDER 由 UI 的 LLM select 控制。
  const providers = {
    local({ proxyUrl = null } = {}) {
      function detectPort() {
        if (typeof window === "undefined") return 3001;
        if (window.electron?.proxyPort) return window.electron.proxyPort;
        const m = new URLSearchParams(location.search).get("port");
        return m ? Number(m) : 3001;
      }
      const url = proxyUrl || `http://127.0.0.1:${detectPort()}`;
      async function call(endpoint, body) {
        const provider = (typeof window !== "undefined" && window.CURRENT_PROVIDER) || null;
        if (!provider) throw new Error("no provider selected (probeProxy not yet completed)");
        const resp = await fetch(`${url}${endpoint}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider, ...body }),
        });
        if (!resp.ok) throw new Error(`llm-proxy ${resp.status}: ${await resp.text()}`);
        return await resp.json();
      }
      return {
        // V2.9-2：复盘解说员调用（不走 buildSystemPrompt 模板）
        async summarize({ system, user }) {
          const data = await call("/chat", {
            system, user,
            maxTokens: 200,
          });
          return data.text || "";
        },
        // 终局法官：根据完整事件流给出胜负叙事
        async judge({ winner, players, events, history }) {
          const judgeSystem = externalOr("judge",
            "你是狼人杀解说员。请根据胜负与事件流写 80-150 字的本局解说，包括关键转折点和 MVP 失误。中文口语化，不分点不换行。"
          );
          const playerLines = players.map(p =>
            `${p.no}号 ${p.name}(${p.personality}) = ${p.role}${p.alive ? "" : " 已亡"}${p.isSheriff ? " 🎖警长" : ""}`
          ).join("\n");
          const evtLines = events.map(e => {
            const tagDay = `D${e.day}`;
            if (e.type === "speak") return `${tagDay} ${e.agentNo}号发言: ${e.text}`;
            if (e.type === "wolf-kill") return `${tagDay} 狼队终选刀: ${e.targetNo}号`;
            if (e.type === "wolf-propose-kill" && e.round === 1) return `${tagDay} 狼${e.wolfNo}提议刀${e.targetNo}: ${e.thinking || ""}`;
            if (e.type === "seer-check") return `${tagDay} 预言家${e.seerNo}验${e.targetNo}=${e.isWolf ? "狼" : "好人"}`;
            if (e.type === "seer-report") return `${tagDay} ${e.fromNo}号公开报${e.targetNo}=${e.result}`;
            if (e.type === "witch-save") return `${tagDay} 女巫救${e.targetNo}号`;
            if (e.type === "witch-poison") return `${tagDay} 女巫毒${e.targetNo}号`;
            if (e.type === "guard-protect") return `${tagDay} 守卫守${e.targetNo}号`;
            if (e.type === "death") return `${tagDay} 死亡: ${e.targetNo}号`;
            if (e.type === "vote") return `${tagDay} ${e.fromNo}→${e.targetNo}(${e.kind})`;
            if (e.type === "execute") return `${tagDay} 处决: ${e.targetNo}号 (${e.reason})`;
            if (e.type === "wolf-explode") return `${tagDay} ${e.wolfNo}号狼自爆`;
            if (e.type === "sheriff-elected") return `${tagDay} 警长选出: ${e.sheriffNo}号`;
            if (e.type === "badge-pass") return `${tagDay} 警徽: ${e.fromNo}→${e.toNo}`;
            return null;
          }).filter(Boolean).join("\n");
          const user = [
            `胜方：${winner === "good" ? "好人" : "狼人"}`,
            `共持续 ${(events.find(e => e.day) ? events[events.length-1].day : "?")} 天`,
            "",
            "## 身份表",
            playerLines,
            "",
            "## 关键事件",
            evtLines,
            "",
            "请输出 80-150 字解说：",
          ].join("\n");
          const data = await call("/chat", {
            system: judgeSystem,
            user,
            maxTokens: 300,
          });
          return data.text || "";
        },
        async speak(payload) {
          const data = await call("/chat", {
            system: buildSystemPrompt(payload.agent),
            user:   buildUserPrompt(payload),
            maxTokens: 500,   // V2.8：留空间给【思考】段
          });
          const raw = data.text || "";
          // V2.8：解析【思考】【发言】两段，思考存进 agent.thinkingLog
          const { thinking, speech } = parseThinkingAndSpeech(raw);
          if (thinking && payload.agent && payload.agent.thinkingLog) {
            payload.agent.thinkingLog.push({
              day: payload.game?.day ?? 0,
              kind: payload.kind || "day",
              thinking,
            });
          }
          return speech;
        },
        async decide(payload) {
          const tools = DECIDE_TOOLS[payload.kind];
          if (!tools) return null;
          const required = tools[0].input_schema?.required || [];
          const system = buildSystemPrompt(payload.agent);
          const user = buildDecidePrompt(payload);

          // 最多重试 3 次（首次 + 2 次重试），校验 required 字段
          let lastInput = null;
          for (let attempt = 0; attempt < 3; attempt++) {
            const data = await call("/decide", {
              system,
              user: attempt === 0 ? user : user + `\n\n⚠ 上次输出缺少必填字段，请确保 tool 调用包含 ${required.join("/")}，并填写有效值。`,
              tools,
              maxTokens: 500,
            });
            const input = data.input;
            const missing = !input ? required : required.filter(f => input[f] === undefined || input[f] === null);
            if (missing.length === 0) {
              lastInput = input;
              break;
            }
            console.warn(`[LLM] decide ${payload.kind} attempt ${attempt + 1} 缺少字段:`, missing);
            lastInput = input;
          }
          // 三层记忆 · 增量写回 beliefs(规格 §3)
          if (lastInput && lastInput.beliefs_update && payload.agent?.memory) {
            const n = payload.agent.memory.updateBeliefs(
              lastInput.beliefs_update,
              payload.game?.day ?? 0
            );
            if (n > 0) console.log(`[memory] agent ${payload.agent.no} beliefs +${n}`);
          }
          // V2.8：input.thinking 存进 agent.thinkingLog
          if (lastInput && lastInput.thinking && payload.agent && payload.agent.thinkingLog) {
            payload.agent.thinkingLog.push({
              day: payload.game?.day ?? 0,
              kind: payload.kind || "decide",
              thinking: lastInput.thinking,
            });
          }
          return lastInput;
        },
      };
    },
  };

  // ===== 暴露开关 =====
  const TIMEOUT_MS = 15000;   // 含 tool_use 的请求通常 3-10s，12 voter 并发时单条可能到 8s+
  function withTimeout(promise, ms = TIMEOUT_MS) {
    return Promise.race([
      promise,
      new Promise((_, rej) => setTimeout(() => rej(new Error(`LLM timeout ${ms}ms`)), ms)),
    ]);
  }

  window.LLM_AGENT = {
    use(providerName, config = {}) {
      const factory = providers[providerName];
      if (!factory) throw new Error("unknown provider: " + providerName);
      const impl = factory(config);
      window.LLM_HOOK = {
        enabled: true,
        async speak(payload)  { return await withTimeout(impl.speak(payload)); },
        async decide(payload) { return await withTimeout(impl.decide(payload)); },
        async summarize(payload) {
          if (typeof impl.summarize !== "function") return null;
          return await withTimeout(impl.summarize(payload), 10000);
        },
        async judge(payload) {
          if (typeof impl.judge !== "function") return null;
          return await withTimeout(impl.judge(payload), 20000);
        },
      };
      console.log(`[LLM_AGENT] enabled with ${providerName}, model=${config.model || "default"}`);
    },
    disable() {
      if (window.LLM_HOOK) window.LLM_HOOK.enabled = false;
      console.log("[LLM_AGENT] disabled, fallback to rule-based.");
    },
  };

  // ===== Memory 持久化（与 LLM 调用解耦，不依赖 provider）=====
  function memoryBaseUrl() {
    if (typeof window === "undefined") return "http://127.0.0.1:3001";
    if (window.electron?.proxyPort) return `http://127.0.0.1:${window.electron.proxyPort}`;
    const p = new URLSearchParams(location.search).get("port");
    return `http://127.0.0.1:${p ? Number(p) : 3001}`;
  }
  async function memoryPost(endpoint, body) {
    const resp = await fetch(`${memoryBaseUrl()}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : "",
    });
    if (!resp.ok) throw new Error(`memory ${resp.status}: ${await resp.text()}`);
    return await resp.json();
  }
  window.MEMORY = {
    async reset() {
      try { await memoryPost("/memory/reset"); }
      catch (e) { console.warn("[MEMORY] reset failed:", e.message); }
    },
    async append({ agentNo, header, content }) {
      try { await memoryPost("/memory", { agentNo, header, content }); }
      catch (e) { console.warn(`[MEMORY] append agent-${agentNo} failed:`, e.message); }
    },
  };
})();
