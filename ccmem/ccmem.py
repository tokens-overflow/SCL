#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ccmem — Claude Code 跨会话记忆检索。

通过 Claude Code hook 在每次用户提交 prompt 时(UserPromptSubmit),
自动从历史会话 transcript 中检索相关片段并注入上下文;
通过 Stop / SessionEnd hook 做增量索引。

仅依赖 Python 标准库。数据源:~/.claude/projects/**/*.jsonl
索引:SQLite + FTS5(自制 CJK 二元组分词),默认 ~/.claude/ccmem/index.db

用法见 README.md,或运行:python3 ccmem.py --help
"""

import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- 可调参数

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
PROJECTS_DIR = CLAUDE_DIR / "projects"
DB_PATH = Path(os.environ.get("CCMEM_DB") or (CLAUDE_DIR / "ccmem" / "index.db"))

SCHEMA_VERSION = 2         # 变更时 index 会自动全量重建

TOP_K = 4                  # recall 注入的片段条数上限
MAX_INJECT_CHARS = 2800    # 注入文本总预算(hook 输出上限 10000,留足余量)
PART_CHARS = 1200          # 一轮里 assistant 文本按此长度切成多个片段
USER_CLIP = 400            # 索引时 user 展示文本截断长度
ASSISTANT_CLIP = 900       # 索引时 assistant 展示文本截断长度
FTS_USER_CLIP = 2000       # 送进 FTS 的 user 文本上限(防止粘贴的长日志爆索引)
MIN_CHUNK_CHARS = 40       # 整块少于该字符数则丢弃
RECALL_USER_CLIP = 240     # 注入时每条片段的 user 部分再截断
RECALL_ASST_CLIP = 480     # 注入时每条片段的 assistant 部分再截断
DECAY_HALF_DAYS = 45       # 时间衰减:score *= 1 / (1 + age_days / 45)
PROJECT_BOOST = 1.6        # 同项目加权
MAX_QUERY_TOKENS = 64      # 查询取前 N 个去重 token
QUERY_RARE_TOKENS = 12     # 只用其中最稀有的 N 个构造 OR 查询(见 select_query_tokens)
STOP_DF_RATIO = 0.4        # 出现在超过这个比例的片段里的词按停用词丢弃
CANDIDATE_POOL = 300       # 每路 FTS 召回后参与重排的候选数
RECALL_BUSY_MS = 400       # recall 的 SQLite 锁等待上限(它阻塞用户输入,不能久等)

# 注入文本的哨兵标记。索引时凡包含此串的内容一律跳过,防止自我污染。
# 必须是正常对话里几乎不可能出现的字符串——早期版本用一句自然语言当标记,
# 结果任何讨论 ccmem 自身的 prompt 都被误判成噪声丢掉了。
INJECT_SENTINEL = "⟦ccmem⟧"

# ---------------------------------------------------------------- 分词
# 索引和查询必须共用这一个函数,否则召回全错。
# 规则:拉丁字母/数字连续段 → 小写并做轻量复数归一;
#      CJK 连续段 → 二元组(单字段落保留单字)。
# 产出的 token 用空格连接后存入 FTS5(unicode61),unicode61 把空格分隔的
# CJK 二元组当作独立 token,从而绕开 trigram 对 <3 字符查询的限制。

_CJK_RANGES = (
    (0x3040, 0x30FF),    # 日文假名
    (0x3400, 0x4DBF),    # CJK 扩展 A
    (0x4E00, 0x9FFF),    # CJK 基本区
    (0xAC00, 0xD7AF),    # 谚文
    (0xF900, 0xFAFF),    # CJK 兼容
    (0x20000, 0x2FA1F),  # CJK 扩展 B+
)


def _is_cjk(ch):
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def fold_latin(word):
    """轻量复数归一,让 webhooks / webhook、queries / query 落到同一个 token。

    只做对称的、保守的处理:因为索引和查询都过这个函数,即使某个词被归得
    不太漂亮(redis → redi),两侧一致就不会影响精确匹配。
    """
    w = word.lower()
    if len(w) >= 5 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) >= 5 and w.endswith("es") and not w.endswith("ses"):
        return w[:-2]
    if len(w) >= 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokenize(text):
    """把文本切成 token 列表:拉丁小写归一词 + CJK 二元组。"""
    tokens = []
    latin = []
    cjk = []

    def flush_latin():
        if latin:
            tokens.append(fold_latin("".join(latin)))
            del latin[:]

    def flush_cjk():
        if len(cjk) == 1:
            tokens.append(cjk[0])
        else:
            for i in range(len(cjk) - 1):
                tokens.append(cjk[i] + cjk[i + 1])
        del cjk[:]

    for ch in text:
        if _is_cjk(ch):
            flush_latin()
            cjk.append(ch)
        elif ch.isalnum():
            flush_cjk()
            latin.append(ch)
        else:
            flush_latin()
            flush_cjk()
    flush_latin()
    flush_cjk()
    return tokens


def build_match(tokens):
    """构造 FTS5 MATCH 表达式。

    单字 CJK 查询用前缀匹配:索引里存的是二元组("对账"),查询"对"
    只有靠 "对"* 才能命中以该字开头的二元组。
    """
    parts = []
    for t in tokens:
        if len(t) == 1 and _is_cjk(t):
            parts.append('"%s"*' % t)
        else:
            parts.append('"%s"' % t)
    return " OR ".join(parts)


def select_query_tokens(conn, tokens):
    """从 prompt 的 token 里挑最稀有的若干个来构造查询。

    把几十个 token 全部 OR 起来会命中几乎整个库:bm25 得给每条命中打分,
    延迟随语料线性增长,而"的""这个"这类高频词对相关性毫无贡献。
    用索引期维护的 token_df 表挑低频词,既快又准。
    查不到词频(空库、或单字 CJK 前缀查询)时退回用全部 token。
    """
    if not tokens:
        return []
    try:
        ph = ",".join("?" * len(tokens))
        df = {r["token"]: r["df"] for r in conn.execute(
            "SELECT token, df FROM token_df WHERE token IN (%s)" % ph, tokens)}
    except sqlite3.Error:
        df = {}
    known = [t for t in tokens if df.get(t, 0) > 0]
    if not known:
        return tokens[:MAX_QUERY_TOKENS]
    known.sort(key=lambda t: df[t])
    total = 0
    try:
        total = int(get_meta(conn, "n_chunks") or 0)
    except (TypeError, ValueError):
        total = 0
    if total:
        cutoff = max(50, int(total * STOP_DF_RATIO))
        pruned = [t for t in known if df[t] <= cutoff]
        if pruned:
            known = pruned
    return known[:QUERY_RARE_TOKENS]


def project_key(path):
    """模拟 Claude Code 的项目目录转义。

    对应 cli.js 里的 A.replace(/[^a-zA-Z0-9]/g,"-");目录名还会做 NFC 归一。
    """
    return re.sub(r"[^A-Za-z0-9]", "-", unicodedata.normalize("NFC", str(path)))


def project_keys_for_cwd(cwd):
    """返回 cwd 可能对应的项目目录名集合。

    Claude Code 用的是解析过符号链接的真实路径,而 hook 传进来的 cwd 可能是
    符号链接路径。两个都算上,否则同项目加权会静默失效(降级为不加权)。
    """
    if not cwd:
        return set()
    keys = {project_key(cwd)}
    try:
        keys.add(project_key(os.path.realpath(cwd)))
    except Exception:
        pass
    return keys


# ---------------------------------------------------------------- 脱敏
# 索引里存的是对话明文,而这些明文会被自动注入到未来的会话里。
# 对几种一眼可辨的凭据做替换,降低"密钥被翻出来重新贴一遍"的概率。
# 这不是安全边界,只是减少无意扩散;show 从原始 JSONL 重放时仍是原文。

_SECRET_RE = re.compile(
    r"(?i)\b(?:sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|AIza[A-Za-z0-9_\-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{6,})\b"
)
_ASSIGN_RE = re.compile(
    r"(?i)\b(pass(?:word|wd)|secret|token|api[_-]?key|access[_-]?key)"
    r"(\s*[:=]\s*)(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s,;\"']{4,})"
)
REDACTED = "[已脱敏]"


def redact(text):
    if not text:
        return text
    text = _SECRET_RE.sub(REDACTED, text)
    return _ASSIGN_RE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)


# ---------------------------------------------------------------- 文本裁剪


def clip_head_tail(text, limit):
    """超长文本保留头尾而不是只留开头。

    agentic 会话里 assistant 的价值密度是后重前轻:开头是"我先看一下仓库",
    结论在最后。只留开头等于把最有用的部分丢掉。
    """
    if len(text) <= limit:
        return text
    marker = "\n……(中略)……\n"
    room = limit - len(marker)
    if room <= 0:
        return text[:limit]
    head = room * 2 // 5
    tail = room - head
    return text[:head] + marker + text[-tail:]


def _hard_split(seg, limit):
    """把一段过长文本按行边界切成 <= limit 的小块。"""
    if len(seg) <= limit:
        return [seg]
    out, cur = [], ""
    for line in seg.splitlines(True):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        out.append(cur)
    return out


def split_parts(segments, limit=PART_CHARS):
    """把一轮里的多段 assistant 文本聚合成若干 <= limit 的片段。

    一次 agentic 回复会产生很多段文本(每次工具调用之间一段)。整轮合成
    一个 chunk 会让粒度过粗:检索命中被稀释,且截断后只剩过程叙述。
    切成片段后,含结论的最后一段是独立可命中的 chunk。
    """
    parts, cur = [], ""
    for seg in segments:
        for piece in _hard_split(seg, limit):
            if cur and len(cur) + 1 + len(piece) > limit:
                parts.append(cur)
                cur = piece
            else:
                cur = (cur + "\n" + piece) if cur else piece
    if cur:
        parts.append(cur)
    return parts


# ---------------------------------------------------------------- transcript 解析
# 字段结构不保证稳定。对不上时只需要改 extract_role / extract_text 这两个函数,
# 用 `inspect` 命令确认实际格式。


def extract_role(rec):
    """role 从 message.role 取,回退到顶层 type。"""
    msg = rec.get("message")
    if isinstance(msg, dict) and msg.get("role"):
        return msg["role"]
    return rec.get("type")


def extract_text(rec):
    """正文从 message.content 取;可能是字符串,也可能是 block 数组。
    只保留 type == "text" 的 block,丢弃 tool_use / tool_result / thinking。"""
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
    else:
        parts = []
    return "\n".join(p for p in parts if p).strip()


_TAG_OPEN_RE = re.compile(r"^<([a-zA-Z][\w:.-]*)(\s[^>]*)?>")


def is_noise(text):
    """过滤系统注入、命令回显、中断标记,以及 ccmem 自己注入过的内容。"""
    if not text:
        return True
    t = text.strip()
    if INJECT_SENTINEL in t:
        return True
    if t.startswith("[Request interrupted"):
        return True
    if t.startswith("Caveat:"):
        return True
    # 系统注入块的特征是成对标签(<system-reminder>…</system-reminder>)。
    # 只看开头的 '<' 会把"<div> 怎么居中"这类正常提问一起丢掉,所以要求闭合标签存在。
    m = _TAG_OPEN_RE.match(t)
    if m and ("</%s>" % m.group(1)) in t:
        return True
    return False


def parse_turns(path, start_line=0, start_turn=0):
    """把 JSONL 解析成"轮次"列表:一条 user 消息 + 紧随其后的 assistant 文本为一轮。

    返回 (turns, total_lines, next_turn_idx)。文本不截断,截断由调用方决定,
    这样 show 命令可以复用本函数取未截断全文,且轮次编号与索引一致。
    """
    turns = []
    cur = None
    turn_idx = start_turn
    total_lines = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            total_lines = i + 1
            if i < start_line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            role = extract_role(rec)
            text = extract_text(rec)
            if is_noise(text):
                continue
            ts = rec.get("timestamp") or ""
            if role == "user":
                if cur is not None:
                    turns.append(cur)
                cur = {"idx": turn_idx, "ts": ts, "user": text, "asst": []}
                turn_idx += 1
            elif role == "assistant":
                if cur is None:
                    cur = {"idx": turn_idx, "ts": ts, "user": "", "asst": []}
                    turn_idx += 1
                cur["asst"].append(text)
    if cur is not None:
        turns.append(cur)
    return turns, total_lines, turn_idx


# ---------------------------------------------------------------- 存储


def _create_schema(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS files("
        "path TEXT PRIMARY KEY, mtime REAL, size INTEGER, lines INTEGER, turns INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chunks("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, project TEXT,"
        "ts TEXT, user_text TEXT, asst_text TEXT, path TEXT, turn_idx INTEGER,"
        "part INTEGER, fp TEXT)"
    )
    # contentless FTS5 表默认不支持 DELETE。带 contentless_delete=1(SQLite>=3.43)
    # 才能按 rowid 删除;否则删 chunks 会留下孤儿 FTS 行,污染 bm25 统计。
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
            "body, content='', contentless_delete=1,"
            " tokenize=\"unicode61 remove_diacritics 2\")"
        )
    except sqlite3.OperationalError:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
            "body, content='', tokenize=\"unicode61 remove_diacritics 2\")"
        )
    # 词频表:查询时用来挑稀有词。只影响查询词的挑选,允许有偏差
    # (删除片段后 df 会偏高),index --force 会重建。
    conn.execute(
        "CREATE TABLE IF NOT EXISTS token_df(token TEXT PRIMARY KEY, df INTEGER)"
    )
    # prune --session 忘掉的会话记在这里,否则下一次 index 会把它重新索引回来
    conn.execute(
        "CREATE TABLE IF NOT EXISTS excluded(session_id TEXT PRIMARY KEY)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_sess ON chunks(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_fp ON chunks(fp)")


def _migrate(conn):
    """老库平滑升级:检查缺列并 ALTER TABLE 补上。"""
    want = {
        "files": {"mtime": "REAL", "size": "INTEGER", "lines": "INTEGER",
                  "turns": "INTEGER"},
        "chunks": {"session_id": "TEXT", "project": "TEXT", "ts": "TEXT",
                   "user_text": "TEXT", "asst_text": "TEXT", "path": "TEXT",
                   "turn_idx": "INTEGER", "part": "INTEGER", "fp": "TEXT"},
    }
    for table, cols in want.items():
        have = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if not have:
            continue
        for col, typ in cols.items():
            if col not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
    conn.commit()


def open_db():
    """读写连接:建表 + 迁移。索引路径用这个。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _create_schema(conn)
    _migrate(conn)
    conn.commit()
    return conn


def open_db_for_read():
    """只读连接:不建表、不迁移、不写 PRAGMA,锁等待很短。

    recall 走这条路。原先它也走 open_db(),里面的 CREATE TABLE / PRAGMA /
    ALTER 都是写操作,和 async 的 Stop 索引撞锁时要等 SQLite 默认的 5 秒
    busy timeout——而这个 hook 阻塞用户输入。
    """
    uri = "file:%s?mode=ro" % Path(DB_PATH).as_posix().replace("?", "%3f")
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=RECALL_BUSY_MS / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=%d" % RECALL_BUSY_MS)
        conn.execute("SELECT 1 FROM chunks LIMIT 1").fetchone()
        return conn
    except sqlite3.Error:
        # WAL 需要恢复时只读连接会失败,退回读写连接(仍不做迁移)
        conn = sqlite3.connect(str(DB_PATH), timeout=RECALL_BUSY_MS / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=%d" % RECALL_BUSY_MS)
        return conn


def get_meta(conn, key):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    except sqlite3.Error:
        return None


def set_meta(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, str(value))
    )
    conn.commit()


def fts_can_delete(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='chunks_fts'"
    ).fetchone()
    return bool(row and "contentless_delete" in (row["sql"] or ""))


def drop_all(conn):
    for t in ("files", "chunks", "chunks_fts", "token_df"):
        conn.execute("DROP TABLE IF EXISTS %s" % t)
    conn.execute("DELETE FROM meta WHERE key='schema_version'")
    conn.commit()


def delete_chunks(conn, where, params):
    """删除 chunks 及其 FTS 行。FTS 不支持删除时返回 False,由调用方全量重建。"""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM chunks WHERE " + where, params)]
    if not ids:
        return True
    if not fts_can_delete(conn):
        return False
    conn.executemany(
        "DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids]
    )
    conn.execute("DELETE FROM chunks WHERE " + where, params)
    conn.commit()
    return True


# ---------------------------------------------------------------- 索引


def _flush_df(conn, df_acc):
    if not df_acc:
        return
    conn.executemany(
        "INSERT INTO token_df(token, df) VALUES(?,?)"
        " ON CONFLICT(token) DO UPDATE SET df = df + excluded.df",
        list(df_acc.items()),
    )
    df_acc.clear()


def _insert_chunk(conn, session_id, project, ts, path, turn_idx, part,
                  user_full, asst_full, df_acc):
    user_disp = clip_head_tail(redact(user_full), USER_CLIP)
    asst_disp = clip_head_tail(redact(asst_full), ASSISTANT_CLIP)
    if len(user_disp) + len(asst_disp) < MIN_CHUNK_CHARS:
        return 0
    # FTS 索引的是未裁剪的正文(裁剪只影响展示),否则被截掉的中段永远检索不到
    import hashlib  # 只有索引路径需要,recall 路径省掉这次 import
    toks = tokenize(redact(user_full)[:FTS_USER_CLIP] + "\n" + redact(asst_full))
    body = " ".join(toks)
    for t in set(toks):
        df_acc[t] = df_acc.get(t, 0) + 1
    fp = hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]
    cur = conn.execute(
        "INSERT INTO chunks(session_id, project, ts, user_text, asst_text,"
        " path, turn_idx, part, fp) VALUES(?,?,?,?,?,?,?,?,?)",
        (session_id, project, ts, user_disp, asst_disp, path, turn_idx, part, fp),
    )
    conn.execute(
        "INSERT INTO chunks_fts(rowid, body) VALUES(?,?)", (cur.lastrowid, body)
    )
    return 1


def index_file(conn, path, df_acc=None):
    """增量索引单个 transcript 文件。返回 (新增数, 需要全量重建)。"""
    own_df = df_acc is None
    if own_df:
        df_acc = {}
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return 0, False
    row = conn.execute(
        "SELECT mtime, size, lines, turns FROM files WHERE path=?", (str(path),)
    ).fetchone()
    start_line, start_turn = 0, 0
    if row is not None:
        if row["mtime"] == st.st_mtime and row["size"] == st.st_size:
            return 0, False  # 未变化,直接跳过
        if st.st_size >= (row["size"] or 0):
            start_line = row["lines"] or 0
            start_turn = row["turns"] or 0
        else:
            # 文件变小(被改写/轮转):丢掉旧 chunk 全量重建该文件
            if not delete_chunks(conn, "path=?", (str(path),)):
                return 0, True

    turns, total_lines, next_turn = parse_turns(path, start_line, start_turn)

    # 上一次索引可能停在一轮中间(Stop hook 在工具调用之间触发),那么这次
    # 解析出的第一"轮"会没有 user 文本。把它并回上一轮,而不是造一个无主片段。
    part_offset = 0
    if turns and not turns[0]["user"] and start_turn > 0:
        prev = conn.execute(
            "SELECT user_text, MAX(part) AS mp FROM chunks"
            " WHERE path=? AND turn_idx=?",
            (str(path), start_turn - 1),
        ).fetchone()
        if prev and prev["user_text"] is not None:
            for t in turns:
                t["idx"] -= 1
            turns[0]["user"] = prev["user_text"]
            part_offset = (prev["mp"] or 0) + 1
            next_turn -= 1

    session_id = path.stem
    project = path.parent.name
    added = 0
    for ti, t in enumerate(turns):
        parts = split_parts(t["asst"]) or [""]
        offset = part_offset if ti == 0 else 0
        for pi, asst in enumerate(parts):
            added += _insert_chunk(
                conn, session_id, project, t["ts"], str(path), t["idx"],
                offset + pi, t["user"], asst, df_acc,
            )
    conn.execute(
        "INSERT OR REPLACE INTO files(path, mtime, size, lines, turns)"
        " VALUES(?,?,?,?,?)",
        (str(path), st.st_mtime, st.st_size, total_lines, next_turn),
    )
    if own_df:
        _flush_df(conn, df_acc)
    conn.commit()
    return added, False


def index_all(conn):
    added, need_rebuild = 0, False
    df_acc = {}
    excluded = {r["session_id"] for r in conn.execute("SELECT session_id FROM excluded")}
    if PROJECTS_DIR.is_dir():
        for p in sorted(PROJECTS_DIR.glob("*/*.jsonl")):
            if p.stem in excluded:
                continue
            n, rb = index_file(conn, p, df_acc)
            added += n
            need_rebuild = need_rebuild or rb
    _flush_df(conn, df_acc)   # 全量索引时攒到最后一次性写,避免上百万次 upsert
    set_meta(conn, "n_chunks",
             conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    conn.commit()
    return added, need_rebuild


# ---------------------------------------------------------------- 检索


def _age_days(ts, now):
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt.timestamp()) / 86400.0)
    except Exception:
        return 180.0  # 无时间戳的按半年前算


_SELECT = (
    "SELECT c.id, c.session_id, c.project, c.ts, c.user_text, c.asst_text,"
    " c.path, c.turn_idx, c.part, c.fp, bm25(chunks_fts) AS rank"
    " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
    " WHERE chunks_fts MATCH ?"
)


def search(conn, query_text, cwd=None, exclude_session=None, exclude_path=None,
           exclude_fps=None, top_k=TOP_K):
    """返回 [(score, row), ...]。
    最终得分 = (-bm25) × 项目加权 × 时间衰减;同一会话/同一内容各最多出 1 条。"""
    seen_tok, toks = set(), []
    for t in tokenize(query_text):
        if t not in seen_tok:
            seen_tok.add(t)
            toks.append(t)
        if len(toks) >= MAX_QUERY_TOKENS:
            break
    if not toks:
        return []
    match = build_match(select_query_tokens(conn, toks))
    projs = sorted(project_keys_for_cwd(cwd))

    rows = {}
    try:
        # 同项目单独召回一路。否则 OR 查询命中面很大时,全局 bm25 的 LIMIT
        # 会在加权之前就把同项目候选挤掉,项目加权等于没生效。
        if projs:
            ph = ",".join("?" * len(projs))
            for r in conn.execute(
                _SELECT + " AND c.project IN (%s) ORDER BY rank LIMIT ?" % ph,
                [match] + projs + [CANDIDATE_POOL],
            ):
                rows[r["id"]] = r
        for r in conn.execute(
            _SELECT + " ORDER BY rank LIMIT ?", (match, CANDIDATE_POOL)
        ):
            rows.setdefault(r["id"], r)
    except sqlite3.Error:
        return []

    now = time.time()
    projs_set = set(projs)
    scored = []
    for r in rows.values():
        if exclude_session and r["session_id"] == exclude_session:
            continue  # 不把当前会话喂回给自己
        if exclude_path and r["path"] == exclude_path:
            continue  # 同一 transcript 文件(会话被 resume 后换了 id)
        if exclude_fps and r["fp"] in exclude_fps:
            continue  # 内容与当前会话重复(resume 会复制历史到新文件)
        base = max(-r["rank"], 1e-4)
        boost = PROJECT_BOOST if r["project"] in projs_set else 1.0
        decay = 1.0 / (1.0 + _age_days(r["ts"] or "", now) / DECAY_HALF_DAYS)
        scored.append((base * boost * decay, r))
    scored.sort(key=lambda x: -x[0])

    out, seen_sess, seen_fp = [], set(), set()
    for s, r in scored:
        if r["session_id"] in seen_sess:
            continue
        if r["fp"] and r["fp"] in seen_fp:
            continue
        seen_sess.add(r["session_id"])
        if r["fp"]:
            seen_fp.add(r["fp"])
        out.append((s, r))
        if len(out) >= top_k:
            break
    return out


def own_fingerprints(conn, session_id):
    if not session_id:
        return set()
    try:
        return {
            r["fp"]
            for r in conn.execute(
                "SELECT fp FROM chunks WHERE session_id=?", (session_id,)
            )
            if r["fp"]
        }
    except sqlite3.Error:
        return set()


def _turn_label(r):
    label = "第 %d 轮" % r["turn_idx"]
    if r["part"]:
        label += " 片段 %d" % r["part"]
    return label


def build_injection(results):
    """把检索结果拼成注入文本。措辞必须是事实陈述,不能写成对模型的指令。"""
    script = os.path.abspath(__file__)
    header = (
        INJECT_SENTINEL
        + " 以下是此前会话记录中与当前提问相关的片段(由本地工具 ccmem 从历史"
        " transcript 索引中按关键词自动检索,内容为节选,可能与当前问题无关):\n"
    )
    first = results[0][1]
    footer = (
        "\n上面是节选。完整原文可以用 `python3 %s show <会话id> --around <轮次>` "
        "取回该轮前后的上下文,例如 `show %s --around %d`;`--full` 取回整场会话。"
        % (script, first["session_id"][:8], first["turn_idx"])
    )
    budget = MAX_INJECT_CHARS - len(header) - len(footer)
    body_parts, used, n = [], 0, 0
    for _score, r in results:
        date = (r["ts"] or "")[:10] or "日期未知"
        lines = [
            "\n[%d] 会话 %s · %s · 项目 %s · %s"
            % (n + 1, r["session_id"][:8], _turn_label(r), r["project"], date)
        ]
        if r["user_text"]:
            lines.append("用户: " + clip_head_tail(r["user_text"], RECALL_USER_CLIP))
        if r["asst_text"]:
            lines.append("助手: " + clip_head_tail(r["asst_text"], RECALL_ASST_CLIP))
        entry = "\n".join(lines) + "\n"
        if used + len(entry) > budget:
            break
        body_parts.append(entry)
        used += len(entry)
        n += 1
    if not body_parts:
        return ""
    return header + "".join(body_parts) + footer


# ---------------------------------------------------------------- 命令


def cmd_inspect(_args):
    files = sorted(
        PROJECTS_DIR.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    ) if PROJECTS_DIR.is_dir() else []
    if not files:
        print("未在 %s 下找到任何 .jsonl transcript。" % PROJECTS_DIR)
        print("请先用 Claude Code 聊几句再来,或检查 CLAUDE_CONFIG_DIR。")
        return 1
    f = files[0]
    print("最新 transcript: %s" % f)
    print("逐行打印顶层字段、解析出的 role 和正文前 200 字,请确认解析是否正确:\n")
    shown = 0
    with open(f, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if shown >= 8:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                print("-- 第 %d 行不是合法 JSON,跳过" % (shown + 1))
                continue
            role = extract_role(rec)
            text = extract_text(rec)
            print("-- 记录 %d" % (shown + 1))
            print("   顶层字段: %s" % ", ".join(sorted(rec.keys())))
            print("   解析 role: %r" % role)
            print("   解析正文: %r" % text[:200])
            print("   噪声过滤: %s" % ("会被跳过" if is_noise(text) else "保留"))
            shown += 1
    turns, _lines, _n = parse_turns(f)
    print("\n整份文件解析出 %d 轮,切成 %d 个片段。"
          % (len(turns), sum(len(split_parts(t["asst"])) or 1 for t in turns)))
    print("如果 role / 正文解析不对,只需修改 ccmem.py 里的"
          " extract_role() / extract_text() 两个函数。")
    return 0


def cmd_index(args):
    conn = open_db()
    force = args.force
    ver = get_meta(conn, "schema_version")
    if not force and ver != str(SCHEMA_VERSION):
        print("索引格式版本变化(%s → %d),自动全量重建。" % (ver or "无", SCHEMA_VERSION))
        force = True
    if force:
        drop_all(conn)
        conn.close()
        conn = open_db()
    t0 = time.time()
    added, need_rebuild = index_all(conn)
    if need_rebuild:
        # 本机 SQLite 的 FTS5 不支持 contentless 删除,只能整库重建
        print("有文件被改写且当前 SQLite 不支持 FTS 删除,改为全量重建……")
        drop_all(conn)
        conn.close()
        conn = open_db()
        added, _ = index_all(conn)
    set_meta(conn, "schema_version", SCHEMA_VERSION)
    print("新增 %d 个片段,耗时 %.0fms" % (added, (time.time() - t0) * 1000))
    return 0


def cmd_search(args):
    conn = open_db()
    results = search(conn, args.query, cwd=args.cwd, top_k=args.top)
    if not results:
        print("无命中")
        return 0
    for score, r in results:
        print("%.3f  会话 %s · %s · 项目 %s · %s"
              % (score, r["session_id"][:8], _turn_label(r), r["project"],
                 (r["ts"] or "")[:10]))
        if r["user_text"]:
            print("  用户: %s" % r["user_text"][:160].replace("\n", " "))
        if r["asst_text"]:
            print("  助手: %s" % r["asst_text"][:160].replace("\n", " "))
    return 0


def _resolve_session(conn, prefix):
    sids = [r["session_id"] for r in conn.execute(
        "SELECT DISTINCT session_id FROM chunks WHERE session_id LIKE ?"
        " ORDER BY session_id", (prefix + "%",))]
    if not sids:
        print("没有以 %r 开头的会话 id。用 stats/search 查看现有会话。" % prefix,
              file=sys.stderr)
        return None
    if len(sids) > 1:
        print("前缀 %r 不唯一,候选:" % prefix, file=sys.stderr)
        for s in sids:
            print("  " + s, file=sys.stderr)
        return None
    return sids[0]


def cmd_show(args):
    conn = open_db()
    sid = _resolve_session(conn, args.session)
    if sid is None:
        return 1
    row = conn.execute(
        "SELECT path FROM chunks WHERE session_id=? LIMIT 1", (sid,)
    ).fetchone()
    path = row["path"]

    if os.path.exists(path):
        turns, _lines, _next = parse_turns(path)  # 从原始 JSONL 重放,未截断
        source = "原始 transcript"
    else:
        turns, source = [], "索引缓存(原 transcript 已删除,文本是截断版)"
        for r in conn.execute(
            "SELECT * FROM chunks WHERE session_id=? ORDER BY turn_idx, part", (sid,)
        ):
            if turns and turns[-1]["idx"] == r["turn_idx"]:
                turns[-1]["asst"].append(r["asst_text"])
            else:
                turns.append({"idx": r["turn_idx"], "ts": r["ts"],
                              "user": r["user_text"], "asst": [r["asst_text"]]})

    if args.around is not None and not args.full:
        lo, hi = args.around - 2, args.around + 2
        turns = [t for t in turns if lo <= t["idx"] <= hi]

    out = ["会话 %s | 来源: %s | 共 %d 轮\n" % (sid, source, len(turns))]
    for t in turns:
        out.append("── 第 %d 轮 %s ──" % (t["idx"], (t["ts"] or "")[:19]))
        if t["user"]:
            out.append("用户: %s" % t["user"])
        asst = "\n".join(x for x in t["asst"] if x)
        if asst:
            out.append("助手: %s" % asst)
        out.append("")
    text = "\n".join(out)
    if len(text) > args.max_chars:
        text = text[: args.max_chars] + (
            "\n……(输出超过 %d 字符已截断。可用 --around <轮次号> 只看目标轮附近,"
            "或调大 --max-chars。)" % args.max_chars)
    print(text)
    return 0


def cmd_stats(_args):
    conn = open_db()
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_sess = conn.execute("SELECT COUNT(DISTINCT session_id) FROM chunks").fetchone()[0]
    n_proj = conn.execute("SELECT COUNT(DISTINCT project) FROM chunks").fetchone()[0]
    n_fts = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    print("索引库: %s" % DB_PATH)
    print("片段数: %d" % n_chunks)
    print("会话数: %d" % n_sess)
    print("项目数: %d" % n_proj)
    print("库大小: %.1f KB" % (size / 1024.0))
    n_tok = conn.execute("SELECT COUNT(*) FROM token_df").fetchone()[0]
    print("词表大小: %d" % n_tok)
    print("格式版本: %s | FTS 可删除: %s"
          % (get_meta(conn, "schema_version"), fts_can_delete(conn)))
    if n_fts != n_chunks:
        print("警告: FTS 行数 %d 与片段数 %d 不一致(有孤儿行),建议 index --force"
              % (n_fts, n_chunks))
    return 0


def cmd_prune(args):
    conn = open_db()
    if args.session:
        sid = _resolve_session(conn, args.session)
        if sid is None:
            return 1
        where, params = "session_id=?", (sid,)
        target = "会话 %s" % sid
        conn.execute(
            "INSERT OR REPLACE INTO excluded(session_id) VALUES(?)", (sid,)
        )
    else:
        now = time.time()
        ids = [r["id"] for r in conn.execute("SELECT id, ts FROM chunks")
               if _age_days(r["ts"] or "", now) > args.older_than]
        if not ids:
            print("没有超过 %d 天的片段。" % args.older_than)
            return 0
        where = "id IN (%s)" % ",".join("?" * len(ids))
        params = tuple(ids)
        target = "超过 %d 天的 %d 个片段" % (args.older_than, len(ids))
    n = conn.execute("SELECT COUNT(*) FROM chunks WHERE " + where, params).fetchone()[0]
    if not delete_chunks(conn, where, params):
        print("当前 SQLite 的 FTS5 不支持删除,请改用 index --force 重建。",
              file=sys.stderr)
        return 1
    # 被删过的文件要重新走一遍增量状态,否则不会再被索引
    conn.execute("DELETE FROM files WHERE path NOT IN (SELECT DISTINCT path FROM chunks)")
    conn.commit()
    if args.vacuum:
        conn.execute("VACUUM")
    print("已删除 %s(%d 个片段)。" % (target, n))
    return 0


def cmd_recall(_args):
    """UserPromptSubmit hook 入口。

    硬性约束:stdout 只允许出现那一个 JSON 对象(或无命中时什么都不输出);
    任何异常静默吞掉并 exit 0——这个 hook 阻塞用户输入,绝不能连累正常使用。
    """
    try:
        data = json.loads(sys.stdin.read())
        prompt = data.get("prompt") or ""
        if not prompt.strip():
            return 0
        conn = open_db_for_read()
        sid = data.get("session_id")
        results = search(
            conn,
            prompt,
            cwd=data.get("cwd"),
            exclude_session=sid,
            exclude_path=data.get("transcript_path"),
            exclude_fps=own_fingerprints(conn, sid),
        )
        if not results:
            return 0
        text = build_injection(results)
        if not text:
            return 0
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": text,
            },
            "suppressOutput": True,
        }, ensure_ascii=False))
    except Exception:
        pass  # 静默:记忆失效的代价是没有记忆,不能是没法干活
    return 0


def cmd_hook_index(_args):
    """Stop / SessionEnd hook 入口:增量索引,优先只处理当前 transcript。"""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        conn = open_db()
        if get_meta(conn, "schema_version") != str(SCHEMA_VERSION):
            return 0  # 格式过期,交给用户手工跑 index(hook 里不做重活)
        tp = data.get("transcript_path")
        if tp and os.path.exists(tp) and not conn.execute(
                "SELECT 1 FROM excluded WHERE session_id=?",
                (Path(tp).stem,)).fetchone():
            index_file(conn, tp)
        else:
            index_all(conn)
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------- main


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    # recall 阻塞用户输入,给它一条不碰 argparse 的快速通道
    if argv == ["recall"]:
        return cmd_recall(None)

    import argparse
    ap = argparse.ArgumentParser(
        prog="ccmem", description="Claude Code 跨会话记忆检索(hook 版)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("inspect", help="打印最新 transcript 的字段结构,先跑这个确认格式")

    p = sub.add_parser("index", help="增量索引全部 transcript")
    p.add_argument("--force", action="store_true", help="丢弃旧索引全量重建")

    p = sub.add_parser("search", help="命令行调试检索效果")
    p.add_argument("query")
    p.add_argument("--cwd", default=None, help="模拟 hook 传入的 cwd,用于项目加权")
    p.add_argument("--top", type=int, default=TOP_K)

    p = sub.add_parser("show", help="取回某场会话的原文")
    p.add_argument("session", help="会话 id 前缀(8 位即可)")
    p.add_argument("--around", type=int, default=None, help="只看该轮前后各两轮")
    p.add_argument("--full", action="store_true", help="整场会话(覆盖 --around)")
    p.add_argument("--max-chars", type=int, default=20000)

    sub.add_parser("stats", help="索引条目/会话/项目数、库大小")

    p = sub.add_parser("prune", help="删除旧片段或某场会话,控制索引体积")
    p.add_argument("--older-than", type=int, default=365, help="删除超过 N 天的片段")
    p.add_argument("--session", default=None,
                   help="忘掉这个会话(id 前缀);之后的 index 不会再索引它")
    p.add_argument("--vacuum", action="store_true", help="删完后 VACUUM 回收空间")

    sub.add_parser("recall", help="UserPromptSubmit hook 入口(读 stdin)")
    sub.add_parser("hook-index", help="Stop/SessionEnd hook 入口(读 stdin)")

    args = ap.parse_args(argv)
    return {
        "inspect": cmd_inspect, "index": cmd_index, "search": cmd_search,
        "show": cmd_show, "stats": cmd_stats, "prune": cmd_prune,
        "recall": cmd_recall, "hook-index": cmd_hook_index,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
