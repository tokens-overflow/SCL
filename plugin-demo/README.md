# hello-toolkit · 可安装的 Claude Code 插件完整示例

这是一个**开箱即用、可直接安装**的 Claude Code 插件示例，演示了 Claude Code 插件系统的全部五个扩展点：

| 扩展点 | 本示例中的内容 | 文件位置 |
| --- | --- | --- |
| 🔧 斜杠命令 (commands) | `/hello`、`/changelog` | `plugins/hello-toolkit/commands/` |
| 🤖 子代理 (agents) | `code-explainer`（只读代码讲解） | `plugins/hello-toolkit/agents/` |
| 📚 技能 (skills) | `release-notes`（撰写发布说明） | `plugins/hello-toolkit/skills/` |
| 🪝 钩子 (hooks) | `SessionStart`、`PostToolUse` | `plugins/hello-toolkit/hooks/` |
| 🔌 MCP 服务器 | `demo-filesystem`（受限文件系统） | `plugins/hello-toolkit/.mcp.json` |

---

## 目录结构

```
plugin-demo/
├── README.md                         # 本说明
├── .claude-plugin/
│   └── marketplace.json              # 插件市场清单（用于安装）
└── plugins/
    └── hello-toolkit/                # 插件本体
        ├── .claude-plugin/
        │   └── plugin.json           # 插件清单（manifest）
        ├── commands/
        │   ├── hello.md              # /hello 命令
        │   └── changelog.md          # /changelog 命令
        ├── agents/
        │   └── code-explainer.md     # 子代理：代码讲解
        ├── skills/
        │   └── release-notes/
        │       └── SKILL.md          # 技能：发布说明
        ├── hooks/
        │   └── hooks.json            # 钩子配置
        ├── scripts/
        │   ├── session-start.sh      # SessionStart 钩子脚本
        │   └── post-edit.sh          # PostToolUse 钩子脚本
        ├── mcp-data/                 # MCP 沙盒目录
        │   └── README.txt
        └── .mcp.json                 # MCP 服务器配置
```

---

## 安装方法

### 方式一：从本地市场安装（推荐用于试玩）

在 Claude Code 中执行：

```
/plugin marketplace add /home/user/SCL/plugin-demo
/plugin install hello-toolkit@scl-demo-marketplace
```

如果你是从 GitHub 克隆下来的，把上面的路径换成你本地的 `plugin-demo` 目录即可。

### 方式二：直接从 GitHub 安装

```
/plugin marketplace add tokens-overflow/scl
/plugin install hello-toolkit@scl-demo-marketplace
```

> 说明：`/plugin marketplace add` 接受一个**包含 `.claude-plugin/marketplace.json` 的目录**或 git 仓库；如果仓库根目录没有该文件，请改用指向 `plugin-demo` 子目录的本地路径（方式一）。

### 方式三：用交互式菜单浏览

```
/plugin
```

在弹出的菜单里选择 marketplace → 选中 `hello-toolkit` → 安装即可。

安装完成后**重启 Claude Code**（或重新加载），插件即生效。

---

## 验证插件已生效

1. 启动一个新会话——`SessionStart` 钩子会注入一行「hello-toolkit 插件已激活」的上下文。
2. 运行 `/hello 小明`，应当收到一段友好的问候。
3. 运行 `/help`，应当能在命令列表里看到 `/hello` 和 `/changelog`。
4. 让 Claude「讲解某个文件的实现」，它应当会调度 `code-explainer` 子代理。

---

## 各扩展点详解

### 1. 斜杠命令 `commands/*.md`

每个 `.md` 文件就是一个命令，文件名即命令名。前置 frontmatter 支持：

- `description`：在 `/help` 中显示的说明。
- `argument-hint`：参数提示。
- `allowed-tools`：限定该命令可用的工具。

正文是发送给模型的提示词，支持：
- `$ARGUMENTS` / `$1` `$2` … 取用户传入的参数；
- `` !`命令` `` 在提示中内联执行 shell 命令并把输出嵌入（见 `changelog.md`）。

### 2. 子代理 `agents/*.md`

frontmatter 中的 `name`、`description`、`tools`、`model` 定义了一个专用子代理。`description` 写得越清楚，主代理越知道何时把任务委派给它。本示例的 `code-explainer` 只授予只读工具（`Read, Grep, Glob`），保证它绝不修改代码。

### 3. 技能 `skills/<name>/SKILL.md`

技能是模型按需加载的专业知识包。`description` 决定了它何时被触发。本示例的 `release-notes` 教模型如何把技术变更改写成面向用户的发布说明。

### 4. 钩子 `hooks/hooks.json`

钩子让你在生命周期事件上执行脚本：
- `SessionStart`：会话开始时注入上下文。
- `PostToolUse`（匹配 `Edit|Write|MultiEdit`）：每次写文件后记录日志到 `.edit-log`。

脚本中使用 `${CLAUDE_PLUGIN_ROOT}` 引用插件根目录，保证路径在任何安装位置都正确。

### 5. MCP 服务器 `.mcp.json`

插件可以自带 MCP 服务器。本示例声明了一个 `demo-filesystem` 服务器，以插件内的 `mcp-data/` 为根，向 Claude 暴露受限的文件读写工具。

---

## 卸载

```
/plugin uninstall hello-toolkit@scl-demo-marketplace
/plugin marketplace remove scl-demo-marketplace
```

---

## 把它当作你自己插件的脚手架

想做自己的插件？复制 `plugins/hello-toolkit/` 目录，然后：

1. 改掉 `.claude-plugin/plugin.json` 里的 `name`、`description`、`author`。
2. 删掉用不到的扩展点（五个都是可选的）。
3. 把你的命令、子代理、技能塞进对应目录。
4. 在 `marketplace.json` 的 `plugins` 数组里登记你的插件。

许可证：MIT。尽管拿去改。
