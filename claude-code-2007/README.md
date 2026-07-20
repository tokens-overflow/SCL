# Claude Code 2007 🐧

QQ 2007 经典皮肤的 **Claude Code 图形壳**——给你本机的 `claude` CLI 套一层怀旧的界面。
聊天、定时任务、技能管理、CLAUDE.md 编辑、我的好友（可设人设）、QQ空间动态、QQ小游戏，
全部塞进一个像素级复古的窗口里。

引擎就是你本机**原封不动的 Claude Code**：同一个登录、同一份 `~/.claude` 配置、
同样的会话/权限/MCP/技能。界面里开的会话，终端里 `claude --resume <session_id>` 能无缝接管。

---

## 一、技术架构

**一句话：这是个"壳"，真正干活的是你电脑本机的 `claude` 命令。**

```
┌─────────────────────────────────────────────┐
│  前端  index.html （单文件，QQ2007 皮肤）         │
│  纯 HTML/CSS/JS，无框架、无外部依赖               │
└───────────────┬─────────────────────────────┘
                │  HTTP + SSE（浏览器 ⇄ 本地服务）
┌───────────────┴─────────────────────────────┐
│  后端  server.py （Python 标准库，零 pip 依赖）    │
│  · 每个会话 spawn 一个 claude 子进程              │
│  · stdout 的 JSON 事件流经 SSE 转发给前端         │
│  · stdin 写 JSON 行实现多轮对话                   │
└───────────────┬─────────────────────────────┘
                │  子进程（headless stream-json 模式）
┌───────────────┴─────────────────────────────┐
│  claude -p --input-format stream-json         │
│         --output-format stream-json --verbose │
│         [--model … --add-dir … --resume …]    │
│  = 你本机已登录的 Claude Code 本体               │
└─────────────────────────────────────────────┘
```

**组成文件**

| 文件 | 作用 |
|------|------|
| `server.py` | HTTP 服务 + 会话管理 + 定时任务 + 技能/好友/动态/资料等所有 API。**只用 Python 标准库** |
| `app_native.py` | 无边框原生窗口启动器（用 pywebview）。没装 pywebview 会**自动退回浏览器**打开 |
| `index.html` | 全部前端（结构 + 样式 + 逻辑，单文件） |
| `config.json` | 用户配置：昵称、默认模型、权限、项目目录、默认好友 |
| `data/` | 运行时数据（不入库）：会话事件、任务、定时、好友、动态、资料、能力快照 |
| `启动.command` / `启动.bat` | Mac / Windows 双击即启动 |

**关键设计**

- **薄壳**：不打包 Claude，不存任何账号/密钥，不需要 API key。靠你本机 `claude` 已登录的账号运行。
- **引擎可替换**：默认调 PATH 里的 `claude`，可用环境变量 `CLAUDE2007_CLAUDE_BIN` 指到别的二进制（测试用 stub 等）。
- **会话即进程**：一个"聊天信息"= 一个 claude 会话。事件落盘 `data/events/<id>.jsonl`，刷新页面即回放；进程退出后下条消息自动 `--resume` 续接。
- **附加目录**：聊天里「📎 附加」= 给这次会话额外授权目录，底层就是 `--add-dir`。
- **原生窗口**：`app_native.py` 用 pywebview 做无边框窗口（Mac 用 WKWebView，Windows 用 WebView2），标题栏的最小化/最大化/关闭/拖动都是真窗口操作。
- **跨平台 & 傻瓜化**：换电脑不用改配置——项目目录默认兜底「当前目录」，缺失目录自动退回，没装 pywebview 自动用浏览器。

**主要功能**：新建聊天 / 我的好友（自定义头像+人设，点击开聊）/ 我的安排（定时任务）/
我的skill（增删改 `~/.claude/skills`）/ CLAUDE.md 编辑 / 插件·能力面板 /
QQ空间（我的+好友动态）/ QQ小游戏（贪吃蛇·2048·翻牌）/ 编辑我的资料。

---

## 二、安装 & 使用手顺（Windows / Mac）

### 前提（两个平台通用）

1. **装好 Python 3**
   - Mac：一般自带；没有就装 [python.org](https://www.python.org/downloads/) 或 `brew install python`
   - Windows：装 [python.org](https://www.python.org/downloads/) 的安装包，**勾选 "Add Python to PATH"**
2. **装好并登录 Claude Code**
   - 终端/命令行敲 `claude` 能进入、能对话，就说明装好且登录了
   - 没装：见 [claude.com/claude-code](https://claude.com/claude-code)
3. 把 `claude-code-2007` 这个文件夹拷到目标电脑任意位置

> 只要满足以上，本软件就能直接用——它只是个壳，Claude Code 活着它就能跑。

---

### Mac

**安装（一次性，可选）**
```bash
pip3 install --user pywebview      # 想要"无边框窗口"效果就装；不装会自动用浏览器
```

**启动（任选其一）**
- **双击** `启动.command`（第一次会提示"无法验证开发者"，右键 →「打开」一次即可）
- 或终端：
  ```bash
  cd claude-code-2007
  python3 app_native.py            # 无边框原生窗口；没装 pywebview 自动用浏览器
  ```

---

### Windows

**安装（一次性，可选）**
```bat
pip install --user pywebview
```
（Windows 10/11 一般自带 WebView2；老系统若无边框窗口打不开，装一下微软的 WebView2 Runtime）

**启动（任选其一）**
- **双击** `启动.bat`
- 或命令行：
  ```bat
  cd claude-code-2007
  python app_native.py
  ```

---

### 其它启动方式（两个平台通用）

```bash
python3 server.py --app     # Chrome/Edge 的 app 模式打开（会带一条系统标题栏）
python3 server.py           # 普通浏览器标签页，打开 http://localhost:8787
python3 server.py 8899      # 换端口（默认 8787）
```

---

### 使用手顺

1. 启动后进入怀旧界面。第一次没配置也能用（默认项目=当前目录）。
2. **跟 AI 聊天**：右边「我的好友」点任意好友（如 Claude 小蓝），就用它的人设开一段对话；
   底部输入框继续聊（Ctrl+Enter 发送）。中间实时看流式输出、工具调用、耗时/花费。
3. **加好友**：顶部/左侧「➕ 添加好友」→ 填名字、看图选头像、写人设 → 出现在「我的好友」。
4. **附加目录**：聊天输入区「📎 附加」→ 加目录，让 Claude 能访问项目外的路径。
5. **定时任务**：「我的安排」→ 新建，按每天/每隔/一次性触发，到点自动开一段会话。
6. **管理技能**：「我的skill」→ 增/删/改 `~/.claude/skills` 里的 skill。
7. **QQ空间**：发自己的动态、看好友动态、点赞。**QQ小游戏**：贪吃蛇 / 2048 / 翻牌记忆。
8. **改自己的资料**：点左下角自己的头像卡。

---

### 换电脑注意事项

- `config.json` 里的项目路径是原电脑的；新电脑上不存在的会显示「缺失」并自动退回「当前目录」。想指定就把 `config.json` 的 `projects` 改成新电脑真实存在的目录。
- 想把**好友/动态/资料/聊天记录**一起带走，连 `data/` 文件夹一起拷。
- **老会话的"继续对话"（--resume）换电脑接不上**——会话存在各电脑自己的 `~/.claude` 里。老记录能看（回放），接着聊会变成新会话。
- 技能（`~/.claude/skills`）、MCP 等是各电脑自己的环境；花费走那台电脑的 Claude Code 账号。

---

## 配置 `config.json`

```json
{
  "user_name": "我",
  "default_model": "opus",
  "default_permission_mode": "acceptEdits",
  "models": ["opus", "sonnet", "haiku"],
  "projects": [
    { "name": "我的项目", "path": "~/code/my-app", "pinned": true },
    { "name": "当前目录", "path": ".", "pinned": false }
  ]
}
```

- `permission_mode`：即 Claude Code 原生的 `--permission-mode`（`plan`/`default`/`acceptEdits`/`bypassPermissions`）。日常建议 `acceptEdits`。
- `path` 支持 `~`。不存在的目录会自动沉底并在使用时退回当前目录。

## 测试（不消耗额度）

用 stub 假 CLI 验证整条链路：
```bash
CLAUDE2007_CLAUDE_BIN=/path/to/stub-claude python3 server.py
```
stub 只需读 stdin 的 JSON 行、往 stdout 写 `init` / `assistant` / `result` 事件即可。
