"""Pipeline Stage 共用的事件分发协议。

为什么这么设计
--------------
Stage 不应该知道调用方要"流式（SSE）"还是"批处理（攒完一次性返回）"，
它只管 ``emit(event)``。由上层 Agent 决定 emit 的具体行为：

* ``agent.run()``         → 提供 noop emitter（事件全部丢弃）
* ``agent.run_stream()``  → 提供"塞入 asyncio.Queue 的 emitter"（推给 SSE）

只保留一个简单的 ``Callable`` 别名（而不是完整的 pub/sub 总线）是刻意的：
每次 run 只有一个订阅者，加 EventBus 是过度设计。
"""

from __future__ import annotations

from typing import Callable

from ..models import BaseEvent

# 消费单个事件的函数。同步签名是故意的：emit() 必须能在 async 协程内
# 任何位置无锁调用，不需要 await。
EventEmitter = Callable[[BaseEvent], None]


def noop_emitter(_event: BaseEvent) -> None:
    """把事件丢弃。``agent.run()`` 用它。"""
    return None
