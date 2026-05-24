# 狼人杀 · 12 人神局 · 多 Agent AI 自动对战

12 个 LLM 驱动的 AI 玩一局 12 人神局狼人杀（4 狼 + 预言家 + 女巫 + 猎人 + 守卫 + 4 民）。**单一 LLM 后端**（默认 DeepSeek，可切 Claude / OpenAI），所有玩家共享同一模型，靠 **角色 SOP + 狼队战术分工 + 三层记忆隔离** 做出博弈差异化。

```
浏览器 (web/)
  ├─ Game Engine (game.js) ──────── Moderator，握有 GroundTruth
  ├─ 12 × Agent (agents.js)         身份/性格/PrivateMemory，物理隔离
  ├─ LLM Adapter (llm-adapter.js)   prompt 组装 + tool schema
  └─ Memory / Events / TTS
        │
        │ HTTP 127.0.0.1
        ▼
server/llm-proxy.js  ──── /config /chat /decide /memory /replay
        │
        ▼
   DeepSeek / Claude / OpenAI
```

---

## 快速开始

```bash
cd wolf-game

# 1. 配置 API key（config.json 已 .gitignore，不会入库）
cp config.example.json config.json
$EDITOR config.json   # 把 sk-FILL_YOUR_DEEPSEEK_KEY 改成真 key

# 2. 启动（三选一）
./启动狼人杀.command            # macOS 双击
python3 launcher/launcher.py   # pywebview 套壳窗口
node server/llm-proxy.js       # 纯浏览器：起完打开 web/index.html
```

环境：Node ≥ 18（自带 `fetch`）；Python ≥ 3.8 + `pywebview`（仅启动器）。
至少一个 provider 配了真 key（apiKey 不含 `FILL`/`YOUR`）时「开始游戏」才解锁。

---

## 三大核心设计

### 1. 三层记忆 + 信息隔离

| 层 | 持有者 | 写入 | 读取 | 内容 |
|----|--------|------|------|------|
| **GroundTruth** | Moderator | Moderator | Moderator | 真实身份、夜间真实动作、女巫药剂、守卫上夜目标 |
| **PublicLog** | 全场 | Moderator | 玩家只读 | 出局座位号、发言全文、投票、跳身份、开枪结果 |
| **PrivateMemory** | 各玩家自己 | facts 由 Moderator 写、beliefs 玩家自更新 | 仅本人 | 自身身份、查验/刀口/狼队友、跨轮推理表 |

**红线**：
- `PublicLog` **绝不写死因**，只写"X 号出局"。`memory-layers.js#append` 内置 11 个禁字段（`cause`/`role`/`witchPotion` 等）防御性 throw。
- `PrivateMemory` 物理隔离，每个 agent 独立实例，`facts` `Object.freeze` 不可篡改。
- 玩家 agent **永远拿不到** `GroundTruth` 引用。

#### 记忆注入 prompt 的窗口（agents.js 顶部常量）

| 常量 | 默认 | 含义 |
|------|------|------|
| `RECENT_SPEECH_DAYS` | 3 | 注入最近 N 天他人发言（每条截 `SPEECH_TEXT_MAX`=120 字） |
| `KEY_THINKING_DAYS`  | 4 | 注入最近 N 天自己的关键决策（vote/sheriff/day/last-words/夜间神职动作） |
| `KEY_THINKING_PER_DAY` | 3 | 每天最多保留 N 条关键 thinking |
| `RECENT_DAYS` (memory.js) | 3 | 注入最近 N 天 ≤80 字 LLM digest |

每天结束 `Memory.flushAll` 把当日他人发言全文 + 自己思考全文落到 `memory/agent-N.md`（人类 review 用），同时让 LLM 生成 80 字 digest 供后续天数 prompt 注入。

### 2. 狼队 4 战术分工

开局 4 只狼**随机洗牌**分到 4 个不同战术，严格按 SOP 演戏：

| 战术 | 上警 | 风格 |
|------|------|------|
| **悍跳狼** | ✅ 必上 | 第 1 天跳预言家，对位真预查杀真神，把火吸到自己身上 |
| **冲锋狼** | 偶尔 | 不跳身份，白天激进发言，主导推票，优先推真神 |
| **倒钩狼** | 可上（不跳预） | 全程站好人/真预阵营，关键时刻出卖队友骗信任，后期反水 |
| **深水狼** | ❌ 绝不上 | 全程低调，平民视角分析，投票跟大盘，苟到最后翻盘 |

实测真实 DeepSeek 下首夜空刀率从原本 ~100% 降到 0/5。

### 3. Provider 抽象 + Tool Schema 适配

`server/providers.js` 的 `REGISTRY` 把三家厂商统一成 `{ call(conf, opts) }`：

- **Claude**：`POST /v1/messages` + `tools` + `tool_choice: "any"`
- **OpenAI / DeepSeek**：`POST /chat/completions` + `tools` 自动转 function tools + `tool_choice: "required"`
- **JSON 兜底**：模型没走 tool_use 时，`extractJson(text)` 从 markdown code block 或裸 `{...}` 抠 JSON，配合 3 次重试 + 字段校验

加新 provider 只需在 `REGISTRY` 加一行 + `config.json` 加字段，前端 `/config` 探测会自动出现在下拉框。

---

## 目录结构

```
wolf-game/
├── config.example.json           # 配置模板（填 key 后另存为 config.json）
├── package.json                  # 零依赖，纯 Node 18+ fetch
├── 启动狼人杀.command            # macOS 双击启动
│
├── server/                       # Node 代理
│   ├── llm-proxy.js              #   /config /chat /decide /memory /replay
│   ├── config.js                 #   配置加载 + 热重载
│   └── providers.js              #   Claude / OpenAI / DeepSeek 适配
│
├── prompts/                      # 7 份角色 SOP（md，server fs.watch 热加载）
│   ├── system_base.md            #   规则 A 节 + 13 条玩家硬规则（含 ToM）
│   ├── role_{seer,witch,guard,hunter,villager,wolf}.md
│   └── judge.md                  #   终局解说员
│
├── web/                          # 浏览器静态资源
│   ├── index.html / styles.css
│   ├── game.js                   #   Game Engine（夜结算/白天/警长/PK/胜负）
│   ├── agents.js                 #   Agent 类 + 规则版兜底策略
│   ├── llm-adapter.js            #   prompt 组装 + tool schema + speak/decide/judge
│   ├── memory.js                 #   每日 80 字 digest（落盘 memory/agent-N.md）
│   ├── memory-layers.js          #   PublicLog + PrivateMemory（三层记忆契约）
│   ├── events.js                 #   13 种 Event 子类 + visibleTo 视角过滤
│   └── tts.js                    #   Web Speech 语音（12 agent 普通话 1:1 不重复，男女匹配）
│
├── launcher/                     # Python pywebview 启动器
│   ├── launcher.py
│   └── launcher.pyw              #   Windows 双击无 console
│
├── memory/                       # 每局 agent-N.md（运行时生成，.gitignore）
├── logs/                         # 每局 replay_*.json（运行时生成，.gitignore）
└── 狼人杀多Agent重构规格_给ClaudeCode.md
```

---

## 验证

```bash
npm run verify-memory    # 端到端：digest 生成 / fallback / 狼视角不暴露队友
npm run verify-tts       # TTS：pitch/rate 公式 + 12 agent 1:1 voice 分配
```

`verify-memory-digest.js` 会自起 `llm-proxy.js`（占用 3001），若 launcher 在跑需先停掉。

---

## 调试 / 改 prompt

- **改角色战术**：直接编辑 `prompts/*.md`，server `fs.watch` 自动重载，下一局生效（无需重启）
- **看每局完整发言/思考**：`memory/agent-N.md`（按 agent 按天组织，含 `<details>` 折叠原始材料）
- **看完整事件流**：`logs/replay_*.json`（含视角元信息，可用于 bug 排查、统计胜率、二次复盘）
- **看 LLM 实际请求/响应**：`prompts.log`（每次 `/chat` `/decide` 的 prompt + 回包）
- **改注入 prompt 的记忆窗口**：调 `web/agents.js` 顶部的 `RECENT_SPEECH_DAYS` / `KEY_THINKING_DAYS` 等常量

---

## TTS 语音（Web Speech API）

12 agent 全用**标准普通话**（自动过滤 zh-HK 粤语和方言 voice），按 `GENDERS` 表男女匹配，**1:1 不重复分配**（见 `tts.js#assignVoices`）。同性别池不足时跨性别借用、仍保不重复；voice 总数 < 12 时退化为重复 + console.warn。

依赖系统/浏览器实际安装的中文 voice：macOS 系统自带的普通话 voice 基本全是女声（Tingting 等），**男女区分效果在 Edge 浏览器上最好**（自带 Microsoft Online Neural 系列含 Yunjian/Yunxi 等多个男声）。

---

## 常见问题

**「开始游戏」按钮一直灰着** — 前端每 3 秒探测 `/config`。检查：
1. `node server/llm-proxy.js` 没启动？2. 端口冲突？改 `config.json` 的 `port`，浏览器模式用 `?port=4000` 3. `config.json` 不存在或 key 全是占位 → 复制 example 并填一个真 key

**LLM 4xx / 5xx** — 看 `prompts.log` 具体响应。最常见：`model` 字段对不上你账号实际可用的 model ID。

**端口被占** — macOS/Linux：`lsof -ti:3001 | xargs kill -9`；Windows launcher 会自动 taskkill。

**数据安全** — `llm-proxy.js` 只监听 `127.0.0.1`；`config.json` 已 `.gitignore`；`prompts.log` 和 replay 不含 key 但含完整 prompt，自行斟酌分享。

---

## 借鉴

- 三层记忆契约：[`狼人杀多Agent重构规格_给ClaudeCode.md`](./狼人杀多Agent重构规格_给ClaudeCode.md)
- 狼队战术 + JSON 兜底解析：[mewamew/wolf_bot](https://github.com/mewamew/wolf_bot)
- 角色玩法：知乎狼人杀社区（预言家 / 女巫 / 守卫 / 猎人 / 村民攻略）

## License

ISC
