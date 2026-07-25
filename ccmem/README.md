# ccmem — Claude Code 跨会话记忆检索

让 Claude Code 在你**每次提交 prompt 时**,自动从历史会话中检索相关片段并注入上下文。
不需要写提示词、不需要在 CLAUDE.md 里加规则、不需要模型"想起来"去查——hook 是无条件触发的。

单文件实现,仅依赖 Python 标准库(≥3.8,需要 SQLite 带 FTS5,绝大多数发行版默认满足)。

## 原理

```
你敲下 prompt
   │
   ├─ UserPromptSubmit hook ──► ccmem.py recall
   │      读 stdin(prompt/cwd/session_id/transcript_path)
   │      → SQLite FTS5 关键词检索历史会话
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
一轮里 assistant 文本过长时再切成多个片段(见下文"分片"),
存到 `~/.claude/ccmem/index.db`(SQLite,WAL)。

检索排序:`(-bm25) × 项目加权(同项目 ×1.6) × 时间衰减(1/(1+天数/45))`,
同一会话、同一内容各最多出 1 条,默认取 top 4。

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
   - 同时把 `show` 加进 `permissions.allow`,取回原文时不弹确认框

装完后**新开一个 Claude Code 会话**即生效。

## 常用命令

```bash
python3 ~/.claude/ccmem/ccmem.py inspect          # 确认 transcript 格式(先跑这个)
python3 ~/.claude/ccmem/ccmem.py index            # 增量索引;--force 全量重建
python3 ~/.claude/ccmem/ccmem.py search "对账"     # 命令行调试检索;--cwd 模拟项目加权
python3 ~/.claude/ccmem/ccmem.py show 3f2a9c1d    # 按会话 id 前缀(8 位够了)取回原文
python3 ~/.claude/ccmem/ccmem.py show 3f2a9c1d --around 5   # 只看第 5 轮前后各两轮
python3 ~/.claude/ccmem/ccmem.py show 3f2a9c1d --full --max-chars 50000
python3 ~/.claude/ccmem/ccmem.py stats            # 条目/会话/项目/词表数、库大小
python3 ~/.claude/ccmem/ccmem.py prune --older-than 180 --vacuum   # 删掉半年前的片段
python3 ~/.claude/ccmem/ccmem.py prune --session 3f2a9c1d          # 永久忘掉某场会话
```

`recall` 和 `hook-index` 是 hook 入口(读 stdin),一般不需要手工调。

## 可调参数

都在 `ccmem.py` 顶部:

| 参数 | 默认 | 说明 |
|---|---|---|
| `TOP_K` | 4 | 每次注入的片段条数上限 |
| `MAX_INJECT_CHARS` | 2800 | 注入文本总预算(hook 输出上限 10000,留足余量) |
| `PART_CHARS` | 1200 | 一轮里 assistant 文本按此长度切片,决定检索粒度 |
| `USER_CLIP` / `ASSISTANT_CLIP` | 400 / 900 | 展示文本长度(超长保留头尾) |
| `DECAY_HALF_DAYS` | 45 | 时间衰减 `1/(1+age_days/45)`,调大则旧会话更耐衰减 |
| `PROJECT_BOOST` | 1.6 | 同项目(按 cwd 反推)加权 |
| `QUERY_RARE_TOKENS` | 12 | 查询只用最稀有的 N 个词(见"性能") |
| `STOP_DF_RATIO` | 0.4 | 出现在超过该比例片段里的词按停用词丢弃 |
| `CANDIDATE_POOL` | 300 | 每路 FTS 召回后参与重排的候选数 |
| `MIN_CHUNK_CHARS` | 40 | 整块少于该字符数丢弃 |

环境变量:`CLAUDE_CONFIG_DIR`(默认 `~/.claude`)、`CCMEM_DB`(默认 `~/.claude/ccmem/index.db`)。

## 设计取舍(为什么是这样)

- **Hook 而不是 MCP。** MCP 提供的是工具,模型得自己决定调不调,于是你要在
  CLAUDE.md 里写规则提醒它,而它经常想不起来。Hook 无条件触发,这正是需求。
- **关键词而不是向量。** `UserPromptSubmit` 阻塞用户输入,必须快。纯 SQLite
  的查询是几十毫秒量级;加载 embedding 模型要几秒,每次敲回车都付这个代价
  不划算。关键词召回对"我之前是不是聊过 X"够用——人通常记得住关键词,
  只是记不住结论。
- **自制 CJK 二元组分词,而不是 FTS5 的 trigram。** trigram 要求查询串 ≥3 字符,
  "对账""退款""重试"这类两字中文词永远召回不到。ccmem 把 CJK 切成二元组、
  拉丁词转小写并做轻量复数归一(`webhooks` / `webhook` 落到同一个 token),
  空格连接后存入 `unicode61` 的 FTS5 表;索引和查询共用同一个分词函数。
  单字中文查询走前缀匹配(`"对"*`),否则命中不了二元组。
- **注入文本写成事实陈述**("以下是此前会话记录中……的片段"),不写成
  "你必须参考以下内容"。带外指令口吻会触发 Claude 的 prompt-injection 防御,
  它会把这段文本原样念给用户,而不是当上下文用。
- **自我污染标记用哨兵 `⟦ccmem⟧`,不用那句自然语言。** 早期版本拿注入头部的
  中文句子当标记做子串匹配,结果任何**讨论 ccmem 本身**的 prompt 都被判成
  噪声丢掉了(在真实 transcript 上复现过)。标记必须是正常对话里不会出现的串。
- **超长文本保留头尾,而不是只留开头。** agentic 会话里 assistant 的价值密度
  后重前轻:开头是"我先看一下仓库",结论在最后。只留开头会把最有用的部分丢掉
  (实测一次 1600 字符的回复,前 900 字符全是过程旁白)。
- **一轮切成多个片段。** 一次 agentic 回复会在工具调用之间产生很多段文本,
  整轮合成一个 chunk 会让粒度过粗:命中被稀释,含结论的最后一段无法独立命中。
- **FTS 索引未裁剪正文,展示用裁剪版。** 否则被截掉的中段永远检索不到。
- **查询只用最稀有的若干个词。** 把几十个 token 全 OR 起来会命中几乎整个库,
  bm25 得给每条命中打分,延迟随语料线性增长,而"这个""然后"对相关性毫无贡献。
  索引期维护一张 `token_df` 词频表,查询时挑低频词,既快又准。
- **同项目单独召回一路。** 否则全局 bm25 的 `LIMIT` 会在加权之前就把同项目
  候选挤掉,项目加权等于没生效。
- **`recall` 用只读连接。** 它原先走建表/迁移路径,那些都是写操作,和 async 的
  `Stop` 索引撞锁时要等 SQLite 默认的 5 秒——而这个 hook 阻塞用户输入。
  现在只读打开、锁等待上限 400ms,并给 `recall` 留了一条不 import argparse 的
  快速通道。
- **`recall` 任何异常都静默退出(exit 0)。** 记忆失效的代价是没有记忆,
  不能是没法干活。
- **排除当前会话**,并额外排除同一 transcript 文件、以及内容指纹与当前会话
  重复的片段——`--resume` 会把历史复制进新文件,单靠 session_id 挡不住,
  会把你刚说的话当"历史"喂回来。
- **增量索引**:记录每个文件的 mtime/size/已处理行数,未变化直接跳过。
  上一次若停在一轮中间(Stop 在工具调用之间触发),下次会把无主的 assistant
  文本并回上一轮,而不是造一个没有提问的孤立片段。
- **两级取回**:注入上限 10000 字符,而一场会话动辄几万字。节选负责
  "想起来有这回事",`show` 负责"翻出细节"(优先从原始 JSONL 重放未截断全文,
  transcript 已删才退回索引里的截断版)。

## 性能

在 21600 个片段(12 项目 × 20 会话 × 90 轮,长尾词表约 3.7 万词)的索引上实测:

| | |
|---|---|
| 单次 `recall` 端到端 | **约 78 ms** |
| 其中纯检索 | 约 27 ms |
| 其中 Python 进程启动 + import | 约 45 ms(与语料无关) |
| 20 次连续 `recall` | 1.55 s |
| 首次全量索引 | 8.3 s(一次性) |
| 增量索引(无变化) | 15 ms |
| 全量重建期间并发 `recall` | 45–112 ms,不被锁阻塞 |
| 库大小 | 约 22 MB |

几点值得知道的:

- **进程启动占了一半以上。** 语料再小也省不掉这 40 多毫秒,所以别指望
  "小库就飞快";反过来说,语料涨到几万片段也不会明显变慢。
- 查询延迟取决于**命中面**而非库大小。词表长尾正常的语料下,稀有词挑选把
  命中面压得很小;若你的对话高度重复(同一批词反复出现),命中面会接近全库,
  单次 `recall` 可能涨到 130ms 以上。用 `prune --older-than` 控制体积。
- 相对模型自身的响应延迟,这个量级基本感知不到。

## 限制

- 纯关键词检索:换个说法(同义词、只有语义相关)可能召回不到。检索不到不代表没聊过。
- 复数归一是个保守的近似,不是词干还原:`class` 与 `classes` 能对上,
  但不规则变化(`index`/`indices`)对不上。
- 索引里存的是**全部对话明文**。对几种一眼可辨的凭据(`sk-`、`ghp_`、`AKIA`、
  JWT、`password=` 之类)会做脱敏替换,但那只是减少无意扩散,**不是安全边界**。
  别把 `~/.claude/ccmem/` 同步到网盘、公共 dotfiles 仓库等位置;多机不做同步。
- 不索引工具调用及其结果(噪声大、无检索价值),只索引 user/assistant 的文本。
- 系统注入块靠"成对标签"识别并跳过,所以以完整 HTML 片段开头的提问
  (`<div>…</div> 这个怎么居中`)会被误判成噪声。只带开标签的(`<div> 怎么居中`)不受影响。
- `token_df` 词频在 `prune` / 文件重建后会偏高(只增不减),这只影响查询词的
  挑选偏好,`index --force` 会重算。
- transcript 字段结构不保证稳定,Claude Code 升级后若解析失败,重跑 `inspect`
  并按提示改两个解析函数即可。
- 索引格式变更时(`SCHEMA_VERSION`),下一次 `index` 会自动全量重建;
  `hook-index` 遇到过期格式会直接跳过,留给你手工跑一次 `index`。

## 卸载

1. 打开 `~/.claude/settings.json`,删掉 `hooks.UserPromptSubmit / Stop / SessionEnd`
   里 `args` 含 `ccmem` 的条目,以及 `permissions.allow` 里含 `ccmem` 的那行
   (或直接用安装时生成的 `settings.json.bak.<时间戳>` 恢复)。
2. `rm -rf ~/.claude/ccmem/`(脚本和索引都在这里,transcript 原文不受影响)。
