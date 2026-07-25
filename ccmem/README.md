# ccmem — Claude Code 跨会话记忆检索

让 Claude Code 在你**每次提交 prompt 时**,自动从历史会话中检索相关片段并注入上下文。
不需要写提示词、不需要在 CLAUDE.md 里加规则、不需要模型"想起来"去查——hook 是无条件触发的。

单文件实现,仅依赖 Python 标准库(≥3.8,需要 SQLite 带 FTS5,绝大多数发行版默认满足)。

## 原理

```
你敲下 prompt
   │
   ├─ UserPromptSubmit hook ──► ccmem.py recall
   │      读 stdin(prompt/cwd/session_id)
   │      → SQLite FTS5 关键词检索历史会话(~50ms)
   │      → 命中片段以 additionalContext 注入(包装成 system reminder,界面不显示)
   │
   ▼
Claude 处理(此时已"看到"历史片段,需要细节时可再调 show 取回全文)
   │
   ▼
Stop / SessionEnd hook ──► ccmem.py hook-index(async)
       增量索引刚产生的对话,供下一次检索
```

数据源是 Claude Code 自己落盘的 transcript:
`~/.claude/projects/<转义过的项目路径>/<session-uuid>.jsonl`。
索引按"轮次"分块(一条 user 消息 + 紧随其后的 assistant 文本),
存到 `~/.claude/ccmem/index.db`(SQLite,WAL)。

检索排序:`(-bm25) × 项目加权(同项目 ×1.6) × 时间衰减(1/(1+天数/45))`,
同一会话最多出 1 条,默认取 top 4。

**两级取回**:注入的只是节选(总预算约 2800 字符),每条片段带会话 id 和轮次号;
Claude(或你)需要细节时用 `show` 命令从原始 JSONL 重放未截断全文。

## 安装

```bash
./install.sh
```

它会依次:

1. 复制 `ccmem.py` 到 `~/.claude/ccmem/`
2. 跑 `inspect` —— **请务必看一眼输出**:transcript 的字段结构官方不保证稳定,
   确认打印出的 role 和正文解析正确再继续。对不上时只需修改 `ccmem.py` 里的
   `extract_role()` / `extract_text()` 两个函数,然后 `index --force` 重建。
3. 跑 `index` 建索引并打印 `stats`
4. 把三个 hook 合并进 `~/.claude/settings.json`:
   - 先备份成 `settings.json.bak.<时间戳>`;解析失败会中止,绝不覆盖
   - 只增不删,保留你原有的全部配置;重复运行不会重复注册(幂等)
   - 同时把 `show` 命令加进 `permissions.allow`,取回原文时不弹确认框

装完后**新开一个 Claude Code 会话**即生效。

## 常用命令

```bash
python3 ~/.claude/ccmem/ccmem.py inspect          # 确认 transcript 格式(先跑这个)
python3 ~/.claude/ccmem/ccmem.py index            # 增量索引;--force 全量重建
python3 ~/.claude/ccmem/ccmem.py search "对账"     # 命令行调试检索效果;--cwd 模拟项目加权
python3 ~/.claude/ccmem/ccmem.py show 3f2a9c1d    # 按会话 id 前缀(8 位够了)取回原文
python3 ~/.claude/ccmem/ccmem.py show 3f2a9c1d --around 5   # 只看第 5 轮前后各两轮
python3 ~/.claude/ccmem/ccmem.py show 3f2a9c1d --full --max-chars 50000
python3 ~/.claude/ccmem/ccmem.py stats            # 索引条目/会话/项目数、库大小
```

`recall` 和 `hook-index` 是 hook 入口(读 stdin),一般不需要手工调。

## 可调参数

都在 `ccmem.py` 顶部:

| 参数 | 默认 | 说明 |
|---|---|---|
| `TOP_K` | 4 | 每次注入的片段条数上限 |
| `MAX_INJECT_CHARS` | 2800 | 注入文本总预算(hook 输出上限 10000,留足余量) |
| `USER_CLIP` / `ASSISTANT_CLIP` | 400 / 900 | 索引时每轮 user/assistant 文本截断长度 |
| `DECAY_HALF_DAYS` | 45 | 时间衰减:`1/(1+age_days/45)`,调大则旧会话更耐衰减 |
| `PROJECT_BOOST` | 1.6 | 同项目(按 cwd 反推)加权 |
| `MIN_CHUNK_CHARS` | 40 | 整块少于该字符数丢弃 |

环境变量:`CLAUDE_CONFIG_DIR`(默认 `~/.claude`)、`CCMEM_DB`(默认 `~/.claude/ccmem/index.db`)。

## 设计取舍(为什么是这样)

- **Hook 而不是 MCP。** MCP 提供的是工具,模型得自己决定调不调,于是你要在
  CLAUDE.md 里写规则提醒它,而它经常想不起来。Hook 无条件触发,这正是需求。
- **关键词而不是向量。** `UserPromptSubmit` 阻塞用户输入,必须快:纯 SQLite
  全流程约 50ms;加载 embedding 模型要几秒,每次敲回车都付这个代价不划算。
  关键词召回对"我之前是不是聊过 X"够用——人通常记得住关键词,只是记不住结论。
- **自制 CJK 二元组分词,而不是 FTS5 的 trigram。** trigram 要求查询串 ≥3 字符,
  "对账""退款""重试"这类两字中文词永远召回不到。ccmem 把 CJK 切成二元组、
  拉丁词转小写,空格连接后存入 `unicode61` 的 FTS5 表;索引和查询共用同一个
  分词函数,保证召回一致。
- **注入文本写成事实陈述**("以下是此前会话记录中……的片段"),不写成
  "你必须参考以下内容"。带外指令口吻会触发 Claude 的 prompt-injection 防御,
  它会把这段文本原样念给用户,而不是当上下文用。
- **`recall` 任何异常都静默退出(exit 0)。** 这个 hook 阻塞输入,它挂掉不能连累
  你用 Claude Code:记忆失效的代价是没有记忆,不能是没法干活。
- **排除当前会话**,否则会把你刚说的话再喂回来;**跳过 ccmem 自己注入过的内容**,
  否则会自我污染,检索结果里全是上一轮注入的片段。
- **增量索引**:记录每个文件的 mtime/size/已处理行数,未变化直接跳过,
  `Stop` hook 每轮只处理新增行。
- **两级取回**:注入上限 10000 字符,而一场会话动辄几万字。节选负责
  "想起来有这回事",`show` 负责"翻出细节"(优先从原始 JSONL 重放未截断全文,
  transcript 已删才退回索引里的截断版)。

## 限制

- 纯关键词检索:换个说法(同义词、只有语义相关)可能召回不到。检索不到不代表没聊过。
- 索引里存的是**全部对话明文**。别把 `~/.claude/ccmem/` 同步到网盘、公共 dotfiles
  仓库等位置;多机不做同步,各机各建各的索引。
- 不索引工具调用及其结果(噪声大、无检索价值),只索引 user/assistant 的文本。
- transcript 字段结构不保证稳定,Claude Code 升级后若解析失败,重跑 `inspect`
  并按提示改两个解析函数即可。

## 卸载

1. 打开 `~/.claude/settings.json`,删掉 `hooks.UserPromptSubmit / Stop / SessionEnd`
   里 `args` 含 `ccmem` 的条目,以及 `permissions.allow` 里含 `ccmem` 的那行
   (或直接用安装时生成的 `settings.json.bak.<时间戳>` 恢复)。
2. `rm -rf ~/.claude/ccmem/`(脚本和索引都在这里,transcript 原文不受影响)。
