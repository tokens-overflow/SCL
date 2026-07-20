# Claude Code 2007 模块化架构

## 依赖方向

```text
frontend/*
    ↓ HTTP / SSE
backend/api.py
    ↓ application services
backend/task_service.py ← backend/scheduler.py
    ↓                    ↓
backend/cli_adapter.py   backend/stores.py
```

依赖只能向下：

- `cli_adapter.py` 不知道任务、HTTP、定时器和文件存储。
- `stores.py` 不启动 Claude，也不处理 HTTP。
- `task_service.py` 组合 CLI 与持久化，管理一个 Claude 会话的生命周期。
- `scheduler.py` 只通过 `TaskService` 创建任务。
- `api.py` 是 composition root，负责装配依赖和协议转换，不实现业务状态机。

## 后端职责

| 模块 | 唯一职责 |
|---|---|
| `backend/cli_adapter.py` | 构造 Claude CLI 参数，管理 stdin/stdout/stderr |
| `backend/task_service.py` | 任务状态、会话恢复、事件编号、订阅与主动停止 |
| `backend/scheduler.py` | interval/daily/once 的计算、持久化与触发 |
| `backend/stores.py` | 原子 JSON 写入及各类本地数据仓库 |
| `backend/api.py` | HTTP API、SSE、静态资源和依赖装配 |

根目录 `server.py` 仅为兼容入口，因此现有 `app_native.py` 可以继续导入 `Handler`、`QuietHTTPServer` 等符号。

## 前端职责

| 模块 | 唯一职责 |
|---|---|
| `frontend/api.js` | HTTP/SSE 客户端 |
| `frontend/chat.js` | Claude stream-json 事件到聊天视图的映射 |
| `frontend/tasks.js` | 页面级状态和任务、好友、Skill 等控制器 |
| `frontend/scheduler.js` | 定时任务表单与列表 |
| `frontend/styles/main.css` | 所有视觉样式 |

旧单文件界面保留在根目录 `index.html`，访问 `/legacy` 可回退。新功能应优先进入模块化前端；迁移完成前不要删除旧界面。

## 兼容性边界

- 继续使用现有 `config.json` 和 `data/` 文件。
- 继续使用本机 `claude`、`~/.claude`、MCP、Skill 和 session。
- 继续支持 `CLAUDE2007_CLAUDE_BIN` 测试替身。
- HTTP API 路径保持不变。
- SSE 新增递增 `id`，旧客户端仍可只读取 `data:`。

## 回归验证

```bash
python3 -m py_compile server.py backend/*.py
node --check frontend/api.js
node --check frontend/chat.js
node --check frontend/tasks.js
node --check frontend/scheduler.js
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```
