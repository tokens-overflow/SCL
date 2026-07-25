"""HTTP transport and application composition for QQQQC."""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Type
from urllib.parse import parse_qs, unquote, urlparse

from .cli_adapter import ClaudeCliAdapter
from .netchat import NetChatService
from .scheduler import Scheduler
from .stores import (
    CapabilityStore,
    ClaudeMdStore,
    ConfigStore,
    EventStore,
    FriendStore,
    MomentStore,
    ProfileStore,
    SkillStore,
    TaskStore,
)
from .task_service import TaskService


class AppContext:
    """Owns application services and their dependency graph."""

    def __init__(self, base_dir: Path | str, *, cli: ClaudeCliAdapter | None = None,
                 scheduler_autostart: bool = True, skills_dir: Path | None = None):
        self.base_dir = Path(base_dir).resolve()
        self.data_dir = self.base_dir / "data"
        self.frontend_dir = self.base_dir / "frontend"
        self.avatars_dir = self.frontend_dir / "avatars"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.avatars_dir.mkdir(parents=True, exist_ok=True)
        self.config_store = ConfigStore(self.base_dir)
        self.config = self.config_store.snapshot()
        self.task_store = TaskStore(self.data_dir)
        self.event_store = EventStore(self.data_dir)
        self.capability_store = CapabilityStore(self.data_dir)
        self.cli = cli or ClaudeCliAdapter()
        self.task_service = TaskService(self.task_store, self.event_store, self.capability_store, self.cli)
        self.friends = FriendStore(self.data_dir, self.config.get("friends", []), self.avatars_dir)
        self.profile = ProfileStore(self.data_dir, self.config.get("user_name", "我"))
        self.moments = MomentStore(self.data_dir, self.friends, self.profile)
        self.skills = SkillStore(skills_dir)
        self.claude_md = ClaudeMdStore(self.config_store)
        self.scheduler = Scheduler(self.task_service, self.config_store, self.data_dir,
                                   autostart=scheduler_autostart)
        # 网络好友（互联网真人聊天，GitHub 中转）——未配置则待命，对现有零影响
        self.netchat = NetChatService(self.data_dir)

    def close(self) -> None:
        self.scheduler.stop()
        self.task_service.shutdown()
        self.netchat.shutdown()


class QuietHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        error_type = sys.exc_info()[0]
        if error_type and issubclass(error_type,
                (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def make_handler(context: AppContext) -> Type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        app = context
        max_body_bytes = 24 * 1024 * 1024   # 允许粘贴图片(base64 较大)

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                             "application/json; charset=utf-8", status)

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError as exc:
                raise ApiError("非法 Content-Length") from exc
            if length > self.max_body_bytes:
                raise ApiError("请求体过大", 413)
            if length == 0:
                return {}
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError("bad json") from exc
            if not isinstance(value, dict):
                raise ApiError("JSON 请求体必须是对象")
            return value

        def _validate_local_request(self) -> None:
            host = (self.headers.get("Host") or "").split(":", 1)[0].strip("[]").lower()
            if host not in {"localhost", "127.0.0.1", "::1"}:
                raise ApiError("仅允许 localhost 访问", 403)
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ApiError("拒绝跨站请求", 403)

        def _serve_file(self, path: Path) -> None:
            if not path.is_file():
                raise ApiError("not found", 404)
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            self._send_bytes(path.read_bytes(), content_type)

        def _frontend_path(self, request_path: str) -> Path:
            relative = unquote(request_path.lstrip("/"))
            if relative.startswith("frontend/"):
                relative = relative[len("frontend/"):]
            candidate = (self.app.frontend_dir / relative).resolve()
            if candidate != self.app.frontend_dir and self.app.frontend_dir not in candidate.parents:
                raise ApiError("not found", 404)
            return candidate

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path, query = parsed.path, parse_qs(parsed.query)
                if path in {"/", "/index.html"}:
                    return self._serve_file(self.app.frontend_dir / "index.html")
                if path == "/favicon.ico":
                    self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers(); return
                if path.startswith("/frontend/"):
                    return self._serve_file(self._frontend_path(path))
                if path == "/api/config": return self._json(self.app.config)
                if path == "/api/slashcommands": return self._json(self.app.task_service.slash_commands())
                if path == "/api/tasks": return self._json(self.app.task_service.list_tasks())
                if path.startswith("/api/tasks/") and path.endswith("/events"):
                    return self._sse(path.split("/")[3], query)
                # 网络好友（互联网真人聊天）
                if path == "/api/net/state": return self._json(self.app.netchat.public_state())
                if path == "/api/net/friends": return self._json(self.app.netchat.friends())
                if path == "/api/net/history":
                    return self._json(self.app.netchat.history((query.get("peer") or [""])[0]))
                if path == "/api/net/events":
                    return self._sse_net(query)
                if path == "/api/avatars":
                    exts = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
                    files = sorted(p.name for p in self.app.avatars_dir.iterdir()
                                   if p.is_file() and p.suffix.lower() in exts)
                    return self._json(files)
                if path == "/api/friends": return self._json(self.app.friends.list())
                if path == "/api/profile": return self._json(self.app.profile.get())
                if path == "/api/moments": return self._json(self.app.moments.list())
                if path == "/api/skills": return self._json(self.app.skills.list())
                if path.startswith("/api/skills/"):
                    skill = self.app.skills.read(unquote(path.split("/")[3]))
                    if skill is None: raise ApiError("skill 不存在", 404)
                    return self._json(skill)
                if path == "/api/capabilities": return self._json(self.app.capability_store.get())
                if path == "/api/schedules": return self._json(self.app.scheduler.list())
                if path == "/api/claudemd":
                    return self._json(self.app.claude_md.read((query.get("project") or [""])[0]))
                raise ApiError("not found", 404)
            except ApiError as exc:
                self._json({"error": exc.message}, exc.status)
            except KeyError:
                self._json({"error": "资源不存在"}, 404)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)

        def _sse(self, task_id: str, query: dict[str, list[str]]) -> None:
            try:
                last_id = self.headers.get("Last-Event-ID") or (query.get("lastEventId") or ["0"])[0]
                cursor = max(0, int(last_id or 0))
                channel = self.app.task_service.subscribe(task_id)
            except (KeyError, ValueError):
                raise ApiError("任务不存在", 404)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                for event in self.app.task_service.replay_after(task_id, cursor):
                    seq = int(event.get("_seq") or 0)
                    if seq > cursor:
                        self._write_sse(event); cursor = seq
                self.wfile.write(b"event: ready\ndata: {}\n\n"); self.wfile.flush()
                while True:
                    try:
                        event = channel.get(timeout=15)
                        seq = int(event.get("_seq") or 0)
                        if seq <= cursor: continue
                        self._write_sse(event); cursor = seq
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.app.task_service.unsubscribe(task_id, channel)

        def _sse_net(self, query: dict[str, list[str]]) -> None:
            # 网络好友的事件流（仿 _sse，但订阅 netchat 的轻量扇出）
            last_id = self.headers.get("Last-Event-ID") or (query.get("lastEventId") or ["0"])[0]
            try:
                cursor = max(0, int(last_id or 0))
            except ValueError:
                cursor = 0
            channel = self.app.netchat.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                for event in self.app.netchat.replay_after(cursor):
                    seq = int(event.get("_seq") or 0)
                    if seq > cursor:
                        self._write_sse(event); cursor = seq
                self.wfile.write(b"event: ready\ndata: {}\n\n"); self.wfile.flush()
                while True:
                    try:
                        event = channel.get(timeout=15)
                        seq = int(event.get("_seq") or 0)
                        if seq <= cursor: continue
                        self._write_sse(event); cursor = seq
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.app.netchat.unsubscribe(channel)

        def _write_sse(self, event: dict[str, Any]) -> None:
            seq = int(event.get("_seq") or 0)
            payload = json.dumps(event, ensure_ascii=False)
            self.wfile.write(f"id: {seq}\ndata: {payload}\n\n".encode("utf-8"))

        def do_POST(self) -> None:
            try:
                self._validate_local_request()
                path, body = urlparse(self.path).path, self._read_json()
                # 网络好友（互联网真人聊天）
                if path == "/api/net/setup":
                    return self._json(self.app.netchat.setup(
                        str(body.get("owner") or ""), str(body.get("repo") or ""),
                        str(body.get("token") or ""), str(body.get("handle") or ""),
                        str(body.get("avatar") or "🧑"), str(body.get("sign") or ""),
                        str(body.get("nickname") or "")))
                if path == "/api/net/addfriend":
                    return self._json(self.app.netchat.add_friend(str(body.get("handle") or "")))
                if path == "/api/net/delfriend":
                    return self._json(self.app.netchat.del_friend(str(body.get("handle") or "")))
                if path == "/api/net/clearhistory":
                    return self._json(self.app.netchat.clear_history(str(body.get("peer") or "")))
                if path == "/api/net/send":
                    return self._json(self.app.netchat.send(
                        str(body.get("to") or ""), str(body.get("text") or "")))
                if path == "/api/net/ping":
                    self.app.netchat.mark_active(); return self._json({"ok": True})
                if path == "/api/tasks":
                    prompt = str(body.get("prompt") or "").strip()
                    if not prompt: raise ApiError("prompt 不能为空")
                    project = str(body.get("project") or "")
                    # cwd_path：界面「📁 浏览…」选中的目录，优先于项目下拉；
                    # 不存在则直接报错，别悄悄退回别的目录让人以为选中了。
                    cwd_path = str(body.get("cwd_path") or "").strip()
                    if cwd_path:
                        chosen = Path(os.path.expanduser(cwd_path)).resolve()
                        if not chosen.is_dir(): raise ApiError(f"目录不存在: {chosen}")
                        cwd, project = chosen, chosen.name
                    else:
                        cwd = self.app.config_store.resolve_project(project, fallback_to_cwd=True)
                    task = self.app.task_service.create_task(
                        title=str(body.get("title") or "").strip(), project=project or "(默认)",
                        cwd=cwd,
                        model=str(body.get("model") or self.app.config["default_model"]),
                        permission_mode=str(body.get("permission_mode") or self.app.config["default_permission_mode"]),
                        prompt=prompt, add_dirs=list(body.get("add_dirs") or []),
                        agent_name=str(body.get("agent_name")) if body.get("agent_name") else None,
                        agent_avatar=str(body.get("agent_avatar")) if body.get("agent_avatar") else None)
                    return self._json(task)
                if path.startswith("/api/tasks/"):
                    parts = path.split("/"); task_id = parts[3]; action = parts[4] if len(parts) > 4 else ""
                    if action == "message":
                        text = str(body.get("text") or "").strip()
                        raw = body.get("images")
                        images = [i for i in raw if isinstance(i, dict) and i.get("data")] if isinstance(raw, list) else []
                        if not text and not images: raise ApiError("内容不能为空")
                        return self._json(self.app.task_service.send_message(task_id, text, images))
                    if action == "interrupt": return self._json(self.app.task_service.interrupt(task_id))
                    if action == "model": return self._json(self.app.task_service.set_model(task_id, str(body.get("model") or "")))
                    if action == "adddir": return self._json(self.app.task_service.add_dir(task_id, str(body.get("path") or "")))
                    if action == "rmdir": return self._json(self.app.task_service.remove_dir(task_id, str(body.get("path") or "")))
                    if action == "delete": self.app.task_service.delete_task(task_id); return self._json({"ok": True})
                    if action == "pin": return self._json(self.app.task_service.set_pinned(task_id, bool(body.get("pinned"))))
                    if action == "permission": return self._json(self.app.task_service.set_permission_mode(task_id, str(body.get("permission_mode") or "")))
                    if action == "perm-decide": return self._json(self.app.task_service.resolve_permission(
                        task_id, str(body.get("request_id") or ""),
                        str(body.get("behavior") or "deny"), str(body.get("scope") or "once")))
                    raise ApiError("not found", 404)
                if path == "/api/skills":
                    name = str(body.get("name") or "").strip()
                    if not name: raise ApiError("skill 名字不能为空")
                    return self._json(self.app.skills.create(name, str(body.get("description") or ""), str(body.get("body") or "")))
                if path.startswith("/api/skills/"):
                    parts = path.split("/"); dirname = unquote(parts[3]); action = parts[4] if len(parts) > 4 else ""
                    if action == "save": return self._json(self.app.skills.save(dirname, str(body.get("description") or ""), str(body.get("body") or "")))
                    if action == "delete": self.app.skills.delete(dirname); return self._json({"ok": True})
                    raise ApiError("not found", 404)
                if path == "/api/friends":
                    if not str(body.get("name") or "").strip(): raise ApiError("好友名字不能为空")
                    return self._json(self.app.friends.add(body))
                if path.startswith("/api/friends/"):
                    parts = path.split("/")
                    action = parts[4] if len(parts) > 4 else ""
                    if action == "delete": self.app.friends.delete(parts[3]); return self._json({"ok": True})
                    if action == "update":
                        updated = self.app.friends.update(parts[3], body)
                        if updated is None: raise ApiError("好友不存在", 404)
                        return self._json(updated)
                    raise ApiError("not found", 404)
                if path == "/api/profile": return self._json(self.app.profile.update(body))
                if path == "/api/moments":
                    text = str(body.get("text") or "").strip()
                    if not text: raise ApiError("动态内容不能为空")
                    return self._json(self.app.moments.add_mine(text))
                if path.startswith("/api/moments/"):
                    parts = path.split("/"); moment_id = parts[3]; action = parts[4] if len(parts) > 4 else ""
                    if action == "like": return self._json(self.app.moments.like(moment_id))
                    if action == "delete": self.app.moments.delete(moment_id); return self._json({"ok": True})
                    raise ApiError("not found", 404)
                if path == "/api/schedules":
                    if not str(body.get("prompt") or "").strip(): raise ApiError("指令不能为空")
                    return self._json(self.app.scheduler.create(body))
                if path.startswith("/api/schedules/"):
                    parts = path.split("/"); sid = parts[3]; action = parts[4] if len(parts) > 4 else ""
                    if action == "toggle": return self._json(self.app.scheduler.toggle(sid, bool(body.get("enabled"))))
                    if action == "run":
                        task = self.app.scheduler.run_now(sid)
                        if task is None: raise ApiError("运行失败")
                        return self._json(task)
                    if action == "delete": self.app.scheduler.delete(sid); return self._json({"ok": True})
                    raise ApiError("not found", 404)
                if path == "/api/claudemd":
                    return self._json(self.app.claude_md.save(str(body.get("project") or ""), str(body.get("content") or "")))
                raise ApiError("not found", 404)
            except ApiError as exc:
                self._json({"error": exc.message}, exc.status)
            except KeyError:
                self._json({"error": "资源不存在"}, 404)
            except (ValueError, FileNotFoundError) as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)

    return Handler


def _browser_candidates() -> list[str | None]:
    if sys.platform == "darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    if os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        return [os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
                os.path.join(pfx86, r"Google\Chrome\Application\chrome.exe"),
                os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
                os.path.join(pfx86, r"Microsoft\Edge\Application\msedge.exe"),
                os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe")]
    return [shutil.which(name) for name in
            ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave-browser")]


def open_app_window(url: str, data_dir: Path | None = None) -> None:
    browser = next((item for item in _browser_candidates() if item and Path(item).exists()), None)
    if not browser:
        import webbrowser
        webbrowser.open(url)
        print("[app] 未找到 Chrome/Edge，已用默认浏览器打开（带地址栏）")
        return
    profile = str((data_dir or Path.cwd() / "data") / "app-profile")
    subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}",
        "--window-size=960,760", "--no-first-run", "--no-default-browser-check"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def serve(context: AppContext, port: int = 8787, *, app_mode: bool = False) -> None:
    server = QuietHTTPServer(("127.0.0.1", port), make_handler(context))
    url = f"http://localhost:{port}"
    print(f"QQQQC 已启动: {url}")
    if app_mode:
        threading.Timer(0.6, open_app_window, args=(url, context.data_dir)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n再见 👋")
    finally:
        context.close()
        server.server_close()
