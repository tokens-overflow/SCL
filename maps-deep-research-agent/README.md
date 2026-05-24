# Maps Deep Research Agent

一个基于 **DeepSeek + Google Maps Platform** 的自动化地图深度研究智能体。

参考《Agent Study》第十四章（HelloAgents Deep Researcher）的"规划—执行—汇报"三段式架构，在以下方面做了重新设计与增强：

| 维度 | 第十四章版本 | 本项目 |
| --- | --- | --- |
| 子任务模型 | 扁平 TODO 列表 | **有向无环图 (DAG)**，支持依赖关系 |
| 检索后端 | 网页搜索 (Tavily / DuckDuckGo / Perplexity) | **Google Maps Platform**（Places / Directions / Geocoding / Distance Matrix） |
| LLM | 任意 OpenAI 兼容 endpoint (经 HelloAgents) | 直接使用 DeepSeek 官方 SDK（含原生 JSON 模式和流式响应） |
| 工具抽象 | 内置 `note` 工具 | **可插拔 Tool 接口**，每个 Maps API = 一个 Tool |
| SSE 事件 | 字典 + 字符串 type | **Pydantic 类型化事件模型** |
| 缓存 | 无 | **LRU + 磁盘双层缓存**（Maps 请求等价幂等） |
| 重试 | 无 | **指数退避**（LLM 与 Maps API 均含） |
| 成本追踪 | 无 | **Token / Maps 请求数 实时统计** |
| 前端展示 | 纯 Markdown | **Markdown 报告 + 交互式地图 + 时间线行程** |

## 使用场景

给定地点/区域/主题（如 "东京 3 日游"、"上海陆家嘴 5 公里内最佳粤式餐厅"、"硅谷创业公司聚集区"），智能体会：

1. 把主题拆解为若干互相依赖的研究子任务（DAG）；
2. 并行调用 Google Maps Platform 工具检索 / 收集证据；
3. 用 DeepSeek 对每个子任务做要点提炼；
4. 汇总成结构化 Markdown 报告 + 地图可视化 + 推荐行程。

## 目录结构

```
maps-deep-research-agent/
├── backend/        # FastAPI + DeepSeek + Google Maps
│   ├── pyproject.toml
│   ├── .env.example
│   └── src/
│       ├── main.py            # FastAPI 入口
│       ├── config.py          # Pydantic Settings
│       ├── models.py          # 状态 & 事件模型
│       ├── prompts.py         # Planner / Summarizer / Reporter Prompt
│       ├── agent.py           # Orchestrator
│       ├── llm/               # DeepSeek 客户端封装
│       ├── tools/             # 可插拔工具：Maps Places/Directions/...
│       └── services/          # planner / executor / reporter / itinerary
└── frontend/       # Vue 3 + TypeScript + Vite + Google Maps JS
    └── src/
        ├── App.vue
        ├── components/        # 表单 / 任务时间线 / 地图 / 报告 / 行程
        ├── stores/            # 简单的 reactive store
        ├── services/          # axios + SSE
        └── types/
```

## 快速开始

### 1. 准备 API Key

- **DeepSeek**: https://platform.deepseek.com
- **Google Maps Platform**: https://console.cloud.google.com  
  需要启用：Places API (New)、Directions API、Geocoding API、Distance Matrix API、Maps JavaScript API。

### 2. 启动后端

```bash
cd backend
cp .env.example .env   # 填入 DEEPSEEK_API_KEY / GOOGLE_MAPS_API_KEY
pip install -e .       # 或 uv sync
python -m src.main     # http://localhost:8000
```

### 3. 启动前端

```bash
cd frontend
cp .env.local.example .env.local   # 填入 VITE_GOOGLE_MAPS_JS_KEY
npm install
npm run dev            # http://localhost:5173
```

## 接口

| Method | Path | 说明 |
| --- | --- | --- |
| GET  | `/healthz` | 健康检查 |
| POST | `/research` | 同步执行，返回完整报告 |
| POST | `/research/stream` | SSE 流式输出（推荐） |
| GET  | `/usage` | 当前进程的 token / Maps 调用统计 |

请求体：

```json
{
  "topic": "东京涉谷 3 日游",
  "max_tasks": 5,
  "language": "zh"
}
```

## 设计文档

详见 `docs/ARCHITECTURE.md`（核心组件、DAG 调度算法、Prompt 设计原则、缓存策略）。
