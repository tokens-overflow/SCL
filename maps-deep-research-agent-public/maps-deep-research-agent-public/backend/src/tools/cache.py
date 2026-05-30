"""Maps 工具两级缓存（内存 LRU + sqlite 持久化）。

为什么要两级
------------
* **进程内 LRU**：同进程同请求内的重复调用直接命中，零序列化开销。
* **sqlite 落盘**：跨重启保留，避免重新爬同样的 query 多花 Maps 配额。

缓存键 = ``sha256(JSON({tool, args}))``。Maps API 在短时间窗口内是幂等的，
缓存安全；TTL 由 ``app.cache.ttl_seconds`` 控制。
"""

from __future__ import annotations

import hashlib
import json
import pickle
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 落盘层（stdlib-only：sqlite3 + pickle）
# ---------------------------------------------------------------------------
class _DiskKV:
    """sqlite3 实现的 KV 存储，每条记录带 TTL。

    ``ttl_seconds <= 0`` 表示不过期。value 用 pickle 序列化。
    """

    def __init__(self, directory: Path, ttl_seconds: int) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        # check_same_thread=False：允许在多个事件循环线程间共享同一 connection
        # isolation_level=None：autocommit，减少锁竞争
        self._conn = sqlite3.connect(
            str(directory / "cache.db"),
            check_same_thread=False,
            isolation_level=None,
        )
        # WAL 模式 = 读不阻塞写、写不阻塞读
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            "  k TEXT PRIMARY KEY,"
            "  v BLOB NOT NULL,"
            "  exp REAL NOT NULL DEFAULT 0"  # 0 表示不过期
            ")"
        )

    def get(self, key: str) -> Any | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT v, exp FROM kv WHERE k = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        blob, exp = row
        # 过期：删除并视作未命中
        if exp and exp <= time.time():
            with self._lock:
                self._conn.execute("DELETE FROM kv WHERE k = ?", (key,))
            return None
        return pickle.loads(blob)

    def set(self, key: str, value: Any) -> None:
        exp = (time.time() + self._ttl) if self._ttl > 0 else 0.0
        blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv (k, v, exp) VALUES (?, ?, ?)",
                (key, blob, exp),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# 两级缓存
# ---------------------------------------------------------------------------
class MapsCache:
    """LRU + sqlite 两级缓存。对外只暴露 get / set / hits / misses。"""

    def __init__(self, directory: Path, ttl_seconds: int, lru_size: int = 256) -> None:
        self._disk = _DiskKV(directory, ttl_seconds)
        self._lru: OrderedDict[str, Any] = OrderedDict()
        self._lru_size = lru_size
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(tool: str, args: dict[str, Any]) -> str:
        """同样的 (tool, args) 永远算出同样的 key（JSON 排序 + sha256）。"""
        payload = json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, tool: str, args: dict[str, Any]) -> Any | None:
        key = self._key(tool, args)
        # 先看进程内 LRU
        with self._lock:
            if key in self._lru:
                self.hits += 1
                self._lru.move_to_end(key)
                return self._lru[key]

        # 再看落盘层
        value = self._disk.get(key)
        if value is not None:
            with self._lock:
                self._lru[key] = value
                self._lru.move_to_end(key)
                if len(self._lru) > self._lru_size:
                    self._lru.popitem(last=False)  # 淘汰最旧
                self.hits += 1
            return value

        with self._lock:
            self.misses += 1
        return None

    def set(self, tool: str, args: dict[str, Any], value: Any) -> None:
        key = self._key(tool, args)
        self._disk.set(key, value)
        with self._lock:
            self._lru[key] = value
            self._lru.move_to_end(key)
            if len(self._lru) > self._lru_size:
                self._lru.popitem(last=False)
