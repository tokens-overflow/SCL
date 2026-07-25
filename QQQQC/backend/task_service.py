"""Task/session application service.

A task is a durable Claude Code conversation.  This service owns task state,
process lifecycle and event fan-out; it does not know about HTTP routes or
scheduler persistence.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from .cli_adapter import ClaudeCliAdapter, CliLaunchSpec, SessionHandle
from .stores import CapabilityStore, EventStore, TaskStore

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


class TaskService:
    def __init__(
        self,
        task_store: TaskStore,
        event_store: EventStore,
        capability_store: CapabilityStore,
        cli: ClaudeCliAdapter,
    ):
        self.tasks = task_store
        self.events = event_store
        self.capabilities = capability_store
        self.cli = cli
        self.lock = threading.RLock()
        self._handles: dict[str, SessionHandle] = {}
        self._subscribers: dict[str, set[queue.Queue[dict[str, Any]]]] = {}
        self._cancelled: set[str] = set()
        # 权限 control protocol 用。request_id -> {task_id, tool_name, input}（UI 応答待ち）と、
        # 「本会话都允许」で許可済みのツール名（task_id -> set）。
        self._perm_pending: dict[str, dict[str, Any]] = {}
        self._perm_session_allow: dict[str, set[str]] = {}
        self._event_seq = {
            task["id"]: max(int(task.get("event_seq") or 0), self.events.max_seq(task["id"]))
            for task in self.tasks.list()
        }
        self._slash_names = list(SLASH_DESCRIPTIONS)

    # ---------- queries ----------
    def list_tasks(self) -> list[dict[str, Any]]:
        return self.tasks.list()

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def slash_commands(self) -> list[dict[str, str]]:
        with self.lock:
            return [
                {"name": name, "desc": SLASH_DESCRIPTIONS.get(name, "")}
                for name in self._slash_names
            ]

    # ---------- event stream ----------
    def subscribe(self, task_id: str) -> queue.Queue[dict[str, Any]]:
        self.get_task(task_id)
        channel: queue.Queue[dict[str, Any]] = queue.Queue()
        with self.lock:
            self._subscribers.setdefault(task_id, set()).add(channel)
        return channel

    def unsubscribe(self, task_id: str, channel: queue.Queue[dict[str, Any]]) -> None:
        with self.lock:
            subscribers = self._subscribers.get(task_id)
            if subscribers is not None:
                subscribers.discard(channel)
                if not subscribers:
                    self._subscribers.pop(task_id, None)

    def replay_after(self, task_id: str, last_seq: int = 0) -> list[dict[str, Any]]:
        self.get_task(task_id)
        return self.events.replay_after(task_id, last_seq)

    def _next_seq(self, task_id: str) -> int:
        with self.lock:
            next_value = int(self._event_seq.get(task_id, 0)) + 1
            self._event_seq[task_id] = next_value
            return next_value

    def _persist_seq(self, task: dict[str, Any]) -> None:
        task["event_seq"] = int(self._event_seq.get(task["id"], task.get("event_seq") or 0))

    def emit(self, task_id: str, event: dict[str, Any]) -> dict[str, Any]:
        persisted = deepcopy(event)
        persisted.setdefault("_ts", time.time())
        persisted["_seq"] = self._next_seq(task_id)
        self.events.append(task_id, persisted)
        self._apply_event(task_id, persisted)
        with self.lock:
            subscribers = list(self._subscribers.get(task_id, ()))
        for channel in subscribers:
            channel.put(deepcopy(persisted))
        return persisted

    def _apply_event(self, task_id: str, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            slash_commands = [
                name for name in event.get("slash_commands", [])
                if name and not str(name).startswith("__")
            ]
            with self.lock:
                for name in slash_commands:
                    if name not in self._slash_names:
                        self._slash_names.append(name)
            capability_snapshot = {
                "model": event.get("model"),
                "version": event.get("claude_code_version"),
                "mcp_servers": event.get("mcp_servers", []),
                "skills": event.get("skills", []),
                "agents": event.get("agents", []),
                "plugins": event.get("plugins", []),
                "slash_count": len(slash_commands),
                "updated_at": time.time(),
            }
            self.capabilities.replace(capability_snapshot)

            def update(task: dict[str, Any]) -> None:
                task["session_id"] = event.get("session_id") or task.get("session_id")
                task["model"] = event.get("model") or task.get("model")
                task["updated_at"] = time.time()
                self._persist_seq(task)
            self.tasks.mutate(task_id, update)
        elif event_type == "result":
            def update(task: dict[str, Any]) -> None:
                task["status"] = "error" if event.get("is_error") else "idle"
                task["last_cost_usd"] = event.get("total_cost_usd")
                task["updated_at"] = time.time()
                self._persist_seq(task)
            self.tasks.mutate(task_id, update)

    # ---------- lifecycle ----------
    def create_task(
        self,
        *,
        title: str,
        project: str,
        cwd: Path | str,
        model: str | None,
        permission_mode: str | None,
        prompt: str,
        add_dirs: list[str] | None = None,
        agent_name: str | None = None,
        agent_avatar: str | None = None,
    ) -> dict[str, Any]:
        normalized_cwd = Path(cwd).resolve()
        if not normalized_cwd.is_dir():
            raise ValueError(f"项目目录不存在: {normalized_cwd}")
        normalized_dirs = self._normalize_dirs(add_dirs or [])
        now = time.time()
        task = {
            "id": uuid.uuid4().hex[:12],
            "title": title or (prompt[:16] + ("…" if len(prompt) > 16 else "")),
            "project": project,
            "cwd": str(normalized_cwd),
            "model": model,
            "permission_mode": permission_mode,
            "add_dirs": normalized_dirs,
            "agent_name": agent_name,
            "agent_avatar": agent_avatar,
            "status": "running",
            "session_id": None,
            "pinned": False,
            "event_seq": 0,
            "created_at": now,
            "updated_at": now,
        }
        self.tasks.put(task)
        with self.lock:
            self._event_seq[task["id"]] = 0
        try:
            handle = self._spawn(task, resume=False)
        except Exception:
            self.tasks.mutate(task["id"], lambda item: item.update(status="error", updated_at=time.time()))
            raise
        self.emit(task["id"], {"type": "x-user", "text": prompt, "ts": now})
        try:
            handle.send_user_message(prompt)
        except Exception as exc:
            self.emit(task["id"], {"type": "x-stderr", "text": f"写入失败: {exc}"})
            self.tasks.mutate(task["id"], lambda item: item.update(status="error", updated_at=time.time()))
            raise
        return self.get_task(task["id"])

    def send_message(self, task_id: str, text: str,
                     images: list[dict] | None = None) -> dict[str, Any]:
        images = images or []
        with self.lock:
            task = self.get_task(task_id)
            handle = self._handles.get(task_id)
            if handle is None or not handle.is_alive():
                handle = self._spawn(task, resume=True)
            self._cancelled.discard(task_id)
        self.tasks.mutate(
            task_id,
            lambda item: item.update(status="running", updated_at=time.time()),
        )
        self.emit(task_id, {"type": "x-user", "text": text, "ts": time.time(), "images": images})
        try:
            handle.send_user_message(text, images)
        except (BrokenPipeError, OSError) as exc:
            self.emit(task_id, {"type": "x-stderr", "text": f"写入失败: {exc}"})
            self.tasks.mutate(task_id, lambda item: item.update(status="error", updated_at=time.time()))
        return self.get_task(task_id)

    def interrupt(self, task_id: str) -> dict[str, Any]:
        self.get_task(task_id)
        with self.lock:
            handle = self._handles.get(task_id)
            self._cancelled.add(task_id)
        self.tasks.mutate(
            task_id,
            lambda item: item.update(status="stopping", updated_at=time.time()),
        )
        if handle is not None and handle.is_alive():
            handle.terminate()
        else:
            self.tasks.mutate(task_id, lambda item: item.update(status="idle", updated_at=time.time()))
        return self.get_task(task_id)

    def set_pinned(self, task_id: str, pinned: bool) -> dict[str, Any]:
        return self.tasks.mutate(task_id, lambda item: item.update(pinned=bool(pinned)))

    def delete_task(self, task_id: str) -> None:
        self.get_task(task_id)
        with self.lock:
            handle = self._handles.pop(task_id, None)
            self._cancelled.add(task_id)
            self._subscribers.pop(task_id, None)
            self._perm_session_allow.pop(task_id, None)
            for rid in [r for r, p in self._perm_pending.items() if p.get("task_id") == task_id]:
                self._perm_pending.pop(rid, None)
        if handle is not None and handle.is_alive():
            handle.terminate()
        self.tasks.remove(task_id)
        with self.lock:
            self._event_seq.pop(task_id, None)
        self.events.delete(task_id)

    def add_dir(self, task_id: str, path: str) -> dict[str, Any]:
        directory = Path(os.path.expanduser(path)).resolve()
        if not directory.is_dir():
            raise ValueError(f"目录不存在: {directory}")

        def update(task: dict[str, Any]) -> None:
            dirs = task.setdefault("add_dirs", [])
            if str(directory) != task.get("cwd") and str(directory) not in dirs:
                dirs.append(str(directory))
        task = self.tasks.mutate(task_id, update)
        self._restart_session(task_id, f"已添加允许访问目录：{directory}（下条消息起生效）")
        return task

    def remove_dir(self, task_id: str, path: str) -> dict[str, Any]:
        def update(task: dict[str, Any]) -> None:
            dirs = task.setdefault("add_dirs", [])
            if path in dirs:
                dirs.remove(path)
        task = self.tasks.mutate(task_id, update)
        self._restart_session(task_id, f"已移除允许访问目录：{path}（下条消息起生效）")
        return task

    def set_permission_mode(self, task_id: str, mode: str) -> dict[str, Any]:
        # 権限モードは claude 起動時の --permission-mode フラグ。実行中プロセスは
        # 変更できないため、タスクに保存して(ディレクトリ変更と同じく)プロセスを落とし、
        # 次メッセージ送信時に --resume で新モードで再起動させる。
        valid = {"default", "plan", "acceptEdits", "bypassPermissions"}
        if mode not in valid:
            raise ValueError(f"未知的权限模式: {mode}")

        def update(task: dict[str, Any]) -> None:
            task["permission_mode"] = mode
        task = self.tasks.mutate(task_id, update)
        self._restart_session(task_id, f"已切换权限模式：{mode}（下条消息起生效）")
        return task

    # ---------- 权限 control protocol ----------
    def _on_control(self, task_id: str, ev: dict[str, Any]) -> None:
        """claude からの control_request を処理する。can_use_tool なら
        全自动=即 allow / 本会话已允许=即 allow / それ以外は UI に問い合わせる。"""
        request_id = ev.get("request_id")
        req = ev.get("request") or {}
        if req.get("subtype") != "can_use_tool":
            # 未知の control_request は空 success を返して claude を待たせない。
            self._send_control(task_id, request_id, {})
            return
        tool_name = str(req.get("tool_name") or "?")
        tool_input = req.get("input") or {}
        task = self.tasks.get(task_id) or {}
        with self.lock:
            session_ok = tool_name in self._perm_session_allow.get(task_id, set())
        if task.get("permission_mode") == "bypassPermissions" or session_ok:
            self._respond_permission(task_id, request_id, tool_input, "allow")
            return
        # UI に確認カードを出し、応答は resolve_permission で返す。
        with self.lock:
            self._perm_pending[str(request_id)] = {
                "task_id": task_id, "tool_name": tool_name, "input": tool_input,
            }
        self.emit(task_id, {
            "type": "x-perm-req", "request_id": request_id,
            "tool_name": tool_name, "preview": self._perm_preview(tool_name, tool_input),
        })

    def resolve_permission(self, task_id: str, request_id: str,
                           behavior: str, scope: str = "once") -> dict[str, Any]:
        with self.lock:
            pending = self._perm_pending.pop(request_id, None)
            if pending is not None and behavior == "allow" and scope == "session":
                self._perm_session_allow.setdefault(task_id, set()).add(pending.get("tool_name"))
        if pending is None:
            return {"ok": False, "error": "该权限请求已失效"}
        self._respond_permission(task_id, request_id, pending.get("input") or {}, behavior)
        self.emit(task_id, {"type": "x-perm-resolved", "request_id": request_id, "behavior": behavior})
        return {"ok": True}

    def _respond_permission(self, task_id: str, request_id: Any,
                            tool_input: dict[str, Any], behavior: str) -> None:
        if behavior == "allow":
            response = {"behavior": "allow", "updatedInput": tool_input}
        else:
            response = {"behavior": "deny", "message": "用户拒绝了此操作"}
        self._send_control(task_id, request_id, response)

    def _send_control(self, task_id: str, request_id: Any, response: dict[str, Any]) -> None:
        with self.lock:
            handle = self._handles.get(task_id)
        if handle is not None and handle.is_alive():
            try:
                handle.send_control_response(str(request_id), response)
            except Exception:
                pass

    @staticmethod
    def _perm_preview(tool_name: str, tool_input: dict[str, Any]) -> str:
        if tool_name in ("Bash", "PowerShell"):
            return str(tool_input.get("command") or "")[:400]
        if tool_name in ("Write", "Edit", "NotebookEdit", "Read"):
            return str(tool_input.get("file_path") or "")
        try:
            return json.dumps(tool_input, ensure_ascii=False)[:300]
        except Exception:
            return ""

    def set_model(self, task_id: str, model: str) -> dict[str, Any]:
        """切换这个会话用的模型。CLI 的 --model 是启动时定死的，所以先杀掉当前子进程，
        下一条消息会带 --resume 用新模型接上同一个会话，聊天记录不丢。"""
        model = (model or "").strip()
        if not model:
            raise ValueError("模型不能为空")
        task = self.get_task(task_id)
        if task.get("model") == model:
            return task
        task = self.tasks.mutate(task_id, lambda item: item.update(model=model))
        self._restart_session(task_id, f"模型已切换为 {model}（下条消息起生效，会话继续）")
        return task

    def _restart_session(self, task_id: str, note: str) -> None:
        with self.lock:
            # pop で確実に取り除く。get のままだと terminate 後もハンドルが残り、
            # プロセスがまだ生きていると次の送信で旧設定(旧権限モード)のまま再利用され、
            # 権限切替が効かない。pop すれば次の送信は必ず新設定で spawn し直す。
            handle = self._handles.pop(task_id, None)
            self._cancelled.add(task_id)
        self.tasks.mutate(task_id, lambda item: item.update(status="idle", updated_at=time.time()))
        if handle is not None and handle.is_alive():
            handle.terminate()
        self.emit(task_id, {"type": "x-sys", "text": note})

    def shutdown(self) -> None:
        """Terminate all live CLI children during application shutdown."""
        with self.lock:
            handles = list(self._handles.items())
            self._cancelled.update(task_id for task_id, _ in handles)
        for _, handle in handles:
            if handle.is_alive():
                handle.terminate()

    def _normalize_dirs(self, directories: list[str]) -> list[str]:
        result = []
        for raw in directories:
            directory = Path(os.path.expanduser(raw)).resolve()
            if not directory.is_dir():
                raise ValueError(f"目录不存在: {directory}")
            value = str(directory)
            if value not in result:
                result.append(value)
        return result

    def _spawn(self, task: dict[str, Any], *, resume: bool) -> SessionHandle:
        task_id = task["id"]
        spec = CliLaunchSpec(
            task_id=task_id,
            cwd=Path(task["cwd"]),
            model=task.get("model"),
            permission_mode=task.get("permission_mode"),
            add_dirs=tuple(task.get("add_dirs", [])),
            session_id=task.get("session_id"),
            resume=resume,
        )
        handle = self.cli.start(
            spec,
            on_event=lambda event: self._on_cli_event(task_id, event),
            on_error=lambda text: self._on_cli_error(task_id, text),
            on_exit=lambda code: self._on_cli_exit(task_id, code),
            on_control=lambda ev: self._on_control(task_id, ev),
        )
        with self.lock:
            self._handles[task_id] = handle
        return handle

    def _on_cli_event(self, task_id: str, event: dict[str, Any]) -> None:
        if self.tasks.get(task_id) is not None:
            self.emit(task_id, event)

    def _on_cli_error(self, task_id: str, text: str) -> None:
        if self.tasks.get(task_id) is not None:
            self.emit(task_id, {"type": "x-stderr", "text": text})

    def _on_cli_exit(self, task_id: str, code: int) -> None:
        if self.tasks.get(task_id) is None:
            return
        with self.lock:
            self._handles.pop(task_id, None)
            cancelled = task_id in self._cancelled
            self._cancelled.discard(task_id)
        current = self.get_task(task_id)
        if cancelled:
            def mark_cancelled(task: dict[str, Any]) -> None:
                task["status"] = "idle"
                task["updated_at"] = time.time()
                self._persist_seq(task)
            self.tasks.mutate(task_id, mark_cancelled)
            self.emit(task_id, {"type": "x-proc-exit", "code": code, "cancelled": True})
            return

        def mark_exited(task: dict[str, Any]) -> None:
            if current.get("status") == "running":
                task["status"] = "error" if code else "idle"
            task["updated_at"] = time.time()
            self._persist_seq(task)
        self.tasks.mutate(task_id, mark_exited)
        self.emit(task_id, {"type": "x-proc-exit", "code": code, "cancelled": False})
