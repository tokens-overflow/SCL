// LLM 适配器：把游戏内部状态打包成 prompt + tools，调本机 llm-proxy
// 入口：LLM_AGENT.use("local") → window.LLM_HOOK { speak, decide, summarize }
(function () {
  "use strict";

  const ROLE_CN = {
    wolf: "狼人", seer: "预言家", witch: "女巫",
    hunter: "猎人", guard: "守卫", villager: "村民",
  };

  // ===== V2.8：角色 SOP（高手玩法指南）=====
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
    return [
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
      "5. **预言家专属铁律**：每个夜晚会验一个新的人。第二天发言时**必须**公开报告这个新查验（例：「我验 7 号是狼」「我验 9 号是好人」）—— 这是预言家最重要的战术，**绝对不能藏**。之前已经报过的旧查验不必重复。即使 hasClaimed=true，新一天的新查验**仍然必须报**。",
      "6. 不要使用任何 Markdown、表情符号以外的特殊格式，不要换行。",
      "7. 已跳过身份的人（hasClaimed=true）：不要重复说「我是 X 号 是 XX 角色」这类自我介绍套话；直接进入分析或报新查验。**预言家见规则 5 —— 每天新验的人必须报**。",
      "8. **必须基于「本场已有发言」段做针对性回应**：认同/反驳/质疑/补充某号玩家的观点；不要只说自己想说的；引用对方原话中的关键词。",
      "9. 不要每句都用「我觉得 X 号像狼」这类套话；分析要具体到对方发言中的逻辑漏洞或矛盾点。",
      "10. **不主动暴露身份**（**预言家除外，见规则 5**）：默认保守发言，不要主动说「我是民」「我是好人」「我是猎人」等亮身份的话。",
      "    - **预言家**：第 1-2 天就应该跳，每天必须报新查验，这是好人核心信息源。",
      "    - 女巫/守卫/猎人：仅在战术需要（被诬陷需自证、对位真狼、保护重要好人）时才跳身份；否则继续潜伏分析。",
      "    - 狼人：默认潜伏，**不要主动悍跳**，除非队友被查杀或局势危急。",
      "    - 村民：永远不要说「我是民」「我是好人」「我是平民」。",
      "    - 措辞中性，让对手猜不出你的阵营。",
      "11. **输出格式严格遵守两段式**：",
      "    【思考】<你内心的策略分析，30-60 字，会被记录到你私人日记，对外不公开。包括：当前已知事实、谁最可疑、你这一发言要达到什么目的>",
      "    【发言】<你公开说的话，≤80 字，会被场上所有人听到。必须基于上述思考，不能矛盾>",
      "    思考段帮你跨轮保持策略一致 —— 下次发言时会回放你过去的思考。",
      "12. 参考「上轮复盘」段（事实 + 推断）了解前情，从中学习推断的方向 —— 复盘是全场共享的，但你的【思考】是私人的，不要明说看了复盘。",
      "13. 参考「你的个人备忘」段（你视角下最近 3 天的他人发言 + 自己思考）保持人设和打法一致；不要在【发言】里明说「我看了备忘」。",
      "",
      `你的身份：${agent.no} 号 ${agent.name}，角色 = ${ROLE_CN[agent.role] || agent.role}，性格 = ${agent.personality.name}`,
      "",
      ROLE_GUIDES[agent.role] || "",
      "",
      "请严格按角色和性格说话，并遵守上述所有规则。",
    ].join("\n");
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

    // V2.8：私有思考日记（让 agent 跨轮策略一致）
    const myThinking = (context.me.thinkingLog || []).slice(-5);
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
      myInfo.length ? "我的隐藏信息：\n" + myInfo.join("\n") : "",
      "",
      speechesBlock,
      "",
      thinkingBlock,
      "",
      `你的状态：${myState}`,
      "",
      "请输出【思考】+【发言】两段格式。**发言必须针对已有发言做回应，不要套话，不要重复跳身份**：",
    ].filter(Boolean).join("\n");
  }

  // ===== 你视角下的最近 3 天个人记忆（摘要式，每天 ≤80 字） =====
  // 设计意图：避免把当天的原始发言+思考全塞 prompt 导致上下文爆掉。
  // 摘要由 _flushAgentMemories 调 summarize hook 生成（fallback 用规则）；
  // 原始数据仍写盘到 memory/agent-N.md，供人工 review 或外部工具按需读取。
  function buildMemoryBlock(recentMemoryDigests) {
    if (!recentMemoryDigests || recentMemoryDigests.length === 0) return "";
    const lines = ["=== 你的个人记忆（最近 3 天压缩摘要，你视角）==="];
    recentMemoryDigests.forEach(entry => {
      if (entry.digest) lines.push(`【第${entry.day}天】${entry.digest}`);
    });
    lines.push("=== 记忆结束（如需查阅当天原始发言全文，本局 memory/agent-N.md 已落盘）===");
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

    // V2.8：私有思考日记
    const myThinking = (context.me.thinkingLog || []).slice(-5);
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

    const memoryBlock = buildMemoryBlock(context.me.recentMemory);

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
      lines.push(`提示：狼人协商击杀目标。优先刀已跳神（预言家>女巫>守卫>猎人）和强发言者，避免刀队友。`);
    } else if (kind === "seer-check") {
      const checked = (context.me.seerChecks || []).map(c => `${c.no}号`).join("、");
      lines.push(`已验过：${checked || "（无）"}`);
      lines.push(`提示：优先验高怀疑度、强发言者，避免验已死/已验过的人。`);
    } else if (kind === "witch-save") {
      lines.push(`今晚被刀目标：${options.killedNo}号`);
      lines.push(`首夜可救任何人（含自己）；第 2 夜起不能自救。`);
      lines.push(`提示：首夜默认救人；其余夜优先救已跳神。`);
    } else if (kind === "witch-poison") {
      lines.push(`提示：毒药只在第 2 夜起可用。优先毒已暴露的狼或场上多跳预言家中最怀疑的那个。目标号 -1 表示今晚不毒。`);
    } else if (kind === "guard-protect") {
      const last = context.me.lastGuardedNo;
      lines.push(`上一晚守护对象：${last != null ? last + "号" : "（无）"}（不能连续守同一人）`);
      lines.push(`提示：优先守已跳预言家；其次守自己或低怀疑度发言者。`);
    } else if (kind === "run-sheriff") {
      lines.push(`提示：预言家几乎必上警；狼人按队友策略适度上警；其他角色按性格判断。`);
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
  const THINK = { type: "string", description: "你的内心分析（30-60字，会被记录到私人日记，帮你跨轮策略一致）" };
  const DECIDE_TOOLS = {
    "vote":          [{ name: "vote",            description: "投票放逐目标",         input_schema: { type: "object", properties: { thinking: THINK, target: { type: "integer", description: "玩家座位号 1-12，或 -1 弃票" } }, required: ["thinking", "target"] } }],
    "pk-vote":       [{ name: "vote",            description: "PK 复投目标",          input_schema: { type: "object", properties: { thinking: THINK, target: { type: "integer", description: "玩家座位号 1-12，或 -1 弃票" } }, required: ["thinking", "target"] } }],
    "sheriff-vote":  [{ name: "sheriff_vote",    description: "投票选警长",           input_schema: { type: "object", properties: { thinking: THINK, target: { type: "integer", description: "上警玩家座位号 1-12，或 -1 弃票" } }, required: ["thinking", "target"] } }],
    "wolf-kill":     [{ name: "wolf_kill",       description: "选择今晚击杀目标",     input_schema: { type: "object", properties: { thinking: THINK, target: { type: "integer", description: "玩家座位号 1-12" } }, required: ["thinking", "target"] } }],
    "seer-check":    [{ name: "seer_check",      description: "选择今晚查验目标",     input_schema: { type: "object", properties: { thinking: THINK, target: { type: "integer", description: "玩家座位号 1-12" } }, required: ["thinking", "target"] } }],
    "witch-save":    [{ name: "witch_save",      description: "决定是否使用解药救人", input_schema: { type: "object", properties: { thinking: THINK, save: { type: "boolean", description: "true=使用解药救人，false=不救" } }, required: ["thinking", "save"] } }],
    "witch-poison":  [{ name: "witch_poison",    description: "决定是否使用毒药及目标", input_schema: { type: "object", properties: { thinking: THINK, target: { type: "integer", description: "毒杀目标座位号 1-12，-1 表示今晚不毒" } }, required: ["thinking", "target"] } }],
    "guard-protect": [{ name: "guard_protect",   description: "选择今晚守护目标",     input_schema: { type: "object", properties: { thinking: THINK, target: { type: "integer", description: "玩家座位号 1-12" } }, required: ["thinking", "target"] } }],
    "run-sheriff":   [{ name: "run_for_sheriff", description: "决定是否上警",         input_schema: { type: "object", properties: { thinking: THINK, run: { type: "boolean", description: "true=上警，false=不上" } }, required: ["thinking", "run"] } }],
    "self-explode":  [{ name: "self_explode",    description: "决定是否立即自爆",     input_schema: { type: "object", properties: { thinking: THINK, explode: { type: "boolean", description: "true=自爆，false=不爆" } }, required: ["thinking", "explode"] } }],
    "pass-badge":    [{ name: "pass_badge",      description: "决定警徽流转目标",     input_schema: { type: "object", properties: { thinking: THINK, target: { type: "integer", description: "接徽玩家座位号 1-12，-1 撕毁" } }, required: ["thinking", "target"] } }],
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
          const data = await call("/decide", {
            system: buildSystemPrompt(payload.agent),
            user:   buildDecidePrompt(payload),
            tools,
            maxTokens: 500,   // V2.8：留空间给 thinking 字段
          });
          // V2.8：input.thinking 存进 agent.thinkingLog
          if (data.input && data.input.thinking && payload.agent && payload.agent.thinkingLog) {
            payload.agent.thinkingLog.push({
              day: payload.game?.day ?? 0,
              kind: payload.kind || "decide",
              thinking: data.input.thinking,
            });
          }
          return data.input || null;
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
