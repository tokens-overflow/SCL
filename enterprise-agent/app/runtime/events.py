"""领域事件总线。

用途：把「状态变了」这件事广播给关心它的旁路组件（指标、通知、Webhook），
**而不让主流程知道有谁在听**。

一条重要纪律：**事件处理器不能影响主流程**。

如果一个指标上报失败就导致任务失败，那就本末倒置了。
所以 `publish()` 会吞掉处理器的异常并记日志——
这是少数几个「异常应该被吞掉」的场景之一，因为这里的失败
确实不影响业务正确性。

注意事件总线**不是**状态持久化的替代品。
状态的权威来源永远是数据库里的步骤表；事件只是通知。
如果哪天有人开始用「重放事件」来恢复任务状态，那就走上歧路了——
事件可能丢失、可能乱序，而状态表不会。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.ids import utcnow
from app.operations.logging import get_logger

logger = get_logger(__name__)


@dataclass
class DomainEvent:
    """领域事件。

    Attributes:
        name: 事件名，如 ``task.completed`` / ``step.retry_scheduled``。
        task_id: 关联任务。
        payload: 事件载荷。
        trace_id: 链路 ID。
        occurred_at: 发生时间。
    """

    name: str
    task_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    occurred_at: str = field(default_factory=lambda: utcnow().isoformat())


EventHandler = Callable[[DomainEvent], Awaitable[None]]


class EventBus:
    """进程内异步事件总线。

    Note:
        生产环境若需要跨进程分发，应替换为 Kafka / RabbitMQ / Redis Stream。
        接口保持不变，调用方零改动。
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._wildcard: list[EventHandler] = []

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        """订阅指定事件。

        Args:
            event_name: 事件名，``"*"`` 表示订阅全部。
            handler: 异步处理器。
        """
        if event_name == "*":
            self._wildcard.append(handler)
        else:
            self._handlers[event_name].append(handler)

    def unsubscribe_all(self) -> None:
        """清空所有订阅（测试用）。"""
        self._handlers.clear()
        self._wildcard.clear()

    async def publish(self, event: DomainEvent) -> None:
        """发布事件。

        所有处理器并发执行，**任何一个失败都不会影响主流程**，
        也不会影响其它处理器（`return_exceptions=True`）。
        """
        handlers = list(self._handlers.get(event.name, [])) + list(self._wildcard)
        if not handlers:
            return

        results = await asyncio.gather(
            *(handler(event) for handler in handlers), return_exceptions=True
        )
        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, BaseException):
                # 刻意吞掉：旁路失败不能拖垮主流程。但一定要记日志，
                # 否则「指标为什么不涨」会变成一个没有线索的问题。
                logger.warning(
                    "event_handler_failed",
                    event_name=event.name,
                    handler=getattr(handler, "__name__", repr(handler)),
                    error=str(result),
                    task_id=event.task_id,
                )


#: 全局事件总线。
event_bus = EventBus()


# --------------------------------------------------------------------------------------
# 标准事件名。集中定义避免拼写不一致导致「订阅了但永远收不到」。
# --------------------------------------------------------------------------------------
TASK_CREATED = "task.created"
TASK_COMPLETED = "task.completed"
TASK_FAILED = "task.failed"
TASK_PARTIAL_SUCCESS = "task.partial_success"
TASK_WAITING_APPROVAL = "task.waiting_approval"
TASK_RESUMED = "task.resumed"
STEP_SUCCEEDED = "step.succeeded"
STEP_FAILED = "step.failed"
STEP_TIMEOUT = "step.timeout"
STEP_RETRY_SCHEDULED = "step.retry_scheduled"
STEP_COMPENSATED = "step.compensated"
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_DECIDED = "approval.decided"


async def register_metrics_handlers() -> None:
    """注册把领域事件转成指标的处理器。

    这是「事件总线的正确用法」的示范：指标采集是纯旁路，
    它关心状态变化，但状态变化完全不需要关心它。
    """
    from app.operations.metrics import metrics

    async def on_any(event: DomainEvent) -> None:
        metrics.increment("agent_events_total", event=event.name)

    async def on_task_completed(event: DomainEvent) -> None:
        metrics.increment("agent_tasks_total", outcome="completed")
        if "elapsed_seconds" in event.payload:
            metrics.observe(
                "agent_task_duration_seconds", float(event.payload["elapsed_seconds"])
            )

    async def on_task_failed(event: DomainEvent) -> None:
        metrics.increment(
            "agent_tasks_total",
            outcome="failed",
            error_code=str(event.payload.get("error_code", "unknown")),
        )

    async def on_retry(event: DomainEvent) -> None:
        metrics.increment(
            "agent_step_retries_total", tool=str(event.payload.get("tool_name", "unknown"))
        )

    event_bus.subscribe("*", on_any)
    event_bus.subscribe(TASK_COMPLETED, on_task_completed)
    event_bus.subscribe(TASK_FAILED, on_task_failed)
    event_bus.subscribe(STEP_RETRY_SCHEDULED, on_retry)
