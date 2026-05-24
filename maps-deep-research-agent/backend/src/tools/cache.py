"""Two-level cache used by Maps tools to avoid redundant paid API calls.

* In-memory LRU (hot cache, per process)
* DiskCache (cold cache, survives restarts)

Keys are derived from `(tool_name, args_dict)`. Maps API responses are
idempotent for short windows, so caching is generally safe; the TTL is
configurable via ``CACHE_TTL_SECONDS``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from diskcache import Cache


class MapsCache:
    def __init__(self, directory: Path, ttl_seconds: int, lru_size: int = 256) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._disk = Cache(str(directory))
        self._ttl = ttl_seconds
        self._lru: OrderedDict[str, Any] = OrderedDict()
        self._lru_size = lru_size
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(tool: str, args: dict[str, Any]) -> str:
        payload = json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, tool: str, args: dict[str, Any]) -> Any | None:
        key = self._key(tool, args)
        with self._lock:
            if key in self._lru:
                self.hits += 1
                self._lru.move_to_end(key)
                return self._lru[key]

        value = self._disk.get(key)
        if value is not None:
            with self._lock:
                self._lru[key] = value
                self._lru.move_to_end(key)
                if len(self._lru) > self._lru_size:
                    self._lru.popitem(last=False)
                self.hits += 1
            return value

        with self._lock:
            self.misses += 1
        return None

    def set(self, tool: str, args: dict[str, Any], value: Any) -> None:
        key = self._key(tool, args)
        if self._ttl > 0:
            self._disk.set(key, value, expire=self._ttl)
        else:
            self._disk.set(key, value)
        with self._lock:
            self._lru[key] = value
            self._lru.move_to_end(key)
            if len(self._lru) > self._lru_size:
                self._lru.popitem(last=False)
