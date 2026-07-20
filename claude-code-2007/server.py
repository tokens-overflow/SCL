#!/usr/bin/env python3
"""Claude Code 2007 —— QQ 2007 风格的 Claude Code 本地壳。

零依赖（仅 Python 3 标准库）。用法：

    python3 server.py [端口]      # 默认 8787，然后打开 http://localhost:8787

原理：后端为每个任务 spawn 一个原生 claude CLI 进程（headless stream-json 模式），
stdout 的 JSON 事件流通过 SSE 转发给浏览器，stdin 写入 JSON 行实现多轮对话。
Claude Code 本体（登录、~/.claude 配置、会话存档、权限、MCP）完全不变，
终端里 `claude --resume <session_id>` 可无缝接管界面中开的任务。
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
EVENTS_DIR = DATA_DIR / "events"
TASKS_FILE = DATA_DIR / "tasks.json"
SCHED_FILE = DATA_DIR / "schedules.json"
CAP_FILE = DATA_DIR / "capabilities.json"
FRIENDS_FILE = DATA_DIR / "friends.json"
PROFILE_FILE = DATA_DIR / "profile.json"
MOMENTS_FILE = DATA_DIR / "moments.json"
CONFIG_FILE = BASE / "config.json"

# 可用环境变量指向别的二进制（例如测试 stub）
CLAUDE_BIN = os.environ.get("CLAUDE2007_CLAUDE_BIN", "claude")

# 斜杠命令的中文说明（headless stream-json 模式下把 "/xxx" 当作用户消息发出即可触发）。
# 实际可用列表以真实任务的 init 事件为准（会动态合并进来），这里只是给常用命令配上说明和排序。
SLASH_DESCRIPTIONS = {
    "context": "查看当前上下文 token 用量",
    "code-review": "审查当前改动（bug / 简化 / 效率）",
    "review": "审查一个 GitHub PR",
    "security-review": "对改动做安全审查",
    "simplify": "简化 / 重构当前改动并应用",
    "verify": "端到端验证改动是否真的生效",
    "run": "启动并驱动本项目 app",
    "init": "扫描代码库生成 CLAUDE.md",
    "usage": "查看用量额度",
    "usage-credits": "查看积分余额",
    "insights": "使用洞察",
    "recap": "回顾最近的工作",
    "agents": "管理子代理",
    "mcp": "管理 MCP 服务器",
    "schedule": "创建 / 管理定时云代理",
    "loop": "按间隔循环运行某个命令",
    "model": "切换模型",
    "compact": "压缩会话上下文",
    "doctor": "诊断 Claude Code 安装",
    "claude-api": "Claude API / SDK 参考",
    "dataviz": "生成规范的图表 / 可视化",
    "design": "前端设计规范助手",
    "chatgpt": "（技能）从终端调用 ChatGPT",
    "crowdworks": "（技能）在 CrowdWorks 上找活应募",
    "home": "（技能）操作 ~ 目录",
    "goal": "设定 / 查看目标",
    "batch": "批量处理",
}
# 展示优先顺序（有说明的常用命令排前面）
SLASH_ORDER = list(SLASH_DESCRIPTIONS.keys())

DEFAULT_CONFIG = {
    "user_name": "我",
    "default_model": "sonnet",
    "default_permission_mode": "acceptEdits",
    "models": ["sonnet", "opus", "haiku"],
    "projects": [
        {"name": "当前目录", "path": ".", "pinned": True},
    ],
    # 「我的好友」：每个好友一套人设，点一下就新建一个跟"他"的对话（引擎仍是 Claude）
    "friends": [
        {"name": "Claude 小蓝", "avatar": "🤖", "sign": "随时待命～",
         "persona": "你是 Claude 小蓝，一个可靠、热心的编程搭子，说话简洁、带点亲切的语气。"},
        {"name": "小美", "avatar": "👧", "sign": "谁帮我修下CSS",
         "persona": "你是小美，一个毒舌但技术很强的前端妹子，说话直接、爱吐槽，但每次都能把问题真的解决掉。"},
        {"name": "老王", "avatar": "🧔", "sign": "有 Bug 找我",
         "persona": "你是老王，一个经验丰富、稳重的后端老工程师，讲话像带徒弟，先讲原理再给方案。"},
    ],
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[config] 解析 config.json 失败，使用默认配置: {e}")
    projects = list(cfg.get("projects", []))
    # 傻瓜化：永远保证有个「当前目录」兜底，换电脑没配置也能直接用
    if not any(p.get("path") == "." for p in projects):
        projects.append({"name": "当前目录", "path": ".", "pinned": False})
    for p in projects:
        p["abspath"] = str(Path(os.path.expanduser(p["path"])).resolve())
        p["exists"] = os.path.isdir(p["abspath"])
    # 存在的项目排前面（默认取第一个总是能用的），缺失的沉到后面
    projects.sort(key=lambda p: not p["exists"])
    cfg["projects"] = projects
    return cfg


class TaskManager:
    """任务 = 一个 claude CLI 会话。进程按需存活，事件落盘可回放。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.tasks = {}          # id -> task dict
        self.procs = {}          # id -> subprocess.Popen
        self.subscribers = {}    # id -> set(queue.Queue)
        # 真实任务的 init 事件里带 slash_commands，遇到就合并进来（保持与用户实际环境一致）
        self.slash_names = list(SLASH_ORDER)
        # 能力面板（MCP / 技能 / 子代理 / 插件）也从 init 事件抓取并落盘，重启后仍在
        self.capabilities = {}
        if CAP_FILE.exists():
            try:
                self.capabilities = json.loads(CAP_FILE.read_text(encoding="utf-8"))
            except Exception:
                self.capabilities = {}
        DATA_DIR.mkdir(exist_ok=True)
        EVENTS_DIR.mkdir(exist_ok=True)
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        if TASKS_FILE.exists():
            try:
                for t in json.loads(TASKS_FILE.read_text(encoding="utf-8")):
                    # 重启后进程都不在了
                    if t.get("status") == "running":
                        t["status"] = "idle"
                    self.tasks[t["id"]] = t
            except Exception as e:
                print(f"[tasks] 加载 tasks.json 失败: {e}")

    def _save(self):
        with self.lock:
            items = sorted(self.tasks.values(), key=lambda t: t["created_at"])
            TASKS_FILE.write_text(
                json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    # ---------- 事件 ----------
    def _events_path(self, task_id):
        return EVENTS_DIR / f"{task_id}.jsonl"

    def emit(self, task_id, obj):
        line = json.dumps(obj, ensure_ascii=False)
        with self.lock:
            with open(self._events_path(task_id), "a", encoding="utf-8") as f:
                f.write(line + "\n")
            subs = list(self.subscribers.get(task_id, ()))
            task = self.tasks.get(task_id)
            if task is not None:
                self._apply_event(task, obj)
        for q in subs:
            q.put(line)

    def _apply_event(self, task, obj):
        t = obj.get("type")
        if t == "system" and obj.get("subtype") == "init":
            task["session_id"] = obj.get("session_id") or task.get("session_id")
            task["model"] = obj.get("model") or task.get("model")
            # 用真实环境的命令列表补全缓存：已知的保持原顺序，新发现的追加到后面
            for name in obj.get("slash_commands", []):
                if name and not name.startswith("__") and name not in self.slash_names:
                    self.slash_names.append(name)
            # 能力面板快照
            self.capabilities = {
                "model": obj.get("model"),
                "version": obj.get("claude_code_version"),
                "mcp_servers": obj.get("mcp_servers", []),
                "skills": obj.get("skills", []),
                "agents": obj.get("agents", []),
                "plugins": obj.get("plugins", []),
                "slash_count": len([n for n in obj.get("slash_commands", []) if not n.startswith("__")]),
                "updated_at": time.time(),
            }
            try:
                CAP_FILE.write_text(
                    json.dumps(self.capabilities, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
            self._save()
        elif t == "result":
            task["status"] = "error" if obj.get("is_error") else "idle"
            task["last_cost_usd"] = obj.get("total_cost_usd")
            task["updated_at"] = time.time()
            self._save()

    def subscribe(self, task_id):
        q = queue.Queue()
        with self.lock:
            self.subscribers.setdefault(task_id, set()).add(q)
        return q

    def unsubscribe(self, task_id, q):
        with self.lock:
            self.subscribers.get(task_id, set()).discard(q)

    def replay(self, task_id):
        path = self._events_path(task_id)
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    # ---------- 进程 ----------
    def _spawn(self, task, resume):
        args = [
            CLAUDE_BIN, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
        if task.get("model"):
            args += ["--model", task["model"]]
        mode = task.get("permission_mode")
        if mode and mode != "default":
            args += ["--permission-mode", mode]
        for d in task.get("add_dirs", []):        # 「附加」= 允许 Claude 访问的额外目录
            args += ["--add-dir", d]
        if resume and task.get("session_id"):
            args += ["--resume", task["session_id"]]
        proc = subprocess.Popen(
            args,
            cwd=task["cwd"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.procs[task["id"]] = proc
        threading.Thread(
            target=self._read_stdout, args=(task["id"], proc), daemon=True
        ).start()
        threading.Thread(
            target=self._read_stderr, args=(task["id"], proc), daemon=True
        ).start()
        return proc

    def _read_stdout(self, task_id, proc):
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                obj = {"type": "x-raw", "text": line}
            self.emit(task_id, obj)
        code = proc.wait()
        with self.lock:
            if self.procs.get(task_id) is proc:
                del self.procs[task_id]
            task = self.tasks.get(task_id)
            if task and task["status"] == "running":
                task["status"] = "error" if code else "idle"
                self._save()
        self.emit(task_id, {"type": "x-proc-exit", "code": code})

    def _read_stderr(self, task_id, proc):
        tail = []
        for line in proc.stderr:
            tail.append(line.rstrip())
            tail = tail[-40:]
        if proc.wait() != 0 and tail:
            self.emit(task_id, {"type": "x-stderr", "text": "\n".join(tail)})

    def _write_user_message(self, proc, text):
        msg = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    # ---------- 对外操作 ----------
    def create_task(self, title, project, cwd, model, permission_mode, prompt,
                    add_dirs=None, agent_name=None, agent_avatar=None):
        task = {
            "id": uuid.uuid4().hex[:12],
            "title": title or (prompt[:16] + ("…" if len(prompt) > 16 else "")),
            "project": project,
            "cwd": cwd,
            "model": model,
            "permission_mode": permission_mode,
            "add_dirs": add_dirs or [],
            "agent_name": agent_name,        # 跟谁在聊(好友名)
            "agent_avatar": agent_avatar,    # 好友头像(emoji 或 qqN)
            "status": "running",
            "session_id": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with self.lock:
            self.tasks[task["id"]] = task
            self._save()
            proc = self._spawn(task, resume=False)
        self.emit(task["id"], {"type": "x-user", "text": prompt, "ts": time.time()})
        self._write_user_message(proc, prompt)
        return task

    def send_message(self, task_id, text):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            proc = self.procs.get(task_id)
            if proc is None or proc.poll() is not None:
                proc = self._spawn(task, resume=True)
            task["status"] = "running"
            task["updated_at"] = time.time()
            self._save()
        self.emit(task_id, {"type": "x-user", "text": text, "ts": time.time()})
        try:
            self._write_user_message(proc, text)
        except (BrokenPipeError, OSError) as e:
            self.emit(task_id, {"type": "x-stderr", "text": f"写入失败: {e}"})
            with self.lock:
                task["status"] = "error"
                self._save()
        return task

    def interrupt(self, task_id):
        with self.lock:
            proc = self.procs.get(task_id)
            task = self.tasks.get(task_id)
            if task:
                task["status"] = "idle"
                self._save()
        if proc and proc.poll() is None:
            proc.terminate()
        return task

    def set_pinned(self, task_id, pinned):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            task["pinned"] = bool(pinned)
            self._save()
            return task

    def delete_task(self, task_id):
        with self.lock:
            proc = self.procs.pop(task_id, None)
            self.tasks.pop(task_id, None)
            self.subscribers.pop(task_id, None)
            self._save()
        if proc and proc.poll() is None:
            proc.terminate()
        try:
            self._events_path(task_id).unlink(missing_ok=True)
        except OSError:
            pass

    def _respawn_for_dirs(self, task_id, note):
        """改了附加目录后，结束当前进程；下条消息会用 --resume + --add-dir 重开同一会话生效。"""
        with self.lock:
            proc = self.procs.get(task_id)
            task = self.tasks.get(task_id)
            if task:
                task["status"] = "idle"
                self._save()
        if proc and proc.poll() is None:
            proc.terminate()
        self.emit(task_id, {"type": "x-sys", "text": note})

    def add_dir(self, task_id, path):
        d = str(Path(os.path.expanduser(path)).resolve())
        if not os.path.isdir(d):
            raise ValueError(f"目录不存在: {d}")
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            dirs = task.setdefault("add_dirs", [])
            if d == task["cwd"] or d in dirs:
                return task
            dirs.append(d)
            self._save()
        self._respawn_for_dirs(task_id, f"已添加允许访问目录：{d}（下条消息起生效）")
        return task

    def remove_dir(self, task_id, path):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            dirs = task.setdefault("add_dirs", [])
            if path in dirs:
                dirs.remove(path)
                self._save()
        self._respawn_for_dirs(task_id, f"已移除允许访问目录：{path}（下条消息起生效）")
        return task


def resolve_cwd(proj_name):
    proj = next((p for p in CONFIG["projects"] if p["name"] == proj_name), None)
    return proj["abspath"] if proj else os.getcwd()


class Scheduler:
    """本地定时任务：到点就 spawn 一个 claude 任务（跟手动新建完全一样）。
    支持三种触发：interval（每 N 分钟）/ daily（每天 HH:MM）/ once（一次性时间戳）。"""

    def __init__(self, manager):
        self.manager = manager
        self.lock = threading.RLock()
        self.items = {}       # id -> schedule dict
        self._load()
        threading.Thread(target=self._loop, daemon=True).start()

    # ---------- 持久化 ----------
    def _load(self):
        if SCHED_FILE.exists():
            try:
                for s in json.loads(SCHED_FILE.read_text(encoding="utf-8")):
                    self.items[s["id"]] = s
            except Exception as e:
                print(f"[sched] 加载 schedules.json 失败: {e}")

    def _save(self):
        with self.lock:
            items = sorted(self.items.values(), key=lambda s: s["created_at"])
            SCHED_FILE.write_text(
                json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    # ---------- 计算下次运行 ----------
    def _compute_next(self, s, after=None):
        now = after if after is not None else time.time()
        typ = s.get("sched_type")
        if typ == "interval":
            mins = max(1, int(s.get("interval_min") or 60))
            base = s.get("last_run") or s.get("created_at") or now
            nxt = base + mins * 60
            while nxt <= now:
                nxt += mins * 60
            return nxt
        if typ == "daily":
            hh, mm = (s.get("at_time") or "09:00").split(":")
            lt = time.localtime(now)
            cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                int(hh), int(mm), 0, 0, 0, -1))
            if cand <= now:
                cand += 86400
            return cand
        if typ == "once":
            return s.get("at_datetime")
        return None

    # ---------- 对外操作 ----------
    def create(self, data):
        sid = uuid.uuid4().hex[:12]
        s = {
            "id": sid,
            "title": (data.get("title") or "").strip(),
            "project": data.get("project") or "",
            "model": data.get("model") or CONFIG["default_model"],
            "permission_mode": data.get("permission_mode") or CONFIG["default_permission_mode"],
            "prompt": (data.get("prompt") or "").strip(),
            "sched_type": data.get("sched_type") or "interval",
            "interval_min": int(data.get("interval_min") or 60),
            "at_time": data.get("at_time") or "09:00",
            "at_datetime": data.get("at_datetime"),
            "enabled": True,
            "created_at": time.time(),
            "last_run": None,
            "last_task_id": None,
        }
        s["next_run"] = self._compute_next(s)
        with self.lock:
            self.items[sid] = s
            self._save()
        return s

    def toggle(self, sid, enabled):
        with self.lock:
            s = self.items.get(sid)
            if not s:
                raise KeyError(sid)
            s["enabled"] = bool(enabled)
            if s["enabled"] and not s.get("next_run"):
                s["next_run"] = self._compute_next(s)
            self._save()
            return s

    def delete(self, sid):
        with self.lock:
            self.items.pop(sid, None)
            self._save()

    def run_now(self, sid):
        with self.lock:
            s = self.items.get(sid)
            if not s:
                raise KeyError(sid)
        return self._fire(s)

    def list(self):
        with self.lock:
            return sorted(self.items.values(), key=lambda s: s["created_at"], reverse=True)

    # ---------- 触发 ----------
    def _fire(self, s):
        cwd = resolve_cwd(s["project"])
        if not os.path.isdir(cwd):
            with self.lock:
                s["last_error"] = f"项目目录不存在: {cwd}"
                self._save()
            return None
        title = ("⏰ " + (s["title"] or s["prompt"][:16]))
        task = self.manager.create_task(
            title=title, project=s["project"] or "(定时)", cwd=cwd,
            model=s["model"], permission_mode=s["permission_mode"], prompt=s["prompt"],
        )
        with self.lock:
            s["last_run"] = time.time()
            s["last_task_id"] = task["id"]
            s["last_error"] = None
            if s["sched_type"] == "once":
                s["enabled"] = False
                s["next_run"] = None
            else:
                s["next_run"] = self._compute_next(s, after=s["last_run"])
            self._save()
        return task

    def _loop(self):
        while True:
            time.sleep(15)
            now = time.time()
            due = []
            with self.lock:
                for s in self.items.values():
                    if s.get("enabled") and s.get("next_run") and s["next_run"] <= now:
                        due.append(s)
            for s in due:
                try:
                    self._fire(s)
                except Exception as e:
                    print(f"[sched] 触发失败 {s.get('id')}: {e}")


class FriendStore:
    """「我的好友」：可在界面里添加/删除，每个好友一套人设。持久化在 data/friends.json，
    首次用 config.json 里的 friends 做种子。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.friends = []
        self._load()

    def _load(self):
        if FRIENDS_FILE.exists():
            try:
                self.friends = json.loads(FRIENDS_FILE.read_text(encoding="utf-8"))
                return
            except Exception:
                pass
        self.friends = [{**f, "id": uuid.uuid4().hex[:8]} for f in CONFIG.get("friends", [])]
        self._save()

    def _save(self):
        with self.lock:
            FRIENDS_FILE.write_text(
                json.dumps(self.friends, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def list(self):
        with self.lock:
            return list(self.friends)

    def add(self, data):
        f = {
            "id": uuid.uuid4().hex[:8],
            "name": (data.get("name") or "").strip() or "新朋友",
            "avatar": (data.get("avatar") or "🙂").strip()[:16] or "🙂",
            "sign": (data.get("sign") or "").strip(),
            "persona": (data.get("persona") or "").strip(),
            "project": data.get("project") or "",
            "model": data.get("model") or "",
        }
        with self.lock:
            self.friends.append(f)
            self._save()
        return f

    def delete(self, fid):
        with self.lock:
            self.friends = [f for f in self.friends if f.get("id") != fid]
            self._save()


class MomentStore:
    """QQ空间的动态。持久化在 data/moments.json，首次用好友的签名生成几条好友动态当种子。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.moments = []
        self._load()

    def _load(self):
        if MOMENTS_FILE.exists():
            try:
                self.moments = json.loads(MOMENTS_FILE.read_text(encoding="utf-8"))
                return
            except Exception:
                pass
        seed = []
        now = time.time()
        for i, f in enumerate(FRIENDS.list()):
            text = f.get("sign") or "今天也在线，随时找我～"
            seed.append({
                "id": uuid.uuid4().hex[:8], "author_name": f.get("name"),
                "author_avatar": f.get("avatar", "🙂"), "text": text,
                "ts": now - (i + 1) * 3600, "likes": (i * 3) % 7, "mine": False,
            })
        self.moments = seed
        self._save()

    def _save(self):
        with self.lock:
            MOMENTS_FILE.write_text(
                json.dumps(self.moments, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def list(self):
        with self.lock:
            return sorted(self.moments, key=lambda m: m["ts"], reverse=True)

    def add_mine(self, text):
        m = {
            "id": uuid.uuid4().hex[:8], "author_name": PROFILE.get("name", "我"),
            "author_avatar": PROFILE.get("avatar", "qq1"), "text": text.strip(),
            "ts": time.time(), "likes": 0, "mine": True,
        }
        with self.lock:
            self.moments.append(m)
            self._save()
        return m

    def like(self, mid):
        with self.lock:
            for m in self.moments:
                if m["id"] == mid:
                    m["likes"] = m.get("likes", 0) + 1
                    self._save()
                    return m
            raise KeyError(mid)

    def delete(self, mid):
        with self.lock:
            self.moments = [m for m in self.moments if m["id"] != mid]
            self._save()


def load_profile():
    if PROFILE_FILE.exists():
        try:
            return json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    p = {"name": CONFIG.get("user_name", "我"), "avatar": "qq1"}
    save_profile(p)
    return p


def save_profile(p):
    PROFILE_FILE.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")


MANAGER = TaskManager()
CONFIG = load_config()
SCHEDULER = Scheduler(MANAGER)
FRIENDS = FriendStore()
PROFILE = load_profile()
MOMENTS = MomentStore()


SKILLS_DIR = Path(os.path.expanduser("~/.claude/skills"))


def _skill_slug(name):
    slug = re.sub(r"[^a-z0-9-]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "skill"


def _parse_skill(md):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", md, re.S)
    meta, body = {}, md
    if m:
        fm, body = m.group(1), m.group(2)
        for line in fm.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    return meta, body.lstrip("\n")


def _skill_md(name, description, body):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body.rstrip()}\n"


def list_skills():
    out = []
    if SKILLS_DIR.is_dir():
        for d in sorted(SKILLS_DIR.iterdir()):
            f = d / "SKILL.md"
            if d.is_dir() and f.is_file():
                try:
                    meta, _ = _parse_skill(f.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
                out.append({"dir": d.name, "name": meta.get("name", d.name),
                            "description": meta.get("description", "")})
    return out


def read_skill(dirname):
    f = SKILLS_DIR / dirname / "SKILL.md"
    if not f.is_file():
        return None
    meta, body = _parse_skill(f.read_text(encoding="utf-8"))
    return {"dir": dirname, "name": meta.get("name", dirname),
            "description": meta.get("description", ""), "body": body}


def create_skill(name, description, body):
    slug = _skill_slug(name)
    d = SKILLS_DIR / slug
    if d.exists():
        raise ValueError(f"已存在同名 skill: {slug}")
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_skill_md(slug, description, body or ""), encoding="utf-8")
    return read_skill(slug)


def save_skill(dirname, description, body):
    d = SKILLS_DIR / dirname
    f = d / "SKILL.md"
    if not f.is_file():
        raise KeyError(dirname)
    meta, _ = _parse_skill(f.read_text(encoding="utf-8"))
    f.write_text(_skill_md(meta.get("name", dirname), description, body or ""), encoding="utf-8")
    return read_skill(dirname)


def delete_skill(dirname):
    d = SKILLS_DIR / dirname
    # 只允许删除 ~/.claude/skills 下的目录，且必须真的在这个目录里
    if d.is_dir() and d.resolve().parent == SKILLS_DIR.resolve():
        shutil.rmtree(d)


class QuietHTTPServer(ThreadingHTTPServer):
    """浏览器（尤其 SSE 长连接）断开时会抛 ConnectionResetError/BrokenPipe，
    默认会打一大堆 traceback。这里安静忽略这类客户端断连错误。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        et = sys.exc_info()[0]
        if et is not None and issubclass(
            et, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)
        ):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # SSE 长连接会刷屏，安静一点
        pass

    # ---------- 工具 ----------
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            body = (BASE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/config":
            self._json(CONFIG)
        elif path == "/api/slashcommands":
            with MANAGER.lock:
                names = list(MANAGER.slash_names)
            self._json([
                {"name": n, "desc": SLASH_DESCRIPTIONS.get(n, "")} for n in names
            ])
        elif path == "/api/friends":
            self._json(FRIENDS.list())
        elif path == "/api/profile":
            self._json(PROFILE)
        elif path == "/api/moments":
            self._json(MOMENTS.list())
        elif path == "/api/skills":
            self._json(list_skills())
        elif path.startswith("/api/skills/"):
            dirname = path.split("/")[3]
            sk = read_skill(dirname)
            self._json(sk if sk else {"error": "skill 不存在"}, 200 if sk else 404)
        elif path == "/api/capabilities":
            with MANAGER.lock:
                self._json(dict(MANAGER.capabilities))
        elif path == "/api/schedules":
            self._json(SCHEDULER.list())
        elif path == "/api/claudemd":
            qs = parse_qs(urlparse(self.path).query)
            proj = (qs.get("project") or [""])[0]
            cwd = resolve_cwd(proj)
            f = Path(cwd) / "CLAUDE.md"
            self._json({
                "project": proj, "path": str(f), "exists": f.exists(),
                "content": f.read_text(encoding="utf-8") if f.exists() else "",
            })
        elif path == "/api/tasks":
            with MANAGER.lock:
                items = sorted(
                    MANAGER.tasks.values(), key=lambda t: t["created_at"], reverse=True
                )
            self._json(items)
        elif path.startswith("/api/tasks/") and path.endswith("/events"):
            task_id = path.split("/")[3]
            self._sse(task_id)
        elif path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json({"error": "not found"}, 404)

    def _sse(self, task_id):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = MANAGER.subscribe(task_id)
        try:
            # 先回放历史（订阅之后再回放，避免漏事件；重复事件由前端幂等处理）
            for line in MANAGER.replay(task_id):
                self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
            self.wfile.write(b"event: ready\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    line = q.get(timeout=15)
                    self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            MANAGER.unsubscribe(task_id, q)

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._read_body()
        except ValueError:
            return self._json({"error": "bad json"}, 400)

        if path == "/api/tasks":
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                return self._json({"error": "prompt 不能为空"}, 400)
            proj_name = body.get("project") or ""
            proj = next(
                (p for p in CONFIG["projects"] if p["name"] == proj_name), None
            )
            cwd = proj["abspath"] if proj else os.getcwd()
            if not os.path.isdir(cwd):
                cwd = os.getcwd()   # 傻瓜化：目录不存在就退回当前目录，别报错挡住用户
            task = MANAGER.create_task(
                title=(body.get("title") or "").strip(),
                project=proj_name or "(默认)",
                cwd=cwd,
                model=body.get("model") or CONFIG["default_model"],
                permission_mode=body.get("permission_mode")
                or CONFIG["default_permission_mode"],
                prompt=prompt,
                add_dirs=body.get("add_dirs") or [],
            )
            return self._json(task)

        if path.startswith("/api/tasks/"):
            parts = path.split("/")
            task_id, action = parts[3], parts[4] if len(parts) > 4 else ""
            try:
                if action == "message":
                    text = (body.get("text") or "").strip()
                    if not text:
                        return self._json({"error": "text 不能为空"}, 400)
                    return self._json(MANAGER.send_message(task_id, text))
                if action == "interrupt":
                    return self._json(MANAGER.interrupt(task_id) or {})
                if action == "adddir":
                    try:
                        return self._json(MANAGER.add_dir(task_id, body.get("path") or ""))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                if action == "rmdir":
                    return self._json(MANAGER.remove_dir(task_id, body.get("path") or ""))
                if action == "delete":
                    MANAGER.delete_task(task_id)
                    return self._json({"ok": True})
                if action == "pin":
                    return self._json(MANAGER.set_pinned(task_id, body.get("pinned")))
            except KeyError:
                return self._json({"error": "任务不存在"}, 404)

        if path == "/api/skills":
            if not (body.get("name") or "").strip():
                return self._json({"error": "skill 名字不能为空"}, 400)
            try:
                return self._json(create_skill(body.get("name"), body.get("description") or "", body.get("body") or ""))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        if path.startswith("/api/skills/"):
            parts = path.split("/")
            dirname, action = parts[3], parts[4] if len(parts) > 4 else ""
            try:
                if action == "save":
                    return self._json(save_skill(dirname, body.get("description") or "", body.get("body") or ""))
                if action == "delete":
                    delete_skill(dirname)
                    return self._json({"ok": True})
            except KeyError:
                return self._json({"error": "skill 不存在"}, 404)

        if path == "/api/moments":
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"error": "动态内容不能为空"}, 400)
            return self._json(MOMENTS.add_mine(text))

        if path.startswith("/api/moments/"):
            parts = path.split("/")
            mid, action = parts[3], parts[4] if len(parts) > 4 else ""
            try:
                if action == "like":
                    return self._json(MOMENTS.like(mid))
                if action == "delete":
                    MOMENTS.delete(mid)
                    return self._json({"ok": True})
            except KeyError:
                return self._json({"error": "动态不存在"}, 404)

        if path == "/api/profile":
            PROFILE["name"] = (body.get("name") or "").strip() or PROFILE.get("name") or "我"
            PROFILE["avatar"] = (body.get("avatar") or "").strip()[:16] or PROFILE.get("avatar") or "qq1"
            save_profile(PROFILE)
            return self._json(PROFILE)

        if path == "/api/friends":
            if not (body.get("name") or "").strip():
                return self._json({"error": "好友名字不能为空"}, 400)
            return self._json(FRIENDS.add(body))

        if path.startswith("/api/friends/"):
            parts = path.split("/")
            fid, action = parts[3], parts[4] if len(parts) > 4 else ""
            if action == "delete":
                FRIENDS.delete(fid)
                return self._json({"ok": True})

        if path == "/api/schedules":
            if not (body.get("prompt") or "").strip():
                return self._json({"error": "指令不能为空"}, 400)
            return self._json(SCHEDULER.create(body))

        if path.startswith("/api/schedules/"):
            parts = path.split("/")
            sid, action = parts[3], parts[4] if len(parts) > 4 else ""
            try:
                if action == "toggle":
                    return self._json(SCHEDULER.toggle(sid, body.get("enabled")))
                if action == "run":
                    t = SCHEDULER.run_now(sid)
                    return self._json(t or {"error": "运行失败（项目目录不存在？）"},
                                      200 if t else 400)
                if action == "delete":
                    SCHEDULER.delete(sid)
                    return self._json({"ok": True})
            except KeyError:
                return self._json({"error": "定时任务不存在"}, 404)

        if path == "/api/claudemd":
            proj = body.get("project") or ""
            cwd = resolve_cwd(proj)
            if not os.path.isdir(cwd):
                return self._json({"error": f"项目目录不存在: {cwd}"}, 400)
            f = Path(cwd) / "CLAUDE.md"
            try:
                f.write_text(body.get("content") or "", encoding="utf-8")
            except OSError as e:
                return self._json({"error": f"写入失败: {e}"}, 500)
            return self._json({"ok": True, "path": str(f)})

        self._json({"error": "not found"}, 404)


def _browser_candidates():
    """按平台返回 Chromium 系浏览器的候选路径（mac / windows / linux）。"""
    if sys.platform == "darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    if os.name == "nt":  # Windows
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        return [
            os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pfx86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pfx86, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
        ]
    # linux
    return [shutil.which(b) for b in
            ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave-browser")]


def open_app_window(url):
    """用 Chromium 系浏览器的 --app 模式打开：无地址栏 / 无标签页，像个独立聊天窗口。
    找不到就退回系统默认浏览器（普通标签页）。跨平台（mac / windows / linux）。"""
    binp = next((c for c in _browser_candidates() if c and os.path.exists(c)), None)
    if not binp:
        import webbrowser
        webbrowser.open(url)
        print("[app] 未找到 Chrome/Edge，已用默认浏览器打开（带地址栏）")
        return
    profile = str(DATA_DIR / "app-profile")  # 独立 profile，保证是干净的 app 窗口
    args = [
        binp, f"--app={url}", f"--user-data-dir={profile}",
        "--window-size=960,760", "--no-first-run", "--no-default-browser-check",
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[app] 已用 {os.path.basename(binp)} 打开无边框聊天窗口")


def main():
    args = [a for a in sys.argv[1:]]
    app_mode = "--app" in args or os.environ.get("CLAUDE2007_APP") == "1"
    ports = [a for a in args if a.isdigit()]
    port = int(ports[0]) if ports else 8787
    if shutil.which(CLAUDE_BIN) is None:
        print(f"[警告] 找不到 `{CLAUDE_BIN}`，请先安装并登录 Claude Code CLI")
    srv = QuietHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    url = f"http://localhost:{port}"
    print(f"Claude Code 2007 已启动: {url}")
    if app_mode:
        # 稍等一下让 accept 循环起来，再开窗口
        threading.Timer(0.6, open_app_window, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n再见 👋")


if __name__ == "__main__":
    main()
