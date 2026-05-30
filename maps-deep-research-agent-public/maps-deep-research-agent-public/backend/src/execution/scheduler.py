"""通用 DAG 调度器（Kahn 风格 ready 队列 + 并发上限）。

为什么做成泛型？
----------------
调度器不需要知道节点到底是研究任务、构建步骤、还是别的什么。
只要每个节点暴露 ``id`` 和 ``depends_on`` 字段就够 —— 即 :class:`HasDependencies` 协议。

为什么要有并发上限？
--------------------
Maps / LLM 都有速率限制。``concurrency`` 限制同时跑的 ``run_one`` 数量；
其它已 ready 的节点排队等 semaphore 槽位。

死锁防护
--------
如果 DAG 声明有问题（比如 LLM 生成的 depends_on 引用了不存在的 id 又没被剔
干净），普通 Kahn 调度会死等。本调度器加了一个 stall 超时：N 秒没新节点变 ready
就强制把剩下所有 pending 节点解锁。牺牲一点正确性换 liveness。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Generic, Protocol, TypeVar, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class HasDependencies(Protocol):
    """所有调度节点必须实现的最小接口。"""

    id: int
    depends_on: list[int]


N = TypeVar("N", bound=HasDependencies)


class DAGScheduler(Generic[N]):
    """按依赖关系驱动一组节点跑完。

    调度器自身是**被动**的：它只决定哪个节点该跑了，真正干活的工具调用 / LLM
    调用都发生在调用方提供的 ``run_one`` 回调里。``run_one`` 对正常业务失败
    不应抛异常，应把错误记录到节点字段里然后正常返回，否则该节点的下游永远
    无法启动。
    """

    def __init__(self, concurrency: int, stall_timeout_seconds: float) -> None:
        self._concurrency = max(1, concurrency)
        self._stall_timeout = stall_timeout_seconds

    async def run(
        self,
        nodes: list[N],
        run_one: Callable[[N], Awaitable[None]],
    ) -> None:
        """按 ``depends_on`` 拓扑执行 ``nodes``。

        Kahn 算法骨架：
            1. ``remaining_deps[id]`` = 该节点还差几个 dep 未完成；
            2. ready 队列 = 当前 ``remaining_deps == 0`` 的节点 id；
            3. 在 semaphore 限制下从 ready 取节点跑；
            4. 一个节点跑完后，递减它每个孩子的 remaining_deps，归零的孩子
               入 ready；
            5. stall 看门狗：``ready.get()`` 超时则强制解锁所有 pending。
        """
        if not nodes:
            return

        id_to_node = {n.id: n for n in nodes}
        remaining_deps = {n.id: len(n.depends_on) for n in nodes}
        children: dict[int, list[int]] = {n.id: [] for n in nodes}
        for n in nodes:
            for dep in n.depends_on:
                if dep in children:
                    children[dep].append(n.id)

        ready: asyncio.Queue[int] = asyncio.Queue()
        for nid, count in remaining_deps.items():
            if count == 0:
                ready.put_nowait(nid)

        pending = len(nodes)
        sem = asyncio.Semaphore(self._concurrency)
        in_flight: set[asyncio.Task[None]] = set()
        deps_lock = asyncio.Lock()  # 序列化 remaining_deps[] 的修改

        async def runner(node: N) -> None:
            async with sem:
                await run_one(node)
            async with deps_lock:
                for child_id in children.get(node.id, []):
                    remaining_deps[child_id] -= 1
                    if remaining_deps[child_id] == 0:
                        await ready.put(child_id)

        # 主循环：从 ready 取节点、创建 runner task
        while pending > 0:
            try:
                next_id = await asyncio.wait_for(ready.get(), timeout=self._stall_timeout)
            except asyncio.TimeoutError:  # pragma: no cover - 防御
                logger.error(
                    "调度器卡住：%.1fs 内没有 ready 节点，强制解锁剩余 %d 个",
                    self._stall_timeout, pending,
                )
                # 优先保 liveness：把所有还 pending 的节点解锁
                stuck = [nid for nid, c in remaining_deps.items() if c > 0]
                for nid in stuck:
                    remaining_deps[nid] = 0
                    await ready.put(nid)
                continue

            task = asyncio.create_task(runner(id_to_node[next_id]))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
            pending -= 1

        # 排空尾部还在跑的 runner
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)
