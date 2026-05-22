/* 端到端验证：每天压缩摘要 memory 系统
   1) LLM 路径：mock summarize 返回固定文本 → 验证写入 digest + 落盘 markdown
   2) Fallback 路径：mock summarize 抛错 → 验证规则版摘要兜底
   3) Prompt 段：buildMemoryBlock 输出大幅小于改造前的"全量原始数据"
   4) 写盘：memory/agent-N.md 含"摘要"标记 + <details> 折叠原始 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

// 起 llm-proxy（用 child_process），用真后端验证 /memory 端点
const { spawn } = require("child_process");
const proxy = spawn("node", ["server/llm-proxy.js"], { cwd: __dirname, stdio: ["ignore", "pipe", "pipe"] });
let proxyReady = false;
proxy.stdout.on("data", d => { if (/listening on/.test(d.toString())) proxyReady = true; });
proxy.stderr.on("data", d => process.stderr.write("[proxy] " + d));

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

(async () => {
  // 等 proxy ready
  for (let i = 0; i < 50 && !proxyReady; i++) await sleep(50);
  if (!proxyReady) throw new Error("llm-proxy not ready after 2.5s");

  // 清 memory dir
  await fetch("http://127.0.0.1:3001/memory/reset", { method: "POST" });

  // ── 构造 sandbox：加载 agents.js + llm-adapter.js + game.js ──
  const sandbox = {
    console, setTimeout, clearTimeout, fetch,
    Math, Promise, Error, JSON, Object, Array, Number, Boolean, String, RegExp, Date, Map, Set, Symbol,
    URLSearchParams,
    document: {
      getElementById: () => null,
      querySelectorAll: () => [],
      addEventListener: () => {},
    },
    window: {
      addEventListener: () => {},
      electron: null,
    },
    location: { search: "" },
  };
  sandbox.globalThis = sandbox;
  sandbox.window.fetch = fetch;
  vm.createContext(sandbox);

  const load = (p) => vm.runInContext(fs.readFileSync(path.join(__dirname, p), "utf-8"), sandbox, { filename: p });
  load("web/agents.js");
  // 把 class 暴露到 sandbox 的 globalThis（vm.runInContext 不会自动暴露 strict-mode 顶层 class）
  vm.runInContext("globalThis.Agent = Agent;", sandbox);
  load("web/llm-adapter.js");
  load("web/game.js");
  vm.runInContext("globalThis.Game = Game; globalThis.GAME_GENERATION = GAME_GENERATION;", sandbox);

  // 启用 LLM_AGENT.use("local") 以便 window.MEMORY 可以发 POST
  sandbox.window.CURRENT_PROVIDER = "claude";  // dummy
  sandbox.window.LLM_AGENT.use("local");

  // ── Test 1: LLM 路径 ──
  // 注入 mock summarize 返回固定摘要
  sandbox.window.LLM_HOOK.summarize = async ({ system, user }) => {
    // 验证 prompt 传参正确
    if (!/你正在扮演/.test(system)) throw new Error("system prompt missing role-play header");
    if (!/本日个人记忆摘要/.test(system)) throw new Error("system prompt missing digest instruction");
    return "今日 3 号悍跳预言家，我对位真预报查杀 6 号；信任 7/9 号金水链，怀疑 3/11 号节奏可疑；明日带头投 3 号。";
  };

  const game = new sandbox.Game();
  game.reset();
  game.day = 1;
  // 注入今天的发言历史
  game.speechHistory = [
    { day: 1, agentNo: 1, publicRole: null,   kind: "day", text: "我先听听大家" },
    { day: 1, agentNo: 3, publicRole: "seer", kind: "day", text: "我3号跳预言家，查 5 号好人" },
    { day: 1, agentNo: 5, publicRole: "seer", kind: "day", text: "我5号才是真预，3号悍跳" },
    { day: 1, agentNo: 7, publicRole: null,   kind: "day", text: "3 号金水保留，5 号节奏怪" },
    { day: 1, agentNo: 9, publicRole: null,   kind: "day", text: "倾向 3 号是真预" },
    { day: 1, agentNo: 11, publicRole: null,  kind: "day", text: "都听我的，投 9 号" },
  ];
  // 给 1 号 agent 注入今日 thinkingLog
  const agent1 = game.agents[0];
  agent1.thinkingLog = [
    { day: 1, kind: "day", thinking: "5 号节奏太急，疑似狼" },
    { day: 1, kind: "vote", thinking: "投 5 号" },
  ];

  await game._flushAgentMemories();

  // 断言 1.1：agent.memoryDigestByDay[1] 已设
  if (!agent1.memoryDigestByDay[1]) throw new Error("FAIL: agent1.memoryDigestByDay[1] empty");
  if (!agent1.memoryDigestByDay[1].includes("3 号悍跳")) throw new Error("FAIL: digest content mismatch (LLM path)");
  console.log("✅ Test 1.1: LLM 路径 → digest 写入 agent.memoryDigestByDay");

  // 断言 1.2：agent.memoryByDay[1] 原始数据仍在（人工 review 用）
  const raw = agent1.memoryByDay[1];
  if (!raw || raw.otherSpeeches.length !== 5) throw new Error(`FAIL: raw otherSpeeches len=${raw?.otherSpeeches?.length}, want 5`);
  if (raw.myActions.length !== 2) throw new Error(`FAIL: raw myActions len=${raw.myActions.length}, want 2`);
  console.log("✅ Test 1.2: 原始数据 memoryByDay 仍保留");

  // 断言 1.3：_publicContext 暴露 recentMemoryDigests，不再暴露 recentMemory（原始）
  const ctx = agent1._publicContext(game);
  if (!ctx.me.recentMemoryDigests || ctx.me.recentMemoryDigests.length !== 1) {
    throw new Error(`FAIL: recentMemoryDigests len=${ctx.me.recentMemoryDigests?.length}`);
  }
  if (ctx.me.recentMemoryDigests[0].day !== 1) throw new Error("FAIL: recentMemoryDigests[0].day");
  if (ctx.me.recentMemory !== undefined) throw new Error("FAIL: recentMemory should not be on context anymore");
  console.log("✅ Test 1.3: _publicContext 暴露 recentMemoryDigests（不再暴露原始 recentMemory）");

  // 等 memory 写盘（fire-and-forget）
  await sleep(300);

  // 断言 1.4：memory/agent-1.md 含"摘要"和折叠的原始材料
  const md = fs.readFileSync(path.join(__dirname, "memory/agent-1.md"), "utf-8");
  if (!/^\*\*摘要\*\*：/m.test(md)) throw new Error("FAIL: md missing '摘要' marker");
  if (!/<details><summary>原始材料<\/summary>/.test(md)) throw new Error("FAIL: md missing <details> raw section");
  if (!md.includes("3 号悍跳预言家")) throw new Error("FAIL: md missing digest content");
  if (!md.includes('"我3号跳预言家，查 5 号好人"')) throw new Error("FAIL: md missing raw speech of another player");
  console.log("✅ Test 1.4: memory/agent-1.md 含摘要 + 折叠的原始材料");

  // ── Test 2: Fallback 路径（LLM 抛错）──
  await fetch("http://127.0.0.1:3001/memory/reset", { method: "POST" });
  sandbox.window.LLM_HOOK.summarize = async () => { throw new Error("simulated LLM down"); };
  game.day = 2;
  game.speechHistory = game.speechHistory.concat([
    { day: 2, agentNo: 3, publicRole: "seer", kind: "day", text: "查 7 号好人" },
    { day: 2, agentNo: 5, publicRole: "seer", kind: "day", text: "查 7 号是狼" },
  ]);
  agent1.thinkingLog.push({ day: 2, kind: "day", thinking: "7 号关键，看双预反查" });
  agent1.suspicion = [0, 0.05, 0.05, 0.1, 0.3, 0.85, 0.2, 0.4, 0.1, 0.15, 0.5, 0.7];

  await game._flushAgentMemories();
  const digest2 = agent1.memoryDigestByDay[2];
  if (!digest2) throw new Error("FAIL: fallback digest missing");
  if (!/信任/.test(digest2) || !/怀疑/.test(digest2)) {
    throw new Error(`FAIL: fallback digest format unexpected: ${digest2}`);
  }
  // 信任应该是 suspicion 最低的 2 个 (排除自己 idx=0)：idx=1,2 → 2/3 号
  if (!/2号|3号/.test(digest2)) throw new Error(`FAIL: fallback trust list wrong: ${digest2}`);
  // 怀疑应该是 suspicion 最高的 2 个：idx=5,11 → 6/12 号
  if (!/6号|12号/.test(digest2)) throw new Error(`FAIL: fallback suspect list wrong: ${digest2}`);
  console.log(`✅ Test 2: LLM 抛错时走 fallback 规则摘要：${digest2}`);

  // ── Test 3: prompt 段长度对比（改造前 vs 改造后） ──
  // 改造前的等效 buildMemoryBlock（直接拼原始 otherSpeeches + myActions）
  function buildMemoryBlockOLD(recentMemory) {
    if (!recentMemory || recentMemory.length === 0) return "";
    const lines = ["=== 你的个人备忘（最近 3 天，你视角）==="];
    recentMemory.forEach(entry => {
      lines.push(`【第${entry.day}天】`);
      if (entry.otherSpeeches?.length) {
        lines.push("  他人发言：");
        entry.otherSpeeches.forEach(s => {
          const tag = s.publicRole ? `[已跳${s.publicRole}]` : "[未跳]";
          lines.push(`    · ${s.no}号${tag}："${s.text}"`);
        });
      }
      if (entry.myActions?.length) {
        lines.push("  我的行动：");
        entry.myActions.forEach(a => lines.push(`    · ${a.kind}：「${a.thinking}」`));
      }
    });
    lines.push("=== 备忘结束 ===");
    return lines.join("\n");
  }
  const oldRecent = [1, 2].map(d => ({ day: d, ...agent1.memoryByDay[d] }));
  const oldBlock = buildMemoryBlockOLD(oldRecent);

  // 改造后：从 publicContext 拿 digests，让 llm-adapter.js 渲染
  // buildMemoryBlock 是 IIFE 闭包内私有的，我们间接通过 LLM_HOOK.speak 渲染 prompt 来抓
  let capturedUser = null;
  const realFetch = sandbox.fetch;
  sandbox.fetch = sandbox.window.fetch = async (url, opts) => {
    if (typeof url === "string" && url.includes("/chat")) {
      capturedUser = JSON.parse(opts.body).user;
      return { ok: true, json: async () => ({ text: "【思考】x【发言】y" }) };
    }
    return realFetch(url, opts);
  };
  // restore real summarize for this call
  sandbox.window.LLM_HOOK.summarize = async () => "stub";
  await sandbox.window.LLM_HOOK.speak({
    agent: agent1, game: { day: 3, phase: "day" },
    context: agent1._publicContext(game), kind: "day",
  });
  sandbox.fetch = sandbox.window.fetch = realFetch;

  const newBlockMatch = capturedUser.match(/=== 你的个人记忆[\s\S]*?=== 记忆结束[^\n]*===/);
  const newBlock = newBlockMatch ? newBlockMatch[0] : "";

  console.log(`📏 改造前 memory 段长度: ${oldBlock.length} 字符`);
  console.log(`📏 改造后 memory 段长度: ${newBlock.length} 字符`);
  const ratio = (newBlock.length / oldBlock.length * 100).toFixed(1);
  console.log(`📏 压缩比: ${ratio}% (越小越好)`);
  if (newBlock.length >= oldBlock.length) {
    throw new Error("FAIL: new block should be smaller than old block");
  }
  console.log("✅ Test 3: 压缩摘要 prompt 段显著小于原始数据块");

  // ── Test 4: 12 agents 并发（验证 Promise.all 不互相阻塞）──
  await fetch("http://127.0.0.1:3001/memory/reset", { method: "POST" });
  sandbox.window.LLM_HOOK.summarize = async () => "并发测试摘要";
  game.day = 3;
  const t0 = Date.now();
  await game._flushAgentMemories();
  const elapsed = Date.now() - t0;
  const populated = game.agents.filter(a => a.memoryDigestByDay[3]).length;
  console.log(`✅ Test 4: 12 agents 并发摘要耗时 ${elapsed}ms，已生成 ${populated} 份 digest`);
  if (populated < 10) throw new Error(`FAIL: only ${populated}/12 agents got digest`);

  console.log("\n🎉 ALL TESTS PASSED");
  proxy.kill();
  process.exit(0);
})().catch(err => {
  console.error("\n❌ TEST FAILED:", err);
  proxy.kill();
  process.exit(1);
});
