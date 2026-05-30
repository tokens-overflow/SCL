# 🗺️ Maps Deep Research Agent

> 基于 **LLM 编排 + Google Maps Platform** 的自动化地点深度调研系统
> 输入一句主题 → 自动拆解任务 DAG → 并发调用 Maps API → 汇总成 Markdown 报告 + 交互地图 + 多日行程

---

## 一、系统架构

### 1. 顶层数据流

```
┌──────────────────────────────────────────────────────────────────────┐
│                        前端 Vue 3 + Vite                              │
│                    http://localhost:5173                              │
│   表单 ─────► SSE 流 ─────► 任务时间线 │ 地图 │ 报告 │ 行程            │
└──────────────────────────────┬───────────────────────────────────────┘
                               │  /api  →  vite proxy
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  后端 FastAPI  http://localhost:8000                  │
│                                                                      │
│   POST /research(/stream)                                            │
│                ▼                                                     │
│   ┌────────────────────────────────────────────────────────────┐    │
│   │                  MapsDeepResearchAgent                     │    │
│   │  ┌──────────────────────────────────────────────────────┐  │    │
│   │  │  Pipeline                                            │  │    │
│   │  │  ① PlanStage  → ② ExecuteStage → ③ ComposeStage     │  │    │
│   │  └──────────────────────────────────────────────────────┘  │    │
│   └────────────────────────────────────────────────────────────┘    │
│                                                                      │
│   每个 Stage 用到的零件：                                              │
│       PlanStage    →  PlanTask (LLM)                                 │
│       ExecuteStage →  DAGScheduler + ToolRunner + SummarizeTask      │
│       ComposeStage →  ReportTask + ItineraryTask (并行)              │
└────────────────────────┬─────────────────────────────────────────────┘
                         │
        ┌────────────────┼────────────────────────┐
        ▼                ▼                        ▼
   Google Maps     LLM Provider             SQLite + LRU 缓存
   Platform       (Bedrock / Anthropic /     (./.cache/maps/)
   (4 个 API)      OpenAI / DeepSeek)
```

### 2. 后端分层（高度解耦）

```
backend/src/
│
├─ 🧩 core/                ── 通用骨架（与业务无关，可移植）
│   ├─ events.py           EventEmitter = Callable[[Event], None]
│   ├─ pipeline.py         Stage 协议 + Pipeline
│   └─ llm_task.py         LLMTaskBase + Text/Json/Streaming 三档
│
├─ ⚙️ execution/           ── DAG 调度 + 工具执行（与 LLM/事件无关）
│   ├─ scheduler.py        DAGScheduler[N: HasDependencies]
│   └─ tool_runner.py      prepare_args → execute → _to_evidence
│
├─ 🤖 llm_tasks/           ── 4 个 LLM 用例（prompt + parse 二合一）
│   ├─ plan_task.py        JsonLLMTask[PlanInput, list[TaskNode]]
│   ├─ summarize_task.py   StreamingLLMTask[SummarizeInput, str]
│   ├─ report_task.py      TextLLMTask[ReportInput, str]
│   └─ itinerary_task.py   JsonLLMTask[ItineraryInput, (days, map)]
│
├─ 🎬 stages/              ── 3 个 Pipeline Stage（唯一混合 LLM+tool+event）
│   ├─ plan_stage.py
│   ├─ execute_stage.py
│   └─ compose_stage.py
│
├─ 🔌 llm/                 ── 多 provider 适配
│   ├─ base.py             LLMClient Protocol + LLMUsage
│   ├─ loader.py
│   └─ adapters/           openai_compat / anthropic_adapter (Anthropic + Bedrock)
│
├─ 🌐 tools/               ── 工具与缓存
│   ├─ base.py             Tool / ToolRegistry / ToolResult[T]
│   ├─ cache.py            两级缓存（LRU + sqlite）
│   └─ maps/               places / directions / geocoding / distance_matrix
│       ├─ client.py       async HTTP（5 个端点共用 _get/_post 模板）
│       └─ results.py      4 个 typed Payload（Pydantic）
│
├─ agent.py                顶层 facade：装配 Pipeline，run / run_stream
├─ main.py                 FastAPI 入口
├─ config.py               config.yaml → Pydantic 嵌套模型
├─ models.py               域模型 + SSE 事件
└─ prompts.py              4 个 Prompt 模板（中文）
```

### 3. 解耦原则

| 层 | 知道什么 | 不知道什么 |
|---|---|---|
| `core/` | 什么都不知道 | 业务、地图、LLM、HTTP |
| `tools/` | HTTP、Maps API、缓存 | LLM、Pipeline、Stage |
| `llm/` | LLM 协议、provider | 业务、prompt、工具 |
| `llm_tasks/` | 单个 LLM 用例（prompt + parse） | Pipeline、其它任务、工具 |
| `execution/` | DAG 调度、工具注册表 | LLM、事件分发 |
| `stages/` | 把上面所有零件组合起来 | —— |
| `agent.py` | 装配 Stage 列表 | Stage 内部如何工作 |

新人阅读顺序：**`agent.py` → `core/pipeline.py` → `stages/*`**，需要细节再深入对应零件层。

### 4. 三段式编排时序

```
   ┌────────── PlanStage ──────────┐  ┌──────── ExecuteStage ────────┐  ┌─── ComposeStage ───┐
   │                               │  │                              │  │                    │
   │  PlanTask (chat_json)         │  │  for each task in DAG:       │  │  ReportTask        │
   │     ↓                         │  │    prepare_args()            │  │     ↕ (并行)        │
   │  validate + topo + 兜底       │  │    execute() → ToolResult    │  │  ItineraryTask     │
   │     ↓                         │  │    SummarizeTask.stream() ──►  │     ↓              │
   │  state.tasks = [...]          │  │    state.tasks[*].evidence   │  │  state.report_md   │
   │                               │  │                              │  │  state.itinerary   │
   │  emit(plan_ready)             │  │  emit(task_update × N)       │  │  state.map_overview│
   │                               │  │  emit(summary_chunk × M)     │  │  emit(report)      │
   │                               │  │                              │  │  emit(usage)       │
   └───────────────────────────────┘  └──────────────────────────────┘  └────────────────────┘
            ▲ 同时同一条 Pipeline 被 run() 和 run_stream() 共用，差别只在传给 emit 的实现 ▲
```

---

## 二、初次安装设定

### 1. 开通 Google Maps API

在 [console.cloud.google.com](https://console.cloud.google.com) 同一项目下：

1. 「**APIs & Services → Library**」启用 **5 个 API**：
   - Places API (New)
   - Directions API
   - Geocoding API
   - Distance Matrix API
   - Maps JavaScript API

2. 「**Credentials → Create credentials → API key**」创建 **2 把 key**：

   | 用途 | Application restrictions | API restrictions |
   |---|---|---|
   | 后端 Server key | IP addresses（服务器 IP） | 前 4 个 API |
   | 前端 Browser key | HTTP referrers（`http://localhost:5173/*`） | Maps JavaScript API |

3. 「**Billing**」绑定计费账户（Google 每月赠送 $200 免费额度）。

### 2. LLM Provider 凭证

`config.yaml` 内置 5 个 provider，启动时只看 `llm.active` 字段：

| Provider 别名 | type | 模型 | 凭证 |
|---|---|---|---|
| `bedrock`（默认） | bedrock | `jp.anthropic.claude-sonnet-4-6` | AWS access key + secret + region |
| `anthropic` | anthropic | `claude-sonnet-4-20250514` | `${ANTHROPIC_API_KEY}` |
| `openai` | openai | `gpt-4o` | `${OPENAI_API_KEY}` |
| `deepseek` / `ds-flash` | deepseek | `deepseek-v4-flash` | `${DEEPSEEK_API_KEY}` |
| `ds-pro` | deepseek | `deepseek-v4-pro` | `${DEEPSEEK_API_KEY}` |

### 3. 编辑配置文件

**`backend/config.yaml`**：

```yaml
llm:
  active: deepseek          # 切换 provider 改这一行
  providers:
    deepseek:
      type: deepseek
      model: deepseek-v4-flash
      api_key: sk-...        # 直填或写 ${DEEPSEEK_API_KEY}
      base_url: https://api.deepseek.com
    # ... 其它 provider

app:
  maps:
    api_key: AIzaSy...       # ★必填：后端 Server key
  # 其余字段（端口、并发、缓存、retry…）有默认值，按需调整
```

**`frontend/.env.local`**（拷贝 `.env.local.example`）：

```env
VITE_GOOGLE_MAPS_JS_KEY=AIzaSy...    # ★必填：前端 Browser key
VITE_BACKEND_BASE=/api
```

### 4. 安装依赖

项目根目录两选一（**需要 Python 3.10+、Node 18+**）：

- **双击 `setup.bat`**
- **`.\setup.ps1`**

脚本会：检查 Python/Node/npm → 确认 `config.yaml` 存在、若 `.env.local` 缺失则从 `.env.local.example` 拷贝 → `pip install -e .`（自动按 `config.yaml` 里 active provider 决定要不要追加 `[bedrock]` / `[anthropic]` extra）→ `npm install` → 跑一次 config 加载冒烟 → 提示哪个 key 还没填。

幂等：重复跑只增量补装，不会破坏现有 venv / node_modules。

---

## 三、启动方式

### 一键启动（推荐）

项目根目录两选一：

- **双击 `start.bat`** —— 资源管理器里直接双击
- **`.\start.ps1`** —— 已经在 PowerShell 里就这么跑

脚本会：先杀掉占用 `:8000` / `:5173` 的旧进程 → 在新窗口里起 backend → 等 `/healthz` 200 → 在新窗口里起 frontend → 等 dev server ready → 自动打开浏览器。

再跑一次会先关旧的再重启，无需手动收尾。停止：关掉两个弹窗即可。

### 手动启动（备选）

开 **两个终端**：

```powershell
# 终端 A：后端
cd backend
python -m src.main
# → INFO Uvicorn running on http://127.0.0.1:8000
```

```powershell
# 终端 B：前端
cd frontend
npm run dev
# → VITE v6.x ready, Local: http://localhost:5173
```

浏览器打开 **http://localhost:5173**，填入主题（如「东京涉谷 3 日游」）+ 位置锚点（如「Tokyo, Japan」）→ 点「开始研究」。约 1-2 分钟后任务时间线、地图、行程、Markdown 报告依次渲染完成。

### 健康检查 / 切换 provider

```powershell
curl http://localhost:8000/healthz
# {"status":"ok","provider":"deepseek","model":"deepseek-v4-flash","config_path":"...config.yaml"}
```

改 `backend/config.yaml` 任意字段后双击 `start.bat` 即重启生效。常用切换：

```yaml
llm:
  active: ds-pro          # 换 DeepSeek Pro
```

---

## 四、云端部署

### 1. 两套配置文件的分工

| 文件 | git 状态 | 用途 |
|---|---|---|
| `backend/config.yaml` | **gitignored**（含真实 key） | 本地开发 |
| `backend/config.example.yaml` | committed（全部 `${ENV_VAR}` 占位） | 模板：本地兜底、云端直接用 |

`${VAR}` 占位符支持两种语法：

```yaml
api_key: ${DEEPSEEK_API_KEY}              # 未设环境变量 → 空字符串
region:  ${AWS_REGION:-ap-northeast-1}    # 未设 → 用 ap-northeast-1（bash 风格）
```

### 2. 云端启动方式

启动时通过 `CONFIG_PATH` 指向模板，secret 由平台环境变量注入：

```bash
CONFIG_PATH=config.example.yaml python -m src.main
```

### 3. 必填环境变量清单

按你 `LLM_ACTIVE` 选用的 provider 设对应组：

| Provider | 必须设的环境变量 |
|---|---|
| `bedrock`   | `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`、（可选 `AWS_REGION`） |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `openai`    | `OPENAI_API_KEY` |
| `deepseek` / `ds-flash` / `ds-pro` | `DEEPSEEK_API_KEY` |

**所有 provider 都必须设**：

| 环境变量 | 用途 |
|---|---|
| `GOOGLE_MAPS_API_KEY` | 后端 Server key |
| `CORS_ORIGINS` | 前端真实域名（如 `https://app.example.com`） |

可选覆盖：`LLM_ACTIVE` / `HOST` / `PORT` / `LOG_LEVEL` / `CACHE_DIR` 等（见 `config.example.yaml`）。

### 4. 几个平台的部署要点

| 平台 | 关键配置 |
|---|---|
| **Fly.io** | `fly secrets set GOOGLE_MAPS_API_KEY=...` 注入 env；`fly volumes create` 挂到 `CACHE_DIR` 保证缓存不丢；`fly.toml` 设 `[env] CONFIG_PATH = "config.example.yaml"` |
| **Render** | Dashboard → Environment 加 env vars；Disks 挂载到 `/app/.cache` 并设 `CACHE_DIR=/app/.cache`；Start command 用 `python -m src.main` |
| **Cloud Run** | `gcloud run deploy --set-env-vars=...`；SQLite 缓存随容器实例销毁，建议把 `CACHE_DIR` 设到 `/tmp` 或换 Memorystore；CPU `--cpu=1` + `--timeout=600` 容纳长请求 |

### 5. 公网暴露的最低安全要求

* 加简单 token 鉴权：在 FastAPI middleware 校验 `X-API-Token` 头，否则陌生人调一次 `/research` 就是你 $0.1+ 账单
* 前端构建用的 `VITE_GOOGLE_MAPS_JS_KEY` 在 Cloud Console 限制 HTTP referrer 到你的真实域名
* 后端 Server key 在 Cloud Console 限制 IP 到部署平台的出口 IP
* CORS 白名单去掉 `localhost`，只留生产域名
