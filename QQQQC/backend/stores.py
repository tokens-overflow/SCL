"""Persistence and filesystem adapters for QQQQC.

All JSON writes are atomic and all mutable stores own their own lock.  The
service layer depends on these small adapters instead of touching files
inline, which keeps process management, scheduling and HTTP transport
independent from persistence details.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterable

DEFAULT_CONFIG: dict[str, Any] = {
    "user_name": "我",
    "default_model": "sonnet",
    "default_permission_mode": "acceptEdits",
    "models": ["sonnet", "opus", "haiku"],
    "projects": [{"name": "当前目录", "path": ".", "pinned": True}],
    "friends": [
        {
            "name": "Claude 小蓝",
            "avatar": "🤖",
            "sign": "随时待命～",
            "persona": "你是 Claude 小蓝，一个可靠、热心的编程搭子，说话简洁、带点亲切的语气。",
        },
        {
            "name": "小美",
            "avatar": "👧",
            "sign": "谁帮我修下CSS",
            "persona": "你是小美，一个毒舌但技术很强的前端妹子，说话直接、爱吐槽，但每次都能把问题真的解决掉。",
        },
        {
            "name": "老王",
            "avatar": "🧔",
            "sign": "有 Bug 找我",
            "persona": "你是老王，一个经验丰富、稳重的后端老工程师，讲话像带徒弟，先讲原理再给方案。",
        },
    ],
}


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* atomically, preserving the previous file on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return deepcopy(default)


class JsonStore:
    """Thread-safe JSON document store with atomic replacement writes."""

    def __init__(self, path: Path, default_factory: Callable[[], Any]):
        self.path = path
        self.default_factory = default_factory
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Any:
        with self.lock:
            return read_json(self.path, self.default_factory())

    def save(self, value: Any) -> None:
        with self.lock:
            atomic_write_json(self.path, value)


class ConfigStore:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir.resolve()
        self.path = self.base_dir / "config.json"
        self._config = self._load()

    def _load(self) -> dict[str, Any]:
        merged = deepcopy(DEFAULT_CONFIG)
        user_config = read_json(self.path, {})
        if isinstance(user_config, dict):
            merged.update(user_config)

        projects = [dict(item) for item in merged.get("projects", []) if isinstance(item, dict)]
        if not any(item.get("path") == "." for item in projects):
            projects.append({"name": "当前目录", "path": ".", "pinned": False})
        for project in projects:
            configured = str(project.get("path") or ".")
            expanded = Path(os.path.expanduser(configured))
            if not expanded.is_absolute():
                expanded = self.base_dir / expanded
            absolute = expanded.resolve()
            project["abspath"] = str(absolute)
            project["exists"] = absolute.is_dir()
        projects.sort(key=lambda item: (not item.get("exists", False), not item.get("pinned", False)))
        merged["projects"] = projects
        return merged

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self._config)

    def resolve_project(self, name: str | None, *, fallback_to_cwd: bool = True) -> Path:
        project = next((p for p in self._config["projects"] if p.get("name") == name), None)
        if project and project.get("exists"):
            return Path(project["abspath"])
        if fallback_to_cwd:
            return self.base_dir
        candidate = Path(project["abspath"]) if project else self.base_dir
        raise FileNotFoundError(f"项目目录不存在: {candidate}")


class TaskStore:
    def __init__(self, data_dir: Path):
        self.document = JsonStore(data_dir / "tasks.json", list)
        self.lock = threading.RLock()
        self._tasks: dict[str, dict[str, Any]] = {}
        for task in self.document.load():
            if not isinstance(task, dict) or not task.get("id"):
                continue
            task = dict(task)
            if task.get("status") in {"running", "stopping"}:
                task["status"] = "idle"
            task.setdefault("event_seq", 0)
            self._tasks[task["id"]] = task

    def _save_locked(self) -> None:
        items = sorted(self._tasks.values(), key=lambda item: item.get("created_at", 0))
        self.document.save(items)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [deepcopy(item) for item in sorted(
                self._tasks.values(), key=lambda item: item.get("created_at", 0), reverse=True
            )]

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            item = self._tasks.get(task_id)
            return deepcopy(item) if item else None

    def get_mutable(self, task_id: str) -> dict[str, Any] | None:
        """Internal use only; caller must hold ``lock``."""
        return self._tasks.get(task_id)

    def put(self, task: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._tasks[task["id"]] = task
            self._save_locked()
            return deepcopy(task)

    def mutate(self, task_id: str, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self.lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            mutator(task)
            self._save_locked()
            return deepcopy(task)

    def remove(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            task = self._tasks.pop(task_id, None)
            self._save_locked()
            return deepcopy(task) if task else None

    def save(self) -> None:
        with self.lock:
            self._save_locked()


class EventStore:
    def __init__(self, data_dir: Path):
        self.directory = data_dir / "events"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def path_for(self, task_id: str) -> Path:
        return self.directory / f"{task_id}.jsonl"

    def append(self, task_id: str, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with self.lock:
            with self.path_for(task_id).open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()

    def replay_after(self, task_id: str, last_seq: int = 0) -> list[dict[str, Any]]:
        path = self.path_for(task_id)
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        with self.lock:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(event.get("_seq") or 0) > last_seq:
                events.append(event)
        return events

    def max_seq(self, task_id: str) -> int:
        events = self.replay_after(task_id, 0)
        return max((int(event.get("_seq") or 0) for event in events), default=0)

    def delete(self, task_id: str) -> None:
        with self.lock:
            self.path_for(task_id).unlink(missing_ok=True)


class CapabilityStore:
    def __init__(self, data_dir: Path):
        self.document = JsonStore(data_dir / "capabilities.json", dict)
        self.lock = threading.RLock()
        self._value = self.document.load()

    def get(self) -> dict[str, Any]:
        with self.lock:
            return deepcopy(self._value)

    def replace(self, value: dict[str, Any]) -> None:
        with self.lock:
            self._value = deepcopy(value)
            self.document.save(self._value)


class FriendStore:
    def __init__(self, data_dir: Path, seeds: Iterable[dict[str, Any]]):
        self.document = JsonStore(data_dir / "friends.json", list)
        self.lock = threading.RLock()
        loaded = self.document.load()
        if loaded:
            self._friends = [dict(item) for item in loaded if isinstance(item, dict)]
        else:
            self._friends = [{**item, "id": uuid.uuid4().hex[:8]} for item in seeds]
            self.document.save(self._friends)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return deepcopy(self._friends)

    def add(self, data: dict[str, Any]) -> dict[str, Any]:
        friend = {
            "id": uuid.uuid4().hex[:8],
            "name": str(data.get("name") or "").strip() or "新朋友",
            "avatar": str(data.get("avatar") or "🙂").strip()[:64] or "🙂",
            "sign": str(data.get("sign") or "").strip(),
            "persona": str(data.get("persona") or "").strip(),
            "project": str(data.get("project") or ""),
            "model": str(data.get("model") or ""),
        }
        with self.lock:
            self._friends.append(friend)
            self.document.save(self._friends)
        return deepcopy(friend)

    def update(self, friend_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        fields = ("name", "avatar", "sign", "persona", "project", "model")
        with self.lock:
            for item in self._friends:
                if item.get("id") == friend_id:
                    for key in fields:
                        if key in data:
                            value = str(data.get(key) or "").strip()
                            if key == "avatar":
                                value = value[:64] or item.get("avatar") or "🙂"
                            elif key == "name":
                                value = value or item.get("name") or "新朋友"
                            item[key] = value
                    self.document.save(self._friends)
                    return deepcopy(item)
        return None

    def delete(self, friend_id: str) -> None:
        with self.lock:
            self._friends = [item for item in self._friends if item.get("id") != friend_id]
            self.document.save(self._friends)


class ProfileStore:
    def __init__(self, data_dir: Path, default_name: str):
        self.document = JsonStore(data_dir / "profile.json", dict)
        self.lock = threading.RLock()
        value = self.document.load()
        self._profile = value or {"name": default_name or "我", "avatar": "qq1"}
        if not value:
            self.document.save(self._profile)

    def get(self) -> dict[str, Any]:
        with self.lock:
            return deepcopy(self._profile)

    def update(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._profile["name"] = str(data.get("name") or "").strip() or self._profile.get("name") or "我"
            self._profile["avatar"] = str(data.get("avatar") or "").strip()[:64] or self._profile.get("avatar") or "qq1"
            self.document.save(self._profile)
            return deepcopy(self._profile)


class MomentStore:
    def __init__(self, data_dir: Path, friends: FriendStore, profile: ProfileStore):
        self.document = JsonStore(data_dir / "moments.json", list)
        self.friends = friends
        self.profile = profile
        self.lock = threading.RLock()
        loaded = self.document.load()
        if loaded:
            self._moments = [dict(item) for item in loaded if isinstance(item, dict)]
        else:
            now = time.time()
            self._moments = [
                {
                    "id": uuid.uuid4().hex[:8],
                    "author_name": friend.get("name"),
                    "author_avatar": friend.get("avatar", "🙂"),
                    "text": friend.get("sign") or "今天也在线，随时找我～",
                    "ts": now - (index + 1) * 3600,
                    "likes": (index * 3) % 7,
                    "mine": False,
                }
                for index, friend in enumerate(friends.list())
            ]
            self.document.save(self._moments)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return deepcopy(sorted(self._moments, key=lambda item: item.get("ts", 0), reverse=True))

    def add_mine(self, text: str) -> dict[str, Any]:
        me = self.profile.get()
        moment = {
            "id": uuid.uuid4().hex[:8],
            "author_name": me.get("name", "我"),
            "author_avatar": me.get("avatar", "qq1"),
            "text": text.strip(),
            "ts": time.time(),
            "likes": 0,
            "mine": True,
        }
        with self.lock:
            self._moments.append(moment)
            self.document.save(self._moments)
        return deepcopy(moment)

    def like(self, moment_id: str) -> dict[str, Any]:
        with self.lock:
            for moment in self._moments:
                if moment.get("id") == moment_id:
                    moment["likes"] = int(moment.get("likes") or 0) + 1
                    self.document.save(self._moments)
                    return deepcopy(moment)
        raise KeyError(moment_id)

    def delete(self, moment_id: str) -> None:
        with self.lock:
            self._moments = [item for item in self._moments if item.get("id") != moment_id]
            self.document.save(self._moments)


class SkillStore:
    FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)

    def __init__(self, skills_dir: Path | None = None):
        self.directory = (skills_dir or Path(os.path.expanduser("~/.claude/skills"))).resolve()
        self.lock = threading.RLock()

    @staticmethod
    def _slug(name: str) -> str:
        slug = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
        return slug or "skill"

    @classmethod
    def _parse(cls, markdown: str) -> tuple[dict[str, str], str]:
        match = cls.FRONT_MATTER.match(markdown)
        if not match:
            return {}, markdown
        metadata: dict[str, str] = {}
        for line in match.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip()
        return metadata, match.group(2).lstrip("\n")

    @staticmethod
    def _render(name: str, description: str, body: str) -> str:
        return f"---\nname: {name}\ndescription: {description}\n---\n\n{body.rstrip()}\n"

    def _safe_dir(self, dirname: str) -> Path:
        if not dirname or dirname in {".", ".."} or "/" in dirname or "\\" in dirname:
            raise ValueError("非法 skill 目录")
        path = (self.directory / dirname).resolve()
        if path.parent != self.directory:
            raise ValueError("非法 skill 目录")
        return path

    def list(self) -> list[dict[str, str]]:
        if not self.directory.is_dir():
            return []
        result = []
        with self.lock:
            for directory in sorted(self.directory.iterdir()):
                skill_file = directory / "SKILL.md"
                if not directory.is_dir() or not skill_file.is_file():
                    continue
                try:
                    metadata, _ = self._parse(skill_file.read_text(encoding="utf-8"))
                except OSError:
                    metadata = {}
                result.append({
                    "dir": directory.name,
                    "name": metadata.get("name", directory.name),
                    "description": metadata.get("description", ""),
                })
        return result

    def read(self, dirname: str) -> dict[str, str] | None:
        directory = self._safe_dir(dirname)
        skill_file = directory / "SKILL.md"
        if not skill_file.is_file():
            return None
        metadata, body = self._parse(skill_file.read_text(encoding="utf-8"))
        return {
            "dir": dirname,
            "name": metadata.get("name", dirname),
            "description": metadata.get("description", ""),
            "body": body,
        }

    def create(self, name: str, description: str, body: str) -> dict[str, str]:
        slug = self._slug(name)
        directory = self._safe_dir(slug)
        with self.lock:
            if directory.exists():
                raise ValueError(f"已存在同名 skill: {slug}")
            directory.mkdir(parents=True)
            atomic_write_text(directory / "SKILL.md", self._render(slug, description, body))
        return self.read(slug) or {}

    def save(self, dirname: str, description: str, body: str) -> dict[str, str]:
        directory = self._safe_dir(dirname)
        skill_file = directory / "SKILL.md"
        if not skill_file.is_file():
            raise KeyError(dirname)
        with self.lock:
            metadata, _ = self._parse(skill_file.read_text(encoding="utf-8"))
            atomic_write_text(
                skill_file,
                self._render(metadata.get("name", dirname), description, body),
            )
        return self.read(dirname) or {}

    def delete(self, dirname: str) -> None:
        directory = self._safe_dir(dirname)
        with self.lock:
            if directory.is_dir():
                shutil.rmtree(directory)


class ClaudeMdStore:
    def __init__(self, config: ConfigStore):
        self.config = config

    def read(self, project: str) -> dict[str, Any]:
        cwd = self.config.resolve_project(project, fallback_to_cwd=True)
        path = cwd / "CLAUDE.md"
        return {
            "project": project,
            "path": str(path),
            "exists": path.is_file(),
            "content": path.read_text(encoding="utf-8") if path.is_file() else "",
        }

    def save(self, project: str, content: str) -> dict[str, Any]:
        cwd = self.config.resolve_project(project, fallback_to_cwd=False)
        path = cwd / "CLAUDE.md"
        atomic_write_text(path, content)
        return {"ok": True, "path": str(path)}
