"""Task/session application service.

A task is a durable Claude Code conversation.  This service owns task state,
process lifecycle and event fan-out; it does not know about HTTP routes or
scheduler persistence.
"""
from __future__ import annotations

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
        self._event_seq = {
            task["id"]: max(int(task.get("event_seq") or 0), self.events.max_seq(task["id"]))
            for task in self.tasks.list()
        }
        self._slash_names = list(SLASH_DESCRIPTIONS)

    def list_tasks(self) -> list[dict[str, Any]]:
        return self.tasks.list()

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def slash_commands(self) -> list[dict[str, str]]:
        with self.lock:
            return [{"name": name, "desc": SLASH_DESCRIPTIONS.get(name, "")} for name in self._slash_names]

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
            slash_commands = [name for name in event.get("slash_commands", []) if name and not str(name).startswith("__")]
            with self.lock:
                for name in slash_commands:
                    if name not in self._slash_names:
                        self._slash_names.append(name)
            self.capabilities.replace({
                "model": event.get("model"),
                "version": event.get("claude_code_version"),
                "mcp_servers": event.get("mcp_servers", []),
                "skills": event.get("skills", []),
                "agents": event.get("agents", []),
                "plugins": event.get("plugins", []),
                "slash_count": len(slash_commands),
                "updated_at": time.time(),
            })

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

    def create_task(self, *, title: str, project: str, cwd: Path | str, model: str | None,
                    permission_mode: str | None, prompt: str, add_dirs: list[str] | None = None,
                    agent_name: str | None = None, agent_avatar: str | None = None) -> dict[str, Any]:
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

    def send_message(self, task_id: str, text: str) -> dict[str, Any]:
        with self.lock:
            task = self.get_task(task_id)
            handle = self._handles.get(task_id)
            if handle is None or not handle.is_alive():
                handle = self._spawn(task, resume=True)
            self._cancelled.discard(task_id)
        self.tasks.mutate(task_id, lambda item: item.update(status="running", updated_at=time.time()))
        self.emit(task_id, {"type": "x-user", "text": text, "ts": time.time()})
        try:
            handle.send_user_message(text)
        except (BrokenPipeError, OSError) as exc:
            self.emit(task_id, {"type": "x-stderr", "text": f"写入失败: {exc}"})
            self.tasks.mutate(task_id, lambda item: item.update(status="error", updated_at=time.time()))
        return self.get_task(task_id)

    def interrupt(self, task_id: str) -> dict[str, Any]:
        self.get_task(task_id)
        with self.lock:
            handle = self._handles.get(task_id)
            self._cancelled.add(task_id)
        self.tasks.mutate(task_id, lambda item: item.update(status="stopping", updated_at=time.time()))
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
        self._restart_for_directory_change(task_id, f"已添加允许访问目录：{directory}（下条消息起生效）")
        return task

    def remove_dir(self, task_id: str, path: str) -> dict[str, Any]:
        def update(task: dict[str, Any]) -> None:
            dirs = task.setdefault("add_dirs", [])
            if path in dirs:
                dirs.remove(path)
        task = self.tasks.mutate(task_id, update)
        self._restart_for_directory_change(task_id, f"已移除允许访问目录：{path}（下条消息起生效）")
        return task

    def _restart_for_directory_change(self, task_id: str, note: str) -> None:
        with self.lock:
            handle = self._handles.get(task_id)
            self._cancelled.add(task_id)
        self.tasks.mutate(task_id, lambda item: item.update(status="idle", updated_at=time.time()))
        if handle is not None and handle.is_alive():
            handle.terminate()
        self.emit(task_id, {"type": "x-sys", "text": note})

    def shutdown(self) -> None:
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
        spec = CliLaunchSpec(task_id=task_id, cwd=Path(task["cwd"]), model=task.get("model"),
            permission_mode=task.get("permission_mode"), add_dirs=tuple(task.get("add_dirs", [])),
            session_id=task.get("session_id"), resume=resume)
        handle = self.cli.start(spec,
            on_event=lambda event: self._on_cli_event(task_id, event),
            on_error=lambda text: self._on_cli_error(task_id, text),
            on_exit=lambda code: self._on_cli_exit(task_id, code))
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
