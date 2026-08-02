"""记忆存储。

关键区分（这是「Context ≠ Memory」的落点）：

* **Context** 是这一次调用摊在桌面上的材料，用完就散。
* **Memory** 是跨任务、跨会话保留下来的长期信息。

两者必须分开的原因：如果把长期记忆直接等同于上下文，
上下文一断（进程重启、会话过期）记忆就没了；反过来，
如果把所有历史都塞进上下文，成本会随着交互轮次线性甚至超线性上涨。

另一条纪律：**存进 Memory 的是摘要，不是原始对话。**
原始对话既贵，又容易把上一次的错误结论当成事实带进新任务。
"""

from __future__ import annotations

from typing import Protocol

from app.context.models import MemoryItem
from app.core.ids import new_id, utcnow


class MemoryStore(Protocol):
    """记忆存储接口。

    Demo 用内存实现；生产可换 Redis / PostgreSQL / 向量库，接口不变。
    """

    async def remember(self, scope: str, key: str, content: str) -> MemoryItem:
        """写入一条记忆。"""
        ...

    async def recall(self, scope: str, key: str, limit: int = 5) -> list[MemoryItem]:
        """按作用域读取最近的记忆。"""
        ...

    async def forget(self, scope: str, key: str) -> int:
        """删除某个作用域下的全部记忆，返回删除条数。"""
        ...


class InMemoryMemoryStore:
    """进程内记忆存储（默认实现）。

    Warning:
        进程重启即丢失。**这是刻意的**：记忆是「锦上添花」，
        任务的可恢复性绝不能依赖它——断点续跑靠的是状态表，不是记忆。
        如果去掉记忆之后任务就无法恢复，说明状态设计有问题。
    """

    def __init__(self, max_per_scope: int = 50) -> None:
        self._data: dict[tuple[str, str], list[MemoryItem]] = {}
        self._max_per_scope = max_per_scope

    async def remember(self, scope: str, key: str, content: str) -> MemoryItem:
        """写入一条记忆摘要。

        Args:
            scope: 作用域，例如 ``"user"`` / ``"customer"``。
            key: 作用域内的键，例如 user_id 或 customer_id。
            content: 摘要内容（调用方负责保证已脱敏）。

        Returns:
            新写入的记忆条目。
        """
        item = MemoryItem(
            memory_id=new_id("mem"),
            scope=scope,
            content=content,
            created_at=utcnow(),
        )
        bucket = self._data.setdefault((scope, key), [])
        bucket.append(item)
        # 有界存储：无界增长的「记忆」最终会变成无界增长的成本。
        if len(bucket) > self._max_per_scope:
            del bucket[: len(bucket) - self._max_per_scope]
        return item

    async def recall(self, scope: str, key: str, limit: int = 5) -> list[MemoryItem]:
        """读取最近 ``limit`` 条记忆（按时间倒序）。"""
        bucket = self._data.get((scope, key), [])
        return list(reversed(bucket[-limit:]))

    async def forget(self, scope: str, key: str) -> int:
        """清空某作用域的记忆。

        这个方法存在是为了满足「被遗忘权」这类合规要求——
        个人信息相关的记忆必须可删除。
        """
        removed = self._data.pop((scope, key), [])
        return len(removed)


#: 默认记忆存储实例。
default_memory_store: MemoryStore = InMemoryMemoryStore()
