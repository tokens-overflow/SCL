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
| `backend/netchat.py` | 真人聊天：GitHub 中转信箱收发、后台轮询线程、SSE 扇出（与 AI 会话完全独立） |
| `backend/api.py` | HTTP API、SSE、静态资源和依赖装配 |

入口与后端代码都在 `backend/` 下：`app_native.py`（原生窗口启动器）从 `backend/server.py`
（兼容入口，装配 `AppContext`/`Handler`/`QuietHTTPServer`）导入符号。工程根仅保留启动脚本
`windows.bat`，它以 `python -m backend.app_native` 按模块方式从根目录启动（保证 `backend` 包可导入）。

## 前端职责

前端与后端一样按“结构 / 样式 / 逻辑”分离，全部位于 `frontend/`：

| 文件 | 唯一职责 |
|---|---|
| `frontend/index.html` | 页面结构（DOM 骨架），只引用外部 CSS/JS |
| `frontend/styles/main.css` | 所有视觉样式（QQ2007 皮肤） |
| `frontend/js/*.js` | 前端逻辑按功能拆分成多个文件（见下表） |

逻辑不再是单一大文件，而是拆成 10 个按职责划分的经典脚本，**按固定顺序**在
`index.html` 末尾引入（顶层声明共享同一全局作用域，靠加载顺序保证依赖可见）：

| 加载序 | 文件 | 职责 |
|---|---|---|
| 1 | `js/core.js` | 全局状态 + `$`/`esc` 工具 + 轻量 Markdown |
| 2 | `js/chat.js` | 会话气泡渲染 + 流式事件处理 + 权限确认卡片 |
| 3 | `js/friends.js` | 配置/头像 + 好友列表/开聊(继续会话) + 我的资料 |
| 4 | `js/tasks.js` | 任务列表/SSE/发送 + 新建任务 + 全局点击总线 |
| 5 | `js/panels.js` | 斜杠命令补全 + 附加目录弹层 |
| 6 | `js/views.js` | 视图切换 + My Zone + 小游戏 + skill + 插件 + 定时任务 |
| 7 | `js/shell.js` | CLAUDE.md 编辑 + 按钮绑定 + 原生窗口 + 表情面板 |
| 8 | `js/prefs.js` | 字体/字号 + 权限模式 + 时钟 + 任务轮询 |
| 9 | `js/netchat.js` | 真人聊天：设置/加好友/会话列表/收发与 SSE |
| 10 | `js/main.js` | 启动初始化(加载配置/好友/任务)，必须最后加载 |

> 拆分是纯粹按原文件顺序切成连续区间，逻辑未改；调整加载顺序会破坏依赖，请勿改动。

后端 `/` 直接返回 `frontend/index.html`，`/frontend/*` 提供其静态资源。

## 兼容性边界

- 继续使用现有 `config.json` 和 `data/` 文件。
- 继续使用本机 `claude`、`~/.claude`、MCP、Skill 和 session。
- 继续支持 `CLAUDE2007_CLAUDE_BIN` 测试替身。
- HTTP API 路径保持不变。
- SSE 新增递增 `id`，旧客户端仍可只读取 `data:`。
- `config.json` 的 `models` 同时接受旧的字符串数组和新的 `{id,label}` 对象数组（前端 `modelOptions()` 归一化）。
- 真人聊天未配置(owner/repo/token/handle 不全)时后台线程不启动，对既有功能零影响。

## 回归验证

```bash
python3 -m py_compile backend/*.py
for f in frontend/js/*.js; do node --check "$f"; done
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```
