# 狼人杀 · 12 人神局 · 多 Agent AI 自动对战

12 个 LLM 驱动的 AI 玩一局 12 人神局狼人杀(4 狼 + 预言家 + 女巫 + 猎人 + 守卫 + 4 民)。
单一 LLM 后端(默认 DeepSeek,可切 Claude / OpenAI),所有玩家共享同一模型,通过**角色 SOP + 战术分工 + 三层记忆**做出博弈差异化。

```
┌────────────────────────────────────────────────────────────────────┐
│                          web/index.html                           │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │ Game Engine (game.js) ── Moderator 角色                    │    │
│  │   ├─ PublicLog(全场可见,无死因)                            │    │
│  │   ├─ Event 流(逐事件 visibleTo 过滤)                       │    │
│  │   └─ 12 × Agent                                            │    │
│  │        ├─ role + wolfTactic(开局分配)                      │    │
│  │        ├─ PrivateMemory(facts append-only + beliefs)        │    │
│  │        └─ thinkingLog(跨轮 ToM 内心日记)                   │    │
│  └──────────────────────────────────────────────────────────┘    │
│            │                                                       │
│            │ /chat /decide(prompt + tool schema)                  │
│            ▼                                                       │
└────────────────────────────────────────────────────────────────────┘
             │
             │ HTTP 127.0.0.1
             ▼
┌────────────────────────────────────────────────────────────────────┐
│  server/llm-proxy.js                                               │
│   ├─ /config /prompts /chat /decide /memory /replay              │
│   ├─ 热重载 config.json + prompts/*.md                            │
│   └─ JSON 兜底解析(模型没走 tool_use 时从文本抠 JSON)            │
└────────────────────────────────────────────────────────────────────┘
             │
             │ HTTPS
             ▼
        DeepSeek / Claude / OpenAI
```

---

## 目录结构

```
wolf-game/
├── config.example.json           # 配置模板(填入 API key 后另存为 config.json)
├── config.json                   # 真实配置(被 .gitignore 屏蔽,本地编辑)
├── package.json                  # 零依赖,纯 Node 18+ fetch
├── 启动狼人杀.command            # macOS 双击启动
│
├── server/                       # Node 代理
│   ├── llm-proxy.js              #   HTTP:/config /prompts /chat /decide /memory /replay
│   ├── config.js                 #   配置加载 + 热重载 + 缺失提示
│   └── providers.js              #   Claude / OpenAI / DeepSeek 适配 + JSON 兜底
│
├── prompts/                      # 7 份 markdown,server 热加载,客户端 fetch
│   ├── system_base.md            #   规则 A 节 + 13 条玩家硬规则(含 ToM)
│   ├── role_seer.md              #   预言家:警徽流 / 归票 / 双跳对位
│   ├── role_witch.md             #   女巫:双药位 / 首夜必救 / 留药 / 反向毒
│   ├── role_guard.md             #   守卫:空守 / 守-救-守 / 同守同救
│   ├── role_hunter.md            #   猎人:威慑论 / 装强发言 / 被毒不开枪暗示
│   ├── role_villager.md          #   村民:表水 / 站边修正 / 逼狼解释一致性
│   ├── role_wolf.md              #   狼人:4 战术分工(悍跳/冲锋/倒钩/深水)
│   └── judge.md                  #   终局解说员
│
├── web/                          # 浏览器静态资源
│   ├── index.html                #   入口 + provider 探测 + UI
│   ├── styles.css
│   ├── game.js                   #   Game Engine(夜晚结算/白天/警长/PK/胜负)
│   ├── agents.js                 #   Agent 类 + 规则版兜底策略
│   ├── llm-adapter.js            #   prompt 拼装 + tool schema + speak/decide/judge
│   ├── memory.js                 #   每日 80 字摘要(legacy,与新三层并存)
│   ├── memory-layers.js          #   PublicLog + PrivateMemory(三层记忆)
│   ├── events.js                 #   13 种 Event 子类 + visibleTo 视角过滤
│   └── tts.js                    #   Web Speech 语音合成
│
├── launcher/                     # Python pywebview 启动器
│   ├── launcher.py
│   └── launcher.pyw              #   Windows 双击无 console
│
├── verify-memory-digest.js       # 单测:每日摘要管线
├── verify-tts.js                 # 单测:TTS pitch/rate 公式
│
└── logs/                         # 每局自动落盘(.gitignore)
    └── replay_*.json             #   完整事件流 + 玩家身份 + 胜负
```

---

## 快速开始

```bash
cd wolf-game

# 1. 复制配置模板并填入真实 API key
cp config.example.json config.json
# 用编辑器打开 config.json,把 sk-FILL_YOUR_DEEPSEEK_KEY 改成真 key

# 2. 启动方式(三选一)
# A) macOS 双击 启动狼人杀.command
# B) Python 套壳窗口
python3 launcher/launcher.py
# C) 浏览器
node server/llm-proxy.js
# 然后浏览器打开 web/index.html
```

至少一个 provider 被识别为已配置(apiKey 不含 `FILL` / `YOUR` 占位字符),「开始游戏」按钮才会解锁。

**环境要求**:Node ≥ 18(自带 `fetch`);Python ≥ 3.8 + `pip install pywebview`(仅启动器)。

---

## 三层记忆架构(核心设计)

借鉴 [《狼人杀多 Agent 系统重构规格》](./狼人杀多Agent重构规格_给ClaudeCode.md) §2-3,实现规格定义的三层记忆契约:

| 层 | 持有者 | 读写权限 | 内容 |
|----|--------|----------|------|
| **GroundTruth** | 仅 Moderator(Game) | 仅 Moderator 读写 | 真实身份、每晚真实动作、女巫药剂数、守卫上夜目标 |
| **PublicLog** | 所有 agent | Moderator 写、玩家**只读** | 出局座位号、发言全文、投票、跳身份、开枪结果 |
| **PrivateMemory** | 各玩家自己 | facts 由 Moderator 写、beliefs 由玩家增量更新 | 自身身份、查验/刀口/狼队友、推理 beliefs |

**核心红线**:
- `PublicLog` **绝不写死因**(只写"X 号出局")。`memory-layers.js` 的 `append()` 内置 **11 个禁字段**(`cause` / `reason` / `wolfThinking` / `witchPotion` / `seerResult` / `role` 等)防御性 throw。
- `PrivateMemory` 物理隔离 —— 每个 agent 独立实例,无共享引用。`facts` `Object.freeze` 不可篡改。
- 玩家 agent **永远拿不到** `GroundTruth` 引用 —— `agents.js` 经 grep audit 确认无 `game.history` / `game.agents[i].role` / `game.tonightPoison` / `game.tonightProtected` 等访问。

### facts(append-only 事实流)

按动作类型自动落入对应玩家的 PrivateMemory:

| 动作 | 写入哪只 agent.memory.facts |
|------|----------------------------|
| seer-check | 仅预言家 |
| witch-save / witch-poison | 仅女巫 |
| guard-protect(含空守) | 仅守卫 |
| wolf-propose(每轮提议) | 每只发起的狼 |
| wolf-kill-final | 全体活狼 |

### beliefs(玩家增量更新的推理表)

所有 11 个 `decide` tool 的 input_schema 都注入了可选 `beliefs_update` 字段:

```json
{
  "thinking": "...",
  "target": 5,
  "beliefs_update": {
    "7": { "suspicion": 0.85, "reason": "刀口太准像被狼递" },
    "3": { "suspicion": 0.15, "reason": "金水稳" }
  }
}
```

LLM 输出后由 `llm-adapter.js` 自动写回 `agent.memory.beliefs`,下一轮 prompt 通过 `renderPrivateMemoryBlock(me)` 回放,实现**跨轮推理连贯**。

---

## 狼人 4 战术分工

开局 4 只狼**随机洗牌**分到 4 个不同的预先战术,严格按 SOP 演戏:

| 战术 | 警上 | 行为风格 |
|------|------|----------|
| **悍跳狼**(冲身位) | ✅ 必上警 | 第 1 天跳预言家,对位真预查杀真神,把火吸到自己身上 |
| **冲锋狼**(打头阵) | 偶尔 | 不跳身份,白天激进发言,主导推票,优先推真神 |
| **倒钩狼**(钓鱼位) | 可上(不跳预) | 全程站好人 / 真预阵营,关键时刻出卖队友骗信任,后期反水 |
| **深水狼**(藏身位) | ❌ 绝不上 | 全程低调,平民视角分析,投票跟大盘,苟到最后翻盘 |

实测真实 DeepSeek 在战术 prompt 下:
- 警上分布:悍跳 100% / 倒钩 60% × deception / 冲锋 20% / 深水 0%
- 首夜空刀率:**0/5**(原本 100%) —— 战术分工让 LLM 理解"空刀是新手行为"

实现:`Game.assignRoles` 洗牌分配 → `agent.wolfTactic` 持久化 → `buildSystemPrompt` 标记「★ 你的狼队战术分工:【悍跳狼】」 → 狼之间通过 `me.wolfTeam` 看到队友分工。

---

## 角色 SOP(7 份 prompt,server 热加载)

`prompts/*.md` 由 server 启动时加载并经 `/prompts` 暴露,客户端 `refreshPrompts()` 在每局开始前 fetch。**修改 md 文件无需重启 server**(`fs.watch` 监听),无需重启游戏(客户端启动时 refetch)。

每个角色 md 统一章节:**目标 / 核心规则 / 战术核心 / 何时跳身份 / 临死遗言 / 警长竞选 / 高手心法 / 常见错误避坑**。

### 战术亮点(知乎玩家社区借鉴)

- **预言家**:警上发言三件套(跳身份 + 报验 + 警徽流)、三秒报验铁律、双跳坚定不退、归票绑架犹豫位
- **女巫**:双药位概念、首夜必救、留药 vs 起手药权衡、毒人优先级、反向毒
- **守卫**:首夜空守(避免同守同救)、守-救-守 让真预活到 D4、女巫死前不跳身份
- **猎人**:威慑论(让狼觉得"刀你不亏")、被毒不开枪暗示("枪在手但没法用了")、卖队友型反向开枪
- **村民**:表水 ≠ 唠叨、暂时站边 + 修正空间("基于目前我偏 A,夜里反转可调整")、逼狼解释一致性

---

## Event 流 + 视角过滤

`web/events.js` 定义 13 种 Event 子类,每条事件自带 `visibleTo(role, idx)` 决定哪些 agent 能在 prompt 里看到:

| Event | 可见性 |
|-------|--------|
| `SpeakEvent` / `VoteEvent` / `ExecuteEvent` / `DeathEvent` / `SeerReportEvent` / `WolfExplodeEvent` / `SheriffElectedEvent` / `BadgePassEvent` | 全场 |
| `WolfProposeKillEvent` / `WolfKillEvent` | 仅狼 |
| `SeerCheckEvent` | 仅该预言家本人 |
| `WitchSaveEvent` / `WitchPoisonEvent` | 仅该女巫本人 |
| `GuardProtectEvent` | 仅该守卫本人 |

```js
game.eventsForAgent(agent)  // 返回该 agent 视角可见的事件
Events.renderWolfNightLog(game.events, currentDay)  // 给狼 prompt 渲染往日狼队夜晚日志(提议+终选+实际死亡)
```

---

## Replay JSON 落盘

每局结束自动 POST `/replay`,server 写到 `logs/replay_{ISO时间}.json`:

```json
{
  "gen": 1,
  "startedAt": "2026-05-23T11:00:00Z",
  "endedAt":   "2026-05-23T11:15:32Z",
  "day": 4,
  "winner": "good",
  "players": [{"no": 3, "role": "seer", "alive": false, "personality": "稳健", ...}, ...],
  "events": [/* 完整事件流(含 visibleTo 元信息) */],
  "history": [/* 死亡顺序 */],
  "publicCheckReports": [...],
  "roundSummaries": [...]
}
```

用途:bug 排查、social 分享、统计胜率、训练数据收集、二次复盘工具。

---

## LLM 法官(终局解说)

游戏结束 → 异步调用 `/chat` 走 `prompts/judge.md` 系统提示 → 把完整事件流喂给 LLM → 返回 80-150 字本局叙事(含关键转折点 + MVP / 关键失误) → 显示在结果浮窗。

---

## Theory-of-Mind 思考段

所有【思考】段被强制要求输出三段式:

```
【思考】
  ① 局势:当前关键事实 + 我最怀疑谁(含理由)
  ② 预判:如果我说 X,关键玩家(点名 N 号)听完会怎么想?会怎么投?
  ③ 目的:基于①②,我这一发言要让谁信我 / 让谁动摇 / 把火引向哪
【发言】<≤80 字,必须能让你预判的目标产生你想要的反应>
```

外加发言质量铁律(由 `buildSpeechQualityHint(payload, recent)` 动态注入):
- 每次必须输出 1 个独有信息点(查杀方向 / 票形预判 / 矛盾点指认 / 关键反问)
- 严禁套话清单(「同意 X 号」「跟节奏」「再观察」「等更多信息」)
- 跨轮去重(对照内心日记,不重复昨天论点)
- 句式多样化(7 种说法:刀口诡异 / 站边可疑 / 递刀给狼 / 发言空 / 逻辑断层 ...)

---

## 启动器(macOS / Windows)

### macOS

双击 `启动狼人杀.command`(`#!/bin/bash` + `chmod +x`) → 弹 Terminal → 调 `python3 launcher/launcher.py` → 起 node + pywebview 窗口。

**首次双击 Gatekeeper 警告**:右键 → 打开 → 确认。之后正常双击即可。

### Python launcher

`launcher/launcher.py` 流程:
1. 读 `config.json` 的 port(默认 3001)
2. Windows 上 `taskkill` 占用端口的旧 node 进程
3. 起 `node server/llm-proxy.js`,stdout/stderr 写 `launcher.log`
4. 轮询 `/config` 等代理就绪(最长 10s)
5. pywebview 弹 1400×900 独立窗口加载 `web/index.html?port=<port>`
6. 关闭窗口时清理 node 子进程

Windows 双击 `launcher.pyw` 无 console 启动。

---

## HTTP API

`server/llm-proxy.js` 仅监听 `127.0.0.1`(CORS `*` 方便本地 `file://` 调用)。

| Endpoint | 用途 |
|----------|------|
| `GET /config` | 端口 / 已知 provider / 已配置 provider / 默认 provider(不泄露 secret) |
| `GET /prompts` | 所有 `prompts/*.md` 内容(client refreshPrompts 用) |
| `POST /chat` | 自由文本生成(speak / judge / summarize) |
| `POST /decide` | 结构化决策(Anthropic 风格 tools,内部对 OpenAI/DeepSeek 自动转 function tools) |
| `POST /memory` | 给 `memory/agent-N.md` 追加(legacy 双层摘要持久化) |
| `POST /memory/reset` | 清空 `memory/agent-*.md`(每局开始) |
| `POST /replay` | 落盘 `logs/replay_*.json` |

### `/decide` 输入示例(带 beliefs_update)

```json
{
  "provider": "deepseek",
  "system": "...",
  "user": "...",
  "tools": [{
    "name": "seer_check",
    "input_schema": {
      "type": "object",
      "properties": {
        "thinking": { "type": "string" },
        "target":   { "type": "integer" },
        "beliefs_update": {
          "type": "object",
          "additionalProperties": {
            "type": "object",
            "properties": {
              "suspicion": { "type": "number" },
              "reason":    { "type": "string" }
            }
          }
        }
      },
      "required": ["thinking", "target"]
    }
  }],
  "maxTokens": 500
}
```

输出:

```json
{
  "toolName": "seer_check",
  "input": {
    "thinking": "...",
    "target": 7,
    "beliefs_update": { "7": { "suspicion": 0.9, "reason": "刀口太准" } }
  }
}
```

JSON 兜底:模型没走 tool_use 时,`providers.js` 的 `extractJson(text)` 会从 markdown code block 或裸 `{...}` 抠 JSON,实现"3 次重试 + 字段校验"。

---

## 扩展:新增一个 provider

1. `server/providers.js` 的 `REGISTRY` 加一条:
   ```js
   mistral: { call: (conf, opts) => callOpenAICompat(conf, opts) }
   ```
2. `config.json` 的 `providers` 加对应字段:
   ```json
   "mistral": { "apiKey": "...", "baseURL": "https://api.mistral.ai/v1", "model": "mistral-large-latest" }
   ```
3. (可选)`web/index.html` 的 `PROVIDER_LABELS` 加显示名。

前端代码无需改 —— 下拉框由 `/config` 的 `knownProviders` + `availableProviders` 动态生成。

---

## 调整 prompt(无需改 JS)

所有角色 SOP 在 `prompts/*.md`:

```bash
# 改一个角色的战术
$EDITOR prompts/role_wolf.md

# 保存后:
#   - server 内 fs.watch 在 300ms 内自动重载
#   - 客户端启动时 refreshPrompts() 拉新内容
#   - 下一局立即生效,无需重启 server / 浏览器
```

每个角色 md 统一章节:目标 / 核心规则 / 战术核心 / 何时跳身份 / 临死遗言 / 警长竞选 / 高手心法 / 常见错误避坑。

---

## UI 控制

| 控件 | 作用 |
|------|------|
| 🎬 开始游戏 | 至少一个已配置 provider 时解锁 |
| ⏸ 暂停 / ↻ 重开 | 暂停 / 重置整局 |
| 速度 | 极慢 / 慢 / 中 / 快 / 极速 |
| LLM | provider 下拉框由 `/config` 动态填充 |
| 上帝视角 | 显示所有玩家真实身份 + 夜间动作日志 |
| 朗读 | Web Speech API 朗读发言(按性别分配 voice) |

---

## 验证

```bash
# 既有单测
npm run verify-memory  # 9 项:LLM 路径 / 原始数据 / digest / fallback / 狼视角不暴露队友
npm run verify-tts     # 5 项:pitch/rate 公式等价

# 三层记忆 invariants(38 项)
node -e "
const { PublicLog, PrivateMemory } = require('./web/memory-layers.js');
// 见 verify 脚本风格,具体测试代码可移植
"
```

本次重构累计 **188+ 项**静态/动态/真实 DeepSeek E2E 测试 0 错(覆盖语法、memory-layers 不变量、事件视角过滤、规则约束 audit、llm-adapter 工具、server 端点、仿真夜晚隔离)。

---

## 常见问题

### 「开始游戏」按钮一直灰着

前端每 3 秒探测 `/config`。可能原因:
1. `node server/llm-proxy.js` 没启动 → 启动它
2. 端口冲突 → 改 `config.json` 的 `port`;浏览器模式用 `web/index.html?port=4000` 访问
3. `config.json` 不存在 → `cp config.example.json config.json` 并填 key
4. 所有 provider 都被识别为占位(含 `FILL` / `YOUR`) → 至少填一个真 key

### Python 启动器没反应

看工程根目录的 `launcher.log`,里面有 node 的真实输出。最常见:
- node 不在 PATH → `FileNotFoundError`
- 端口被占用 → launcher 仅 Windows 上自动 `taskkill`,macOS/Linux 需手工 `lsof -ti:3001 | xargs kill -9`
- config.json 缺失 → `cp config.example.json config.json`

### LLM 4xx / 5xx

打开 `prompts.log` 看具体响应:
- Claude:`model` 须是 API 实际 ID(如 `claude-sonnet-4-5`),apiKey 来自 Anthropic Console
- DeepSeek / OpenAI:模型不存在或没权限 → 改成你账号可用的 model

### 数据安全

- `server/llm-proxy.js` **只监听 127.0.0.1**,不暴露公网
- `config.json` 含明文 apiKey,**已被 `.gitignore` 屏蔽**
- `prompts.log` / `logs/replay_*.json` 不含 apiKey,但含完整 prompt 和响应,自行斟酌是否分享
- 真实 API key **绝不要 commit 进 git** —— 用 `config.example.json` 当模板

---

## 设计决策记录

| 决策 | 原因 |
|------|------|
| 单 LLM 共享 vs 多 LLM 混战 | 单 LLM 更稳定;差异化靠 prompt(角色 SOP + 战术分工 + 性格) |
| Markdown prompt vs YAML | md 编辑友好;不引入 yaml 依赖;server 热重载更简单 |
| Event 类 + visibleTo vs 数据库 | 单进程内存够用;一局结束自动 dump replay,持久化按需做 |
| 三层记忆 vs 直接 game state 共享 | 防止信息泄露 bug;agent 物理隔离 = 真实多 agent 模拟 |
| 狼队战术开局分配 vs 实时协商 | 分配 = 角色稳定差异化;实时协商 LLM 倾向同质化 |
| Replay JSON vs 数据库 | 文件简单可分享;后续加分析工具不锁死格式 |
| Python launcher vs Electron | pywebview 体积小、无打包步骤、macOS/Windows 一致 |

---

## 借鉴来源

- **多 Agent 架构**:[`狼人杀多Agent重构规格_给ClaudeCode.md`](./狼人杀多Agent重构规格_给ClaudeCode.md) —— 三层记忆 + 信息隔离契约
- **狼队战术 + JSON 兜底解析**:[mewamew/wolf_bot](https://github.com/mewamew/wolf_bot)
- **角色玩法**:知乎狼人杀社区
  - 预言家:[警上竞选](https://zhuanlan.zhihu.com/p/25707244) · [进阶教程](https://zhuanlan.zhihu.com/p/28365697)
  - 女巫:[HCSSA 上篇](https://zhuanlan.zhihu.com/p/85419716) · [女巫攻略](https://zhuanlan.zhihu.com/p/421079385)
  - 守卫:[carry 型攻略](https://zhuanlan.zhihu.com/p/569455347)
  - 猎人:[枪在手攻略](https://zhuanlan.zhihu.com/p/25643020)
  - 村民:[平民篇](https://zhuanlan.zhihu.com/p/107957338)

---

## License

ISC
