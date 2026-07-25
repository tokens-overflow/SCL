"""Persistent local scheduler for Claude Code tasks."""
from __future__ import annotations

import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .stores import ConfigStore, JsonStore
from .task_service import TaskService


class Scheduler:
    def __init__(
        self,
        task_service: TaskService,
        config: ConfigStore,
        data_dir: Path,
        *,
        poll_seconds: float = 15.0,
        autostart: bool = True,
    ):
        self.task_service = task_service
        self.config = config
        self.document = JsonStore(data_dir / "schedules.json", list)
        self.poll_seconds = poll_seconds
        self.lock = threading.RLock()
        self._items: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        for item in self.document.load():
            if isinstance(item, dict) and item.get("id"):
                self._items[item["id"]] = dict(item)
        self._normalize_loaded_items()
        if autostart:
            threading.Thread(target=self._loop, daemon=True, name="claude-scheduler").start()

    def _normalize_loaded_items(self) -> None:
        changed = False
        with self.lock:
            for item in self._items.values():
                if item.get("enabled") and not item.get("next_run"):
                    item["next_run"] = self.compute_next(item)
                    changed = True
            if changed:
                self._save_locked()

    def _save_locked(self) -> None:
        items = sorted(self._items.values(), key=lambda item: item.get("created_at", 0))
        self.document.save(items)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return deepcopy(sorted(
                self._items.values(), key=lambda item: item.get("created_at", 0), reverse=True
            ))

    @staticmethod
    def compute_next(item: dict[str, Any], after: float | None = None) -> float | None:
        """下一次触发的 epoch 秒。支持三种类型：
        - interval：每 N 分钟。以 last_run(或 created_at) 为基准 +N 分钟；若已过期则
          按整段跳到未来最近一次（避免程序停机后一次性补跑很多次）。
        - daily：每天 HH:MM。取今天该时刻，若已过则顺延到明天。
        - once：一次性，直接用 at_datetime；无值则返回 None(不再触发)。
        `after` 用于以某个时间点为基准重算(测试/补算用)。返回 None 表示不安排。
        """
        now = after if after is not None else time.time()
        schedule_type = item.get("sched_type")
        if schedule_type == "interval":
            minutes = max(1, int(item.get("interval_min") or 60))
            base = float(item.get("last_run") or item.get("created_at") or now)
            next_run = base + minutes * 60
            if next_run <= now:
                elapsed = now - base
                jumps = int(elapsed // (minutes * 60)) + 1
                next_run = base + jumps * minutes * 60
            return next_run
        if schedule_type == "daily":
            raw = str(item.get("at_time") or "09:00")
            try:
                hour, minute = (int(part) for part in raw.split(":", 1))
            except (TypeError, ValueError):
                hour, minute = 9, 0
            current = datetime.fromtimestamp(now)
            candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate.timestamp() <= now:
                candidate += timedelta(days=1)
            return candidate.timestamp()
        if schedule_type == "once":
            value = item.get("at_datetime")
            return float(value) if value is not None else None
        return None

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        item = {
            "id": uuid.uuid4().hex[:12],
            "title": str(data.get("title") or "").strip(),
            "project": str(data.get("project") or ""),
            "model": str(data.get("model") or self.config.snapshot()["default_model"]),
            "permission_mode": str(
                data.get("permission_mode") or self.config.snapshot()["default_permission_mode"]
            ),
            "prompt": str(data.get("prompt") or "").strip(),
            "sched_type": str(data.get("sched_type") or "interval"),
            "interval_min": max(1, int(data.get("interval_min") or 60)),
            "at_time": str(data.get("at_time") or "09:00"),
            "at_datetime": data.get("at_datetime"),
            "enabled": True,
            "created_at": now,
            "last_run": None,
            "last_task_id": None,
            "last_error": None,
        }
        item["next_run"] = self.compute_next(item)
        with self.lock:
            self._items[item["id"]] = item
            self._save_locked()
        return deepcopy(item)

    def toggle(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        with self.lock:
            item = self._items.get(schedule_id)
            if item is None:
                raise KeyError(schedule_id)
            item["enabled"] = bool(enabled)
            if item["enabled"]:
                item["next_run"] = self.compute_next(item)
            else:
                item["next_run"] = None
            self._save_locked()
            return deepcopy(item)

    def delete(self, schedule_id: str) -> None:
        with self.lock:
            if schedule_id not in self._items:
                raise KeyError(schedule_id)
            self._items.pop(schedule_id)
            self._save_locked()

    def run_now(self, schedule_id: str) -> dict[str, Any] | None:
        with self.lock:
            item = self._items.get(schedule_id)
            if item is None:
                raise KeyError(schedule_id)
            snapshot = deepcopy(item)
        return self._fire(snapshot)

    def _fire(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        schedule_id = snapshot["id"]
        try:
            cwd = self.config.resolve_project(snapshot.get("project"), fallback_to_cwd=False)
            task = self.task_service.create_task(
                title="⏰ " + (snapshot.get("title") or snapshot.get("prompt", "")[:16]),
                project=snapshot.get("project") or "(定时)",
                cwd=cwd,
                model=snapshot.get("model"),
                permission_mode=snapshot.get("permission_mode"),
                prompt=snapshot.get("prompt", ""),
                agent_name="定时任务",
                agent_avatar="📅",   # 定时任务的回复用小日历图标
            )
            error = None
        except Exception as exc:
            task = None
            error = str(exc)

        with self.lock:
            item = self._items.get(schedule_id)
            if item is None:
                return task
            item["last_run"] = time.time()
            item["last_task_id"] = task.get("id") if task else None
            item["last_error"] = error
            if item.get("sched_type") == "once":
                item["enabled"] = False
                item["next_run"] = None
            else:
                item["next_run"] = self.compute_next(item, after=item["last_run"])
            self._save_locked()
        return task

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            now = time.time()
            due: list[dict[str, Any]] = []
            with self.lock:
                for item in self._items.values():
                    if item.get("enabled") and item.get("next_run") and item["next_run"] <= now:
                        due.append(deepcopy(item))
            for snapshot in due:
                self._fire(snapshot)

    def stop(self) -> None:
        self._stop.set()
