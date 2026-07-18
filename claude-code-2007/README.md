# Claude Code 2007 🐧

QQ 2007 经典皮肤的 **Claude Code 图形界面**——给原生 `claude` CLI 套一层怀旧的壳。

![风格参考：QQ 2007 / Windows XP Luna](https://img.shields.io/badge/style-QQ%202007-blue)

## 特点

- **真·套壳**:引擎就是你本机原封不动的 Claude Code CLI(headless
  `stream-json` 模式)。同一个登录账号、同一份 `~/.claude` 配置、CLAUDE.md、
  MCP、权限体系、会话存档全部不变。界面里开的任务,终端里
  `claude --resume <session_id>` 可以无缝接管。
- **零依赖**:后端只用 Python 3 标准库,前端是一个自包含的 HTML 文件。
  没有 npm,没有 pip install。
- 任务列表、流式输出、代码块复制、工具调用折叠展示、多轮追问、
  历史回放与断线续聊(`--resume`)。
- 右栏「Claude 小蓝」好友面板纯属情怀彩蛋。

## 使用

前提:本机已安装并登录 [Claude Code](https://claude.com/claude-code) CLI,且有 Python 3。

```bash
cd claude-code-2007
python3 server.py          # 默认端口 8787
# 打开 http://localhost:8787
```

1. 点「新建任务」:选项目目录、模型、权限模式,输入指令,确定。
2. 中栏实时看 Claude 流式输出、工具调用(点击可展开输出)、本轮耗时/花费。
3. 底部输入框继续追问(Ctrl+Enter 发送);任务进程退出后会自动
   `--resume` 续接同一会话。

## 配置

编辑 `config.json`:

```json
{
  "user_name": "Asta",
  "default_model": "sonnet",
  "default_permission_mode": "acceptEdits",
  "models": ["sonnet", "opus", "haiku"],
  "projects": [
    { "name": "scl", "path": "~/workspace/scl", "pinned": true }
  ]
}
```

- `projects[].path` 是任务的工作目录(支持 `~`);`pinned` 决定显示在「置顶」还是「项目」分组。
- 权限模式即 Claude Code 原生的 `--permission-mode`:
  `plan` / `default` / `acceptEdits` / `bypassPermissions`。
  v1 不做逐条交互式批准,建议日常用 `acceptEdits`。

## 架构

```
浏览器 index.html(QQ2007 皮肤)
   │  POST 建任务/发消息        SSE 事件流(回放 + 实时)
   ▼
server.py(Python 标准库 http.server)
   │  每任务 spawn 一个子进程,stdin/stdout 走 JSON 行
   ▼
claude -p --input-format stream-json --output-format stream-json \
       --include-partial-messages --verbose [--model … --permission-mode … --resume …]
```

事件落盘在 `data/events/<task_id>.jsonl`,任务索引在 `data/tasks.json`,
刷新页面即回放。`data/` 不入库。

## 测试

不想消耗额度时,可用 stub 假 CLI 验证整条链路:

```bash
CLAUDE2007_CLAUDE_BIN=/path/to/stub-claude python3 server.py
```

stub 只需读 stdin 的 JSON 行、往 stdout 写 init/stream_event/assistant/result
事件即可。

## Roadmap(v2)

- 已安排(定时任务)、拉取请求视图、插件/站点页
- 交互式逐条权限批准(control 协议)
- 上线抖动 + 「滴滴滴」消息提示音 🔔
