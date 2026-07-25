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

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- 可调参数

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
PROJECTS_DIR = CLAUDE_DIR / "projects"
DB_PATH = Path(os.environ.get("CCMEM_DB") or (CLAUDE_DIR / "ccmem" / "index.db"))

TOP_K = 4                  # recall 注入的片段条数上限
MAX_INJECT_CHARS = 2800    # 注入文本总预算(hook 输出上限 10000,留足余量)
USER_CLIP = 400            # 索引时 user 文本截断长度
ASSISTANT_CLIP = 900       # 索引时 assistant 文本截断长度
MIN_CHUNK_CHARS = 40       # 整块少于该字符数则丢弃
RECALL_USER_CLIP = 240     # 注入时每条片段的 user 部分再截断
RECALL_ASST_CLIP = 480     # 注入时每条片段的 assistant 部分再截断
DECAY_HALF_DAYS = 45       # 时间衰减:score *= 1 / (1 + age_days / 45)
PROJECT_BOOST = 1.6        # 同项目加权
MAX_QUERY_TOKENS = 80      # 查询取前 N 个去重 token
CANDIDATE_POOL = 200       # FTS 召回后参与重排的候选数

# 注入文本的头部标记。索引时凡包含此标记的内容一律跳过,防止自我污染。
INJECT_MARKER = "以下是此前会话记录中与当前提问相关的片段"

# ---------------------------------------------------------------- 分词
# 索引和查询必须共用这一个函数,否则召回全错。
# 规则:拉丁字母/数字连续段 → 小写整词;CJK 连续段 → 二元组(单字段落保留单字)。
# 产出的 token 之间用空格连接后存入 FTS5(unicode61),unicode61 会把
# 空格分隔的 CJK 二元组当作独立 token,从而绕开 trigram 对 <3 字符查询的限制。

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


def tokenize(text):
    """把文本切成 token 列表:拉丁小写整词 + CJK 二元组。"""
    tokens = []
    latin = []
    cjk = []

    def flush_latin():
        if latin:
            tokens.append("".join(latin).lower())
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


def project_key(path):
    """模拟 Claude Code 的项目目录转义:非字母数字一律换成 '-'。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


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


def is_noise(text):
    """过滤系统注入、命令回显、中断标记,以及 ccmem 自己注入过的内容。"""
    if not text:
        return True
    t = text.lstrip()
    if t.startswith("<"):
        return True
    if t.startswith("[Request interrupted"):
        return True
    if t.startswith("Caveat:"):
        return True
    if INJECT_MARKER in t:
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


def open_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS files("
        "path TEXT PRIMARY KEY, mtime REAL, size INTEGER, lines INTEGER, turns INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chunks("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, project TEXT,"
        "ts TEXT, user_text TEXT, asst_text TEXT, path TEXT, turn_idx INTEGER)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
        "body, content='', tokenize=\"unicode61 remove_diacritics 2\")"
    )
    _migrate(conn)
    return conn


def _migrate(conn):
    """老库平滑升级:检查缺列并 ALTER TABLE 补上。"""
    want = {
        "files": {"mtime": "REAL", "size": "INTEGER", "lines": "INTEGER",
                  "turns": "INTEGER"},
        "chunks": {"session_id": "TEXT", "project": "TEXT", "ts": "TEXT",
                   "user_text": "TEXT", "asst_text": "TEXT", "path": "TEXT",
                   "turn_idx": "INTEGER"},
    }
    for table, cols in want.items():
        have = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        for col, typ in cols.items():
            if col not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
    conn.commit()


def _drop_all(conn):
    conn.execute("DROP TABLE IF EXISTS files")
    conn.execute("DROP TABLE IF EXISTS chunks")
    conn.execute("DROP TABLE IF EXISTS chunks_fts")
    conn.commit()


# ---------------------------------------------------------------- 索引


def index_file(conn, path):
    """增量索引单个 transcript 文件,返回新增 chunk 数。"""
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return 0
    row = conn.execute(
        "SELECT mtime, size, lines, turns FROM files WHERE path=?", (str(path),)
    ).fetchone()
    start_line, start_turn = 0, 0
    if row is not None:
        if row["mtime"] == st.st_mtime and row["size"] == st.st_size:
            return 0  # 未变化,直接跳过
        if st.st_size >= (row["size"] or 0):
            start_line = row["lines"] or 0
            start_turn = row["turns"] or 0
        else:
            # 文件变小(极少见):丢掉旧 chunk 全量重建该文件
            conn.execute("DELETE FROM chunks WHERE path=?", (str(path),))

    turns, total_lines, next_turn = parse_turns(path, start_line, start_turn)
    session_id = path.stem
    project = path.parent.name
    added = 0
    for t in turns:
        user = t["user"][:USER_CLIP]
        asst = "\n".join(t["asst"])[:ASSISTANT_CLIP]
        if len(user) + len(asst) < MIN_CHUNK_CHARS:
            continue
        cur = conn.execute(
            "INSERT INTO chunks(session_id, project, ts, user_text, asst_text,"
            " path, turn_idx) VALUES(?,?,?,?,?,?,?)",
            (session_id, project, t["ts"], user, asst, str(path), t["idx"]),
        )
        body = " ".join(tokenize(user + "\n" + asst))
        conn.execute(
            "INSERT INTO chunks_fts(rowid, body) VALUES(?,?)", (cur.lastrowid, body)
        )
        added += 1
    conn.execute(
        "INSERT OR REPLACE INTO files(path, mtime, size, lines, turns)"
        " VALUES(?,?,?,?,?)",
        (str(path), st.st_mtime, st.st_size, total_lines, next_turn),
    )
    conn.commit()
    return added


def index_all(conn):
    added = 0
    if PROJECTS_DIR.is_dir():
        for p in sorted(PROJECTS_DIR.glob("*/*.jsonl")):
            added += index_file(conn, p)
    return added


# ---------------------------------------------------------------- 检索


def _age_days(ts, now):
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt.timestamp()) / 86400.0)
    except Exception:
        return 180.0  # 无时间戳的按半年前算


def search(conn, query_text, cwd=None, exclude_session=None, top_k=TOP_K):
    """返回 [(score, row), ...]。
    最终得分 = (-bm25) × 项目加权 × 时间衰减;同一会话最多出 1 条。"""
    seen_tok = set()
    toks = []
    for t in tokenize(query_text):
        if t not in seen_tok:
            seen_tok.add(t)
            toks.append(t)
        if len(toks) >= MAX_QUERY_TOKENS:
            break
    if not toks:
        return []
    match = " OR ".join('"%s"' % t for t in toks)
    try:
        rows = conn.execute(
            "SELECT c.id, c.session_id, c.project, c.ts, c.user_text, c.asst_text,"
            " c.path, c.turn_idx, bm25(chunks_fts) AS rank"
            " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
            " WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, CANDIDATE_POOL),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    proj = project_key(cwd) if cwd else None
    now = time.time()
    scored = []
    for r in rows:
        if exclude_session and r["session_id"] == exclude_session:
            continue  # 不把当前会话喂回给自己
        base = max(-r["rank"], 1e-4)
        boost = PROJECT_BOOST if (proj and r["project"] == proj) else 1.0
        decay = 1.0 / (1.0 + _age_days(r["ts"] or "", now) / DECAY_HALF_DAYS)
        scored.append((base * boost * decay, r))
    scored.sort(key=lambda x: -x[0])
    out, seen_sess = [], set()
    for s, r in scored:
        if r["session_id"] in seen_sess:
            continue
        seen_sess.add(r["session_id"])
        out.append((s, r))
        if len(out) >= top_k:
            break
    return out


def build_injection(results):
    """把检索结果拼成注入文本。措辞必须是事实陈述,不能写成对模型的指令。"""
    script = os.path.abspath(__file__)
    header = (
        INJECT_MARKER
        + "(由本地工具 ccmem 从历史 transcript 索引中按关键词自动检索,"
        "内容为节选,可能与当前问题无关):\n"
    )
    first = results[0][1]
    footer = (
        "\n上面是节选。完整原文可以用 `python3 %s show %s --around %d` "
        "取回该轮前后的上下文,`--full` 取回整场会话。"
        % (script, first["session_id"][:8], first["turn_idx"])
    )
    budget = MAX_INJECT_CHARS - len(header) - len(footer)
    body_parts = []
    used = 0
    n = 0
    for _score, r in results:
        date = (r["ts"] or "")[:10] or "日期未知"
        entry_lines = [
            "\n[%d] 会话 %s · 第 %d 轮 · 项目 %s · %s"
            % (n + 1, r["session_id"][:8], r["turn_idx"], r["project"], date)
        ]
        if r["user_text"]:
            entry_lines.append("用户: " + r["user_text"][:RECALL_USER_CLIP])
        if r["asst_text"]:
            entry_lines.append("助手: " + r["asst_text"][:RECALL_ASST_CLIP])
        entry = "\n".join(entry_lines) + "\n"
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
        PROJECTS_DIR.glob("*/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ) if PROJECTS_DIR.is_dir() else []
    if not files:
        print("未在 %s 下找到任何 .jsonl transcript。" % PROJECTS_DIR)
        print("请先用 Claude Code 聊几句再来,或检查 CLAUDE_CONFIG_DIR。")
        return 1
    f = files[0]
    print("最新 transcript: %s" % f)
    print("逐行打印顶层字段、解析出的 role 和正文前 200 字," "请确认解析是否正确:\n")
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
    print(
        "\n如果 role / 正文解析不对,只需修改 ccmem.py 里的"
        " extract_role() / extract_text() 两个函数。"
    )
    return 0


def cmd_index(args):
    conn = open_db()
    if args.force:
        _drop_all(conn)
        conn.close()
        conn = open_db()
    t0 = time.time()
    added = index_all(conn)
    print("新增 %d 个片段,耗时 %.0fms" % (added, (time.time() - t0) * 1000))
    return 0


def cmd_search(args):
    conn = open_db()
    results = search(conn, args.query, cwd=args.cwd, top_k=args.top)
    if not results:
        print("无命中")
        return 0
    for score, r in results:
        print(
            "%.3f  会话 %s · 第 %d 轮 · 项目 %s · %s"
            % (score, r["session_id"][:8], r["turn_idx"], r["project"],
               (r["ts"] or "")[:10])
        )
        if r["user_text"]:
            print("  用户: %s" % r["user_text"][:160].replace("\n", " "))
        if r["asst_text"]:
            print("  助手: %s" % r["asst_text"][:160].replace("\n", " "))
    return 0


def cmd_show(args):
    conn = open_db()
    sids = [
        r["session_id"]
        for r in conn.execute(
            "SELECT DISTINCT session_id FROM chunks WHERE session_id LIKE ?"
            " ORDER BY session_id",
            (args.session + "%",),
        )
    ]
    if not sids:
        print("没有以 %r 开头的会话 id。用 stats/search 查看现有会话。" % args.session,
              file=sys.stderr)
        return 1
    if len(sids) > 1:
        print("前缀 %r 不唯一,候选:" % args.session, file=sys.stderr)
        for s in sids:
            print("  " + s, file=sys.stderr)
        return 1
    sid = sids[0]
    row = conn.execute(
        "SELECT path FROM chunks WHERE session_id=? LIMIT 1", (sid,)
    ).fetchone()
    path = row["path"]

    if os.path.exists(path):
        # 优先从原始 JSONL 重放,拿未截断全文
        turns, _lines, _next = parse_turns(path)
        source = "原始 transcript"
    else:
        turns = [
            {"idx": r["turn_idx"], "ts": r["ts"],
             "user": r["user_text"], "asst": [r["asst_text"]]}
            for r in conn.execute(
                "SELECT * FROM chunks WHERE session_id=? ORDER BY turn_idx", (sid,)
            )
        ]
        source = "索引缓存(原 transcript 已删除,文本是截断版)"

    if args.around is not None:
        lo, hi = args.around - 2, args.around + 2
        turns = [t for t in turns if lo <= t["idx"] <= hi]

    out = ["会话 %s | 来源: %s | 共 %d 轮\n" % (sid, source, len(turns))]
    for t in turns:
        out.append("── 第 %d 轮 %s ──" % (t["idx"], (t["ts"] or "")[:19]))
        if t["user"]:
            out.append("用户: %s" % t["user"])
        asst = "\n".join(t["asst"]) if isinstance(t["asst"], list) else t["asst"]
        if asst:
            out.append("助手: %s" % asst)
        out.append("")
    text = "\n".join(out)
    if len(text) > args.max_chars:
        text = text[: args.max_chars]
        text += (
            "\n……(输出超过 %d 字符已截断。可用 --around <轮次号> 只看目标轮附近,"
            "或调大 --max-chars。)" % args.max_chars
        )
    print(text)
    return 0


def cmd_stats(_args):
    conn = open_db()
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_sess = conn.execute("SELECT COUNT(DISTINCT session_id) FROM chunks").fetchone()[0]
    n_proj = conn.execute("SELECT COUNT(DISTINCT project) FROM chunks").fetchone()[0]
    size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    print("索引库: %s" % DB_PATH)
    print("片段数: %d" % n_chunks)
    print("会话数: %d" % n_sess)
    print("项目数: %d" % n_proj)
    print("库大小: %.1f KB" % (size / 1024.0))
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
        conn = open_db()
        results = search(
            conn,
            prompt,
            cwd=data.get("cwd"),
            exclude_session=data.get("session_id"),
        )
        if not results:
            return 0
        text = build_injection(results)
        if not text:
            return 0
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": text,
            },
            "suppressOutput": True,
        }
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    except Exception:
        pass  # 静默:记忆失效的代价是没有记忆,不能是没法干活
    return 0


def cmd_hook_index(_args):
    """Stop / SessionEnd hook 入口:增量索引,优先只处理当前 transcript。"""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        conn = open_db()
        tp = data.get("transcript_path")
        if tp and os.path.exists(tp):
            index_file(conn, tp)
        else:
            index_all(conn)
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------- main


def main(argv=None):
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
    p.add_argument("--full", action="store_true", help="整场会话(默认行为)")
    p.add_argument("--max-chars", type=int, default=20000)

    sub.add_parser("stats", help="索引条目/会话/项目数、库大小")
    sub.add_parser("recall", help="UserPromptSubmit hook 入口(读 stdin)")
    sub.add_parser("hook-index", help="Stop/SessionEnd hook 入口(读 stdin)")

    args = ap.parse_args(argv)
    handlers = {
        "inspect": cmd_inspect,
        "index": cmd_index,
        "search": cmd_search,
        "show": cmd_show,
        "stats": cmd_stats,
        "recall": cmd_recall,
        "hook-index": cmd_hook_index,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
