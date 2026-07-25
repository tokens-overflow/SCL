# Q-CC 🐧

QQ 2007 经典皮肤的 **Claude Code 图形壳**（Q-CC）——给你本机的 `claude` CLI 套一层怀旧的界面。
聊天、定时任务、技能管理、CLAUDE.md 编辑、我的好友（可设人设）、**真人聊天**、My Zone 动态、小游戏，
全部塞进一个像素级复古的窗口里。

引擎就是你本机**原封不动的 Claude Code**：同一个登录、同一份 `~/.claude` 配置、
同样的会话/权限/MCP/技能。界面里开的会话，终端里 `claude --resume <session_id>` 能无缝接管。

---

## 一、技术架构

**一句话：这是个"壳"，真正干活的是你电脑本机的 `claude` 命令。**

采用 backend / frontend 分层，依赖只向下，各模块单一职责（详见 `ARCHITECTURE.md`）：

```
┌──────────────────────────────────────────────────────────┐
│  frontend/  （QQ2007 皮肤，结构/样式/逻辑分离，无框架无依赖）      │
│   index.html   DOM 骨架，只引用外部 CSS/JS                     │
│   styles/main.css   全部样式                                  │
│   js/*.js      逻辑按功能拆成 10 个文件(core/chat/friends/tasks/ │
│                panels/views/shell/prefs/netchat/main)，按序引入   │
└──────────────────────────┬───────────────────────────────┘
                           │  HTTP + SSE（浏览器 ⇄ 本地服务）
┌──────────────────────────┴───────────────────────────────┐
│  backend/  （Python 标准库，零 pip 依赖）                       │
│   api.py          HTTP API / SSE / 静态资源 / 依赖装配          │
│   task_service.py 一个 claude 会话的生命周期、事件编号、订阅       │
│   scheduler.py    定时任务（interval / daily / once）          │
│   cli_adapter.py  构造 claude CLI 参数，管理 stdin/stdout/stderr │
│   stores.py       原子 JSON 持久化（好友/动态/资料/任务/能力快照）  │
│   netchat.py      真人聊天：GitHub 私有仓库当中转邮箱，后台轮询收发  │
│   app_native.py   无边框原生窗口启动器(入口)；server.py 兼容入口     │
└───────────┬──────────────────────────┬───────────────────┘
            │ HTTPS(标准库 urllib)      │  子进程（headless stream-json 模式）
┌───────────┴────────────┐             │
│ GitHub 私有仓库(中转)    │             │
│ chat/<收件人>/*.json    │             │
│ = 真人↔真人 的信箱       │             │
┌──────────────────────────┴───────────────────────────────┐
│  claude -p --input-format stream-json                      │
│         --output-format stream-json --verbose              │
│         [--model … --add-dir … --resume …]                 │
│  = 你本机已登录的 Claude Code 本体                            │
└──────────────────────────────────────────────────────────┘
```

**目录结构**

| 路径 | 作用 |
|------|------|
| `frontend/index.html` | 页面结构（只引用外部 CSS/JS） |
| `frontend/styles/main.css` | 全部样式 |
| `frontend/js/*.js` | 前端逻辑，按功能拆分成 10 个文件按序引入（详见 ARCHITECTURE.md） |
| `backend/api.py` | HTTP/SSE + 静态资源 + 依赖装配 |
| `backend/task_service.py` | 会话生命周期、事件编号、订阅、主动停止 |
| `backend/scheduler.py` | 定时任务计算/持久化/触发 |
| `backend/cli_adapter.py` | claude CLI 参数与 stdin/stdout/stderr |
| `backend/stores.py` | 原子 JSON 写入及各类本地数据仓库 |
| `backend/netchat.py` | 真人聊天：GitHub 中转信箱、后台轮询、SSE 扇出（未配置则完全待命） |
| `backend/app_native.py` | 无边框原生窗口启动器（pywebview）——**入口**，没装则自动退回浏览器 |
| `backend/server.py` | 兼容入口（`app_native.py` 从这里取 `Handler` 等符号） |
| `windows.bat` | 工程根目录唯一的启动入口（`python -m backend.app_native`） |
| `scripts/make_shortcut.ps1` | 生成带企鹅图标的桌面快捷方式 |
| `docs/` | 文档（README / ARCHITECTURE） |
| `frontend/assets/` | 图标资源（icon.png / icon.ico） |
| `config.json` | 用户配置：昵称、默认模型、权限、项目目录、默认好友 |
| `data/` | 运行时数据（**不入库**）：会话事件、任务、定时、好友、动态、资料、能力快照、真人聊天配置(`netchat.json`)与消息(`net_msgs/`) |
| `tests/` | 冒烟测试 + stub claude 替身 |

**关键设计**

- **薄壳**：不打包 Claude，不存任何账号/密钥，不需要 API key。靠你本机 `claude` 已登录的账号运行。
- **引擎可替换**：默认调 PATH 里的 `claude`，可用环境变量 `CLAUDE2007_CLAUDE_BIN` 指到别的二进制（测试用 stub 等）。
- **会话即进程**：一个"聊天信息"= 一个 claude 会话。事件落盘 `data/events/<id>.jsonl`，刷新页面即回放；进程退出后下条消息自动 `--resume` 续接。
- **附加目录**：聊天里「📎 附加」= 给这次会话额外授权目录，底层就是 `--add-dir`；「📁 浏览…」走 pywebview 的系统文件夹对话框（浏览器模式降级为手输路径）。
- **图片输入**：粘贴的图片转成 Anthropic 的 `image` content block 随消息写进 CLI 的 stdin（`--input-format stream-json`），和终端里贴图走同一条路；为此请求体上限放宽到 24MB。
- **原生窗口**：`app_native.py` 用 pywebview 做无边框窗口（Mac 用 WKWebView，Windows 用 WebView2），标题栏的最小化/最大化/关闭/拖动都是真窗口操作。
- **跨平台 & 傻瓜化**：换电脑不用改配置——项目目录默认兜底「当前目录」，缺失目录自动退回，没装 pywebview 自动用浏览器。
- **真人聊天（网络好友）**：跟 AI 聊天完全独立的一套。消息 = GitHub 私有仓库里的一个小文件 `chat/<收件人>/<时间戳>-<发件人>-<随机>.json`；
  收方轮询自己的目录、读到就展示并**删除该文件**（靠删除防重复+防仓库膨胀，本地 `seen_cids` 兜底去重）。
  零 pip 依赖（标准库 `urllib` 调 GitHub Contents API）。**只有 owner/repo/token/handle 配全了才起后台线程**，否则一个网络请求都不发。
- **切模型不丢会话**：`--model` 是启动子进程时定死的，所以切换时先杀掉当前进程，下条消息自动带 `--resume` 用新模型接上同一个会话。

**主要功能**：新建聊天（可粘贴图片、鼠标选工作目录）/ 我的好友（自定义头像+人设，点击开聊）/ **真人聊天（网络好友）** /
我的安排（定时任务）/ 我的skill（增删改 `~/.claude/skills`）/ CLAUDE.md 编辑 / 插件·能力面板 /
My Zone（我的+好友动态）/ 小游戏（贪吃蛇·2048·翻牌）/ 编辑我的资料 / 聊天顶栏随时切模型。

---

## 二、安装 & 使用手顺（Windows / Mac）

### 前提（两个平台通用）

1. **装好 Python 3**
   - Mac：一般自带；没有就装 [python.org](https://www.python.org/downloads/) 或 `brew install python`
   - Windows：装 [python.org](https://www.python.org/downloads/) 的安装包，**勾选 "Add Python to PATH"**
2. **装好并登录 Claude Code**
   - 终端/命令行敲 `claude` 能进入、能对话，就说明装好且登录了
   - 没装：见 [claude.com/claude-code](https://claude.com/claude-code)
3. 把 `Q-CC` 这个文件夹拷到目标电脑任意位置

> 只要满足以上，本软件就能直接用——它只是个壳，Claude Code 活着它就能跑。

---

### Mac

**安装（一次性，可选）**
```bash
pip3 install --user pywebview      # 想要"无边框窗口"效果就装；不装会自动用浏览器
```

**启动**（终端，从工程根目录按模块启动）
```bash
cd Q-CC
python3 -m backend.app_native    # 无边框原生窗口；没装 pywebview 自动用浏览器
```
> Mac 侧没有双击启动脚本（`windows.bat` 只管 Windows）。想双击启动，自己建个 `.command` 文件写上面两行、`chmod +x` 即可。

---

### Windows

**安装（一次性，可选）**
```bat
pip install --user pywebview
```
（Windows 10/11 一般自带 WebView2；老系统若无边框窗口打不开，装一下微软的 WebView2 Runtime）

**启动（任选其一）**
- **双击** `windows.bat`（工程根目录唯一的启动入口）
- 或命令行（从工程根目录，按模块启动）：
  ```bat
  cd Q-CC
  python -m backend.app_native
  ```

---

### 其它启动方式（两个平台通用）

```bash
python3 -m backend.server --app   # Chrome/Edge 的 app 模式打开（会带一条系统标题栏）
python3 -m backend.server         # 普通浏览器标签页，打开 http://localhost:8787
python3 -m backend.server 8899    # 换端口（默认 8787）
```

---

### 使用手顺

1. 启动后进入怀旧界面。第一次没配置也能用（默认项目=当前目录）。
2. **跟 AI 聊天**：右边「我的好友」点任意好友（如 Claude 小蓝），就用它的人设开一段对话；
   底部输入框继续聊（Ctrl+Enter 发送）。中间实时看流式输出、工具调用、耗时/花费。
3. **加好友**：顶部/左侧「➕ 添加好友」→ 填名字、看图选头像、写人设 → 出现在「我的好友」。
4. **发图片**：在输入框里直接 **⌘V / Ctrl+V 粘贴图片**（截图、复制的图片文件都行）。
   输入框上方会出现待发送缩略图，每张右上角 ✕ 可单独删；可以只发图不打字。
5. **选工作目录**：「📋 新建任务」弹窗里点 **📁 浏览…**，用系统对话框挑一个文件夹作为**这次会话的工作目录**
   （选中后项目下拉自动置灰，✕ 可撤销改回下拉）。
6. **附加目录**：聊天输入区「📎 附加」→ **📁 浏览…** 选目录，或手输/粘贴路径，
   让**当前这段会话**也能访问项目外的路径（底层 `--add-dir`，下条消息起生效）。
7. **定时任务**：「我的安排」→ 新建，按每天/每隔/一次性触发，到点自动开一段会话。
8. **管理技能**：「我的skill」→ 增/删/改 `~/.claude/skills` 里的 skill。
9. **切模型**：聊天顶栏右上角的下拉框，聊到一半也能换（见下方「配置」）。
10. **My Zone**：发自己的动态、看好友动态、点赞。**小游戏**：贪吃蛇 / 2048 / 翻牌记忆。
11. **改自己的资料**：点左下角自己的头像卡。
12. **真人聊天**：见下一节。

---

### 真人聊天（网络好友）

跟真人（另一台电脑上的另一个人）互发消息，中转靠一个 **GitHub 私有仓库**，不需要任何服务器。

**一次性设置（两边都要做）**

1. 建一个 **私有** GitHub 仓库当信箱，例如 `你的账号/qcc-chat`（两个人用**同一个**仓库）。
2. 生成一枚 **Fine-grained PAT**，权限只给这一个仓库的 **Contents: Read and write**。
3. 右栏「真人好友」旁边的 **⚙** → 填：
   - **Q-CC ID**：你的唯一身份，只能用 `字母/数字/_/-`，别人靠它加你
   - **仓库**：`owner` / `repo`
   - **PAT**：上一步生成的 token（**只存在本机 `data/netchat.json`，绝不外发、不打日志、不通过接口返回前端**）
   - 昵称 / 头像 / 个性签名随意
4. 保存后点「＋ 加好友」，填**对方的 Q-CC ID**，即可开聊。左栏「真人聊天」分组里能看到会话。

**它是怎么工作的**

- 发消息 = 往仓库写一个小文件 `chat/<对方ID>/<时间戳>-<我的ID>-<随机6位>.json`
- 收消息 = 后台线程轮询 `chat/<我的ID>/`，读到就推给界面并**删掉那个文件**
- 有人正在聊时轮询会自动加快（约 2 秒一次），闲下来自动放慢，省 API 配额

> ⚠️ **PAT 安全**：`data/netchat.json` 里存着你的 GitHub token。`data/` 已在 `.gitignore` 里、不会被提交，
> 但**千万别手动把它加进任何公开仓库**。PAT 请严格按上面说的只授权那一个中转仓库，别给全账号权限。

---

### 换电脑注意事项

- `config.json` 里的项目路径是原电脑的；新电脑上不存在的会显示「缺失」并自动退回「当前目录」。想指定就把 `config.json` 的 `projects` 改成新电脑真实存在的目录。
- 想把**好友/动态/资料/聊天记录**一起带走，连 `data/` 文件夹一起拷。
- **老会话的"继续对话"（--resume）换电脑接不上**——会话存在各电脑自己的 `~/.claude` 里。老记录能看（回放），接着聊会变成新会话。
- 技能（`~/.claude/skills`）、MCP 等是各电脑自己的环境；花费走那台电脑的 Claude Code 账号。
- **真人聊天**换电脑要重新填一次（ID/仓库/PAT），或者把 `data/netchat.json` 一起拷过去。

---

## 配置 `config.json`

```json
{
  "user_name": "我",
  "default_model": "claude-opus-4-8",
  "default_permission_mode": "acceptEdits",
  "models": [
    { "id": "claude-opus-4-8", "label": "Opus 4.8" },
    { "id": "claude-opus-5",   "label": "Opus 5" },
    { "id": "sonnet",          "label": "Sonnet（最新）" },
    { "id": "haiku",           "label": "Haiku（快·便宜）" }
  ],
  "projects": [
    { "name": "我的项目", "path": "~/code/my-app", "pinned": true },
    { "name": "当前目录", "path": ".", "pinned": false }
  ]
}
```

- **`models`（两种写法，可混用）**——这里配的东西会原样传给 `claude --model`：
  - **字符串**：CLI 别名，如 `"opus"` / `"sonnet"` / `"haiku"`，总是指向该档**最新**版本，下拉里就显示这个词
  - **对象** `{ "id": ..., "label": ... }`：`id` 是传给 CLI 的完整模型名（能钉死具体版本，如 `claude-opus-4-8`、`claude-opus-5`），`label` 是下拉里显示的名字
  - 想加别的版本，照着加一行即可，例如 `{ "id": "claude-opus-4-7", "label": "Opus 4.7" }`
- `default_model`：新会话默认用哪个（写 `id` 或别名）。
- **切换模型**：新建任务/定时任务/好友资料三个弹窗里能选；**聊天顶栏右上角的下拉框可以聊到一半随时换**——
  底层会重启子进程并 `--resume` 接上原会话，聊天记录不丢，切换后从**下一条消息**起生效。
- `permission_mode`：即 Claude Code 原生的 `--permission-mode`（`plan`/`default`/`acceptEdits`/`bypassPermissions`）。日常建议 `acceptEdits`。
- `path` 支持 `~`。不存在的目录会自动沉底并在使用时退回当前目录。

## 测试（不消耗额度）

回归检查（语法 + 冒烟测试，全程用 stub 假 CLI，不调用真 claude）：
```bash
python3 -m py_compile backend/*.py
for f in frontend/js/*.js; do node --check "$f"; done
python3 -m unittest discover -s tests
```
`tests/stub_claude.py` 是假的 claude：读 stdin 的 JSON 行、往 stdout 写 `init` / `assistant` / `result` 事件。
（首次手动联调若报 `Permission denied`，先 `chmod +x tests/stub_claude.py`。）
手动联调也可用它：`CLAUDE2007_CLAUDE_BIN=tests/stub_claude.py python3 -m backend.server`。
