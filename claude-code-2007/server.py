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
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
EVENTS_DIR = DATA_DIR / "events"
TASKS_FILE = DATA_DIR / "tasks.json"
CONFIG_FILE = BASE / "config.json"

# 可用环境变量指向别的二进制（例如测试 stub）
CLAUDE_BIN = os.environ.get("CLAUDE2007_CLAUDE_BIN", "claude")

DEFAULT_CONFIG = {
    "user_name": "我",
    "default_model": "sonnet",
    "default_permission_mode": "acceptEdits",
    "models": ["sonnet", "opus", "haiku"],
    "projects": [
        {"name": "当前目录", "path": ".", "pinned": True},
    ],
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[config] 解析 config.json 失败，使用默认配置: {e}")
    for p in cfg.get("projects", []):
        p["abspath"] = str(Path(os.path.expanduser(p["path"])).resolve())
        p["exists"] = os.path.isdir(p["abspath"])
    return cfg


class TaskManager:
    """任务 = 一个 claude CLI 会话。进程按需存活，事件落盘可回放。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.tasks = {}          # id -> task dict
        self.procs = {}          # id -> subprocess.Popen
        self.subscribers = {}    # id -> set(queue.Queue)
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
        exe = shutil.which(CLAUDE_BIN) or CLAUDE_BIN
        args = [
            exe, "-p",
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
        if resume and task.get("session_id"):
            args += ["--resume", task["session_id"]]
        # Windows: npm 装的 claude 是 .cmd 批处理,CreateProcess 无法直接执行
        if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
            args = ["cmd", "/c"] + args
        proc = subprocess.Popen(
            args,
            cwd=task["cwd"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",   # 中文 Windows 默认 GBK,必须显式 UTF-8
            errors="replace",
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
    def create_task(self, title, project, cwd, model, permission_mode, prompt):
        task = {
            "id": uuid.uuid4().hex[:12],
            "title": title or (prompt[:16] + ("…" if len(prompt) > 16 else "")),
            "project": project,
            "cwd": cwd,
            "model": model,
            "permission_mode": permission_mode,
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
            if os.name == "nt":
                # 结束整棵进程树(cmd 包装层 + claude 本体)
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            else:
                proc.terminate()
        return task


MANAGER = TaskManager()
CONFIG = load_config()


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
                return self._json({"error": f"项目目录不存在: {cwd}"}, 400)
            task = MANAGER.create_task(
                title=(body.get("title") or "").strip(),
                project=proj_name or "(默认)",
                cwd=cwd,
                model=body.get("model") or CONFIG["default_model"],
                permission_mode=body.get("permission_mode")
                or CONFIG["default_permission_mode"],
                prompt=prompt,
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
            except KeyError:
                return self._json({"error": "任务不存在"}, 404)

        self._json({"error": "not found"}, 404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    if shutil.which(CLAUDE_BIN) is None:
        print(f"[警告] 找不到 `{CLAUDE_BIN}`，请先安装并登录 Claude Code CLI")
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"Claude Code 2007 已启动: http://localhost:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n再见 👋")


if __name__ == "__main__":
    main()
