# 狼人杀 · 12 人神局 · AI Agent 自动对战

12 个 LLM 驱动的 AI Agent 自动玩一局 12 人神局狼人杀（4 狼 + 预言家 + 女巫 + 猎人 + 守卫 + 4 民）。
本地 Node 代理统一对接 3 个 LLM 后端：**Claude API / DeepSeek / OpenAI**，前端 UI 实时切换。

```
┌────────────────────┐    HTTP    ┌────────────────────┐    HTTPS   ┌────────────────────┐
│  web/index.html    │ ─────────► │  server/llm-proxy  │ ─────────► │  Claude / DeepSeek │
│  (前端 UI + 引擎)   │  127.0.0.1 │  (config + dispatch)│            │  / OpenAI          │
└────────────────────┘            └────────────────────┘            └────────────────────┘
        ▲                                  ▲
        │ pywebview 独立窗口（可选）        │ 读取 config.json（支持热重载）
        └─── launcher/launcher.py ─────────┘
```

---

## 目录结构

```
wolf-game/
├── config.json               # 唯一配置文件（顶层，server/launcher 共享）
├── package.json              # 无运行时依赖，纯 Node 18+ fetch
├── README.md
│
├── server/                   # 后端 Node 代理（功能层）
│   ├── llm-proxy.js          #   HTTP 服务：/config /chat /decide
│   ├── config.js             #   配置加载 + 热重载（fs.watchFile）
│   └── providers.js          #   3 个 provider 适配器 + dispatch
│
├── web/                      # 前端静态资源（浏览器加载即用）
│   ├── index.html            #   入口 + provider 探测 + UI 状态机
│   ├── styles.css
│   ├── game.js               #   游戏引擎：昼夜流程、投票、PK、警长、TTS
│   ├── agents.js             #   Agent 类、性格、规则版兜底策略
│   └── llm-adapter.js        #   把 game 状态打包成 prompt + tools 调代理
│
└── launcher/                 # Python 启动器（可选，绕开 Electron/EDR 拦截）
    ├── launcher.py           #   pywebview 套壳 + 自动起停 node
    └── launcher.pyw          #   Windows 双击无 console 入口
```

**配置与机能彻底解耦**：

- 端口、provider 列表、模型名、API key 都在 `config.json` 中，代码里零硬编码。
- 前端 provider 下拉框由 `GET /config` 动态填充（`knownProviders` + `availableProviders`），增删 provider 只改 `providers.js` 注册表 + `config.json`，前端无需改 HTML。
- 启动器读 `config.json` 拿端口，并通过 URL `?port=xxx` 把端口透传给前端，无需手工对齐。

---

## 快速开始

```bash
cd wolf-game

# 1. 编辑 config.json，至少填入一个 provider 的真实 apiKey
#    （含 FILL / YOUR 的占位会被识别为「未配置」）

# 2. 启动代理（Node 18+，无需 npm install）
node server/llm-proxy.js

# 3. 用浏览器打开 web/index.html
#    或者：python launcher/launcher.py（pywebview 独立窗口）
```

至少一个 provider 被识别为已配置，前端「开始游戏」按钮才会解锁。

---

## 环境要求

| 组件 | 版本 | 是否必需 |
|------|------|----------|
| Node.js | ≥ 18（自带 `fetch`） | ✅ |
| 浏览器 | Chrome / Edge / Firefox 最新版 | 二选一即可 |
| Python | ≥ 3.8 + `pywebview` | 仅用 launcher 时需要 |

**无运行时 npm 依赖**（`package.json` 不声明 dependencies），代理用 Node 原生 `fetch`。

Python 启动器需要 pywebview：

```bash
pip install pywebview
```

---

## 配置 config.json

```json
{
  "defaultProvider": "claude",
  "port": 3001,
  "maxTokens": 200,
  "providers": {
    "claude": {
      "apiKey": "sk-ant-...",
      "baseURL": "https://api.anthropic.com",
      "model": "claude-sonnet-4-5"
    },
    "deepseek": {
      "apiKey": "sk-...",
      "baseURL": "https://api.deepseek.com/v1",
      "model": "deepseek-chat"
    },
    "openai": {
      "apiKey": "sk-...",
      "baseURL": "https://api.openai.com/v1",
      "model": "gpt-4o-mini"
    }
  }
}
```

字段说明：

| 字段 | 含义 |
|------|------|
| `port` | 代理监听端口（默认 3001），launcher 会自动读取并透传给前端 |
| `maxTokens` | 默认值，单次请求被 client 覆盖时优先使用 client 值 |
| `defaultProvider` | client 不指定时的兜底；如果该 provider 未配置，自动降级为可用列表首项 |
| `providers.<name>` | 各 provider 的 `apiKey` / `baseURL` / `model` |

**关键行为**：

- `apiKey` 含 `FILL` 或 `YOUR` 子串（不区分大小写）被识别为占位 → 该 provider 视为未配置 → UI 下拉框对应项标灰为 `(未配置)`。
- `config.json` **支持热重载**：保存后约 1 秒生效，无需重启 `node server/llm-proxy.js`。前端每 3 秒重探 `/config`，UI 状态会自动更新。
- 模型名 `claude-sonnet-4-5` / `deepseek-chat` / `gpt-4o-mini` 是示例，请按你账号实际可用模型替换。

---

## 启动方式

### A. 浏览器（最简单）

```bash
node server/llm-proxy.js          # 控制台输出 listening on http://127.0.0.1:3001
```

然后双击 `web/index.html` 或拖进浏览器。

### B. Python pywebview 套壳窗口

```bash
python launcher/launcher.py
```

`launcher.py` 会：

1. 读 `config.json` 的 `port`（默认 3001）
2. Windows 上 `taskkill` 占用该端口的旧 node 进程
3. 起 `node server/llm-proxy.js`，stdout/stderr 写入 **`launcher.log`** 便于排错
4. 轮询 `/config` 等代理就绪（最长 10s）
5. pywebview 弹 1400×900 独立窗口加载 `web/index.html?port=<port>`
6. 窗口关闭时 `proc.terminate()` 清理 node 子进程

> **Windows 双击 `launcher.pyw` 即可无 console 启动**。如果代理起不来，看工程根目录的 `launcher.log`。

---

## UI 操作

| 控件 | 作用 |
|------|------|
| 🎬 开始游戏 | 至少一个已配置 provider 时解锁 |
| ⏸ 暂停 / ↻ 重开 | 暂停 / 重置整局 |
| 速度 | 极慢 / 慢 / 中 / 快 / 极速 |
| LLM | provider 下拉框由 `/config` 动态填充，未配置项灰显 |
| 上帝视角 | 显示所有玩家真实身份 |
| 朗读 | Web Speech API 朗读发言（按性别分配 voice） |

运行后工程根目录会出现：

- `prompts.log` — 每次 `/chat` 和 `/decide` 的 system / user / tools / response
- `launcher.log` — Python 启动器抓的 node stdout/stderr（仅 launcher 模式）
- `memory/agent-N.md` — 每个 Agent 自己的本局备忘（见下文）

## Agent 本局记忆 (memory/agent-N.md)

**memory tool 风格的分层设计**：避免把原始流水账全塞 prompt，每天用 LLM 生成 ≤80 字摘要喂给后续决策，原始数据写盘供人工 review。

### 双层结构

| 层 | 内容 | 进 prompt？ | 写盘？ |
|---|---|---|---|
| **压缩摘要** `agent.memoryDigestByDay[day]` | LLM 生成的 ≤80 字本日个人记忆 | ✅ 最近 3 天 | ✅ markdown 顶部 |
| **原始数据** `agent.memoryByDay[day]` | 当天他人发言 + 自己思考动作全文 | ❌ 不进 prompt | ✅ `<details>` 折叠区 |

每天投票结束后，`game._flushAgentMemories()` 会给每个存活 Agent 调一次 LLM：

```
你正在扮演 N 号 ...（角色 · 性格）。请用第一人称写一段 ≤80 字的【本日个人记忆摘要】
作为你明天打牌的依据。要点：今日最关键判断、谁可信、谁可疑、明天计划。
```

LLM 失败时走规则 fallback（基于 suspicion 排出"信任/怀疑"名单）。

### markdown 结构

```markdown
# Agent 3 · 阿狸 · 预言家 · 稳健

## 第 1 天

**摘要**：今日 5 号悍跳，我对位真预报查 6 号查杀；信任 7/9 号金水，怀疑 5/11 号节奏可疑；明日投 5 号。

<details><summary>原始材料</summary>

**他人发言**：
- 1号[未跳]（白天发言）："..."
- 5号[已跳预言家]（白天发言）："..."

**我的行动**：
- speak: 「今天必须报查验，对位 5 号悍跳」
- vote: 「7 号查杀稳投」

</details>
```

### 用途

- **决策回灌（压缩摘要）**：next day 的 prompt 自动附带最近 3 天的 ≤80 字摘要（共 ≤240 字），远小于原始流水账（每天几百到上千字）
- **避免上下文爆掉**：实测压缩到 ~50%；真实长发言场景下能压缩到 ~20%
- **人工复盘（原始数据）**：游戏结束后直接看 `memory/agent-N.md`，能完整还原每个 Agent 的视角和决策链
- **仅本局**：每次「开始游戏」会先 `POST /memory/reset` 清掉上局所有 `agent-*.md`

### 端点

```
POST /memory        body: { agentNo, header?, content }   # 追加（首次写带 header）
POST /memory/reset                                          # 删除 memory/agent-*.md
```

`agentNo` 必须是 1-99 的整数，路径穿越会被拒（`{"error":"bad agentNo: ..."}`）。

### 验证

```bash
npm run verify-memory
```

会起一个本地 llm-proxy + 用 mock 数据跑 4 组测试：(1) LLM 路径生效 (2) LLM 失败时 fallback (3) prompt 段压缩率 (4) 12 agent 并发摘要。

---

## HTTP API

代理仅监听 `127.0.0.1`，CORS 全开（方便本地 `file://` 加载 HTML 调用）。

### `GET /config`

返回端口、所有已知 provider、当前已配置 provider、默认 provider。**不泄露任何 secret**。

```json
{
  "port": 3001,
  "knownProviders": ["claude", "openai", "deepseek"],
  "availableProviders": ["claude"],
  "default": "claude"
}
```

### `POST /chat`

文本发言。

```json
{
  "provider": "claude",
  "system": "你是 3 号 …",
  "user": "当前阶段：第 1 天 白天发言 …",
  "maxTokens": 500
}
```

响应：

```json
{ "text": "【思考】… 【发言】我 3 号，跳预言家，昨晚验 7 号是狼。" }
```

### `POST /decide`

结构化决策（投票 / 夜行动）。前端传 **Anthropic 风格 tools**，代理内部对 OpenAI / DeepSeek 自动转 function tools。

```json
{
  "provider": "claude",
  "system": "…",
  "user": "…",
  "tools": [{
    "name": "vote",
    "description": "投票放逐目标",
    "input_schema": {
      "type": "object",
      "properties": {
        "thinking": { "type": "string" },
        "target":   { "type": "integer" }
      },
      "required": ["thinking", "target"]
    }
  }]
}
```

响应：

```json
{ "toolName": "vote", "input": { "thinking": "7 号查杀稳投", "target": 7 } }
```

---

## 扩展：新增一个 provider

1. **`server/providers.js`** 的 `REGISTRY` 加一条：

   ```js
   const REGISTRY = {
     claude:   { call: (conf, opts) => callClaude(conf, opts) },
     deepseek: { call: (conf, opts) => callOpenAICompat(conf, { ...opts, extraBody: { thinking: { type: "disabled" } } }) },
     openai:   { call: (conf, opts) => callOpenAICompat(conf, opts) },
     // 新 provider：
     mistral:  { call: (conf, opts) => callOpenAICompat(conf, opts) },
   };
   ```

2. **`config.json`** 的 `providers` 加对应字段：

   ```json
   "mistral": { "apiKey": "...", "baseURL": "https://api.mistral.ai/v1", "model": "mistral-large-latest" }
   ```

3. （可选）**`web/index.html`** 的 `PROVIDER_LABELS` 加显示名 —— 不加也能跑，下拉框会显示 provider key。

前端代码无需改动 —— 下拉框由 `/config` 的 `knownProviders` + `availableProviders` 动态生成。

---

## 验证

提交后做过的完整自验：

```bash
# 语法检查
node --check server/config.js
node --check server/providers.js
node --check server/llm-proxy.js
python3 -c "import ast; ast.parse(open('launcher/launcher.py').read())"

# 配置加载
node -e "console.log(require('./server/config').loadConfig())"

# 启动 + 端点
node server/llm-proxy.js &
curl http://127.0.0.1:3001/config

# 热重载（改 config.json 中 apiKey 占位 → 真值 → 再改回）
# /config 的 availableProviders 会同步变化

# Python 启动器（不弹窗模式）
python3 -c "
import sys, types; sys.modules['webview'] = types.ModuleType('webview')
import importlib.util
spec = importlib.util.spec_from_file_location('launcher','launcher/launcher.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.read_port(), m.start_proxy().pid)
"
```

---

## 常见问题

### 「开始游戏」按钮一直灰着

前端每 3 秒探测一次 `/config`。可能原因：

1. `node server/llm-proxy.js` 没启动 → 启动它。
2. 端口冲突 → 改 `config.json` 的 `port`；浏览器模式用 `web/index.html?port=4000` 访问；launcher 模式自动透传。
3. `config.json` 里所有 provider 都被识别为占位 → 至少填一个真 apiKey（不要含 `FILL` / `YOUR`）。

### Python 启动器没反应 / proxy 起不来

看工程根目录的 `launcher.log`，里面有 node 的真实输出。最常见原因：

- **`node` 不在 PATH** → `launcher.log` 会显示 `FileNotFoundError`。
- **端口被占用** → launcher 会先 `taskkill`（仅 Windows）；Linux/macOS 需手工处理。
- **`config.json` 不存在或 JSON 语法错** → launcher 启动前会预检并打到 stderr。

### LLM 调用 4xx / 5xx

打开 `prompts.log` 看具体响应：

- **Claude**：检查 `model` 是 API 实际 ID（如 `claude-sonnet-4-5`），并且 apiKey 来自 Anthropic Console。
- **DeepSeek / OpenAI**：`model` 不存在或没权限 → 改成你账号有权限的 model。

### 数据安全

- `server/llm-proxy.js` **只监听 127.0.0.1**，不暴露公网。
- `config.json` 含明文 apiKey，已被 `.gitignore` 排除时**仍要避免误 commit**。
- `prompts.log` 不含 apiKey，但包含完整 prompt 和响应，自行斟酌是否分享。

---

## License

ISC
