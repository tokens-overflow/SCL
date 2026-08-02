"""分布式追踪（OpenTelemetry 接口预留）。

**为什么 Agent 特别需要 Trace？**

因为它的行为是非确定性的。传统服务看堆栈就知道哪行报错；
Agent 出问题可能是四种原因之一，而它们的**症状完全一样**：

* 模型把「¥1,200.00」读成了「¥120000」（解析层）
* Prompt 里没强调币种，模型默认按美元算（Prompt 层）
* 工具返回的数据本身是脏的（工具层）
* 模型逻辑没错，但上一步喂给它的上下文就错了（编排层）

没有 Trace，你只能靠猜。

关键点有三个，缺一个就只是「打印了点东西」：

1. **Prompt 快照**：记录当时实际发给模型的完整上下文；
2. **可关联**：所有 span 挂在 task_id 下，30 秒能回放出因果链；
3. **决策依据**：不只记「结果是转人工」，记「因为置信度 0.62 < 0.8」。

本模块默认使用**无依赖的内置实现**，同时预留 OpenTelemetry 接入点：
装了 opentelemetry-api 且 `OTEL_ENABLED=true` 时自动切换。
这样骨架项目不会因为一个可观测性库装不上就跑不起来。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.ids import new_span_id, new_trace_id, utcnow
from app.operations.logging import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - 取决于是否安装了 OTel
    from opentelemetry import trace as otel_trace

    _HAS_OTEL = True
except ImportError:  # pragma: no cover
    otel_trace = None  # type: ignore[assignment]
    _HAS_OTEL = False


@dataclass
class Span:
    """一次操作的追踪片段。"""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=lambda: utcnow().isoformat())
    ended_at: str | None = None
    status: str = "OK"

    def set_attribute(self, key: str, value: Any) -> None:
        """设置属性。"""
        self.attributes[key] = value

    def record_error(self, error: BaseException) -> None:
        """记录异常。"""
        self.status = "ERROR"
        self.attributes["error.type"] = type(error).__name__
        self.attributes["error.message"] = str(error)[:500]


class Tracer:
    """追踪器。

    默认实现把 span 写进结构化日志——这已经足以支撑「按 task_id 回放因果链」。
    接入 OTel 之后 span 会同时发往真实的追踪后端。
    """

    def __init__(self, service_name: str = "enterprise-agent") -> None:
        self.service_name = service_name
        self._settings = get_settings()
        self._otel_tracer = (
            otel_trace.get_tracer(service_name)
            if (_HAS_OTEL and self._settings.otel_enabled)
            else None
        )

    @contextmanager
    def span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        **attributes: Any,
    ) -> Iterator[Span]:
        """开启一个 span。

        Args:
            name: span 名称，例如 ``llm.parse_intent`` / ``tool.apply_discount``。
            trace_id: 链路 ID。缺省新建（顶层 span）。
            parent_span_id: 父 span。
            **attributes: 初始属性。至少应带上 task_id。

        Yields:
            :class:`Span`。可在块内继续 `set_attribute`。
        """
        span = Span(
            name=name,
            trace_id=trace_id or new_trace_id(),
            span_id=new_span_id(),
            parent_span_id=parent_span_id,
            attributes=dict(attributes),
        )
        otel_ctx = (
            self._otel_tracer.start_as_current_span(name) if self._otel_tracer else None
        )
        if otel_ctx is not None:  # pragma: no cover - 需要 OTel 环境
            otel_ctx.__enter__()
        try:
            yield span
        except BaseException as exc:
            span.record_error(exc)
            raise
        finally:
            span.ended_at = utcnow().isoformat()
            if otel_ctx is not None:  # pragma: no cover
                otel_ctx.__exit__(None, None, None)
            logger.debug(
                "span",
                span_name=span.name,
                trace_id=span.trace_id,
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                status=span.status,
                **{f"attr_{k}": v for k, v in span.attributes.items()},
            )


#: 全局追踪器。
tracer = Tracer()


def ensure_trace_id(existing: str | None = None) -> str:
    """确保有一个 trace_id。

    Args:
        existing: 已有的 trace_id（例如从 HTTP header 里透传进来的）。

    Returns:
        原值或新生成的 trace_id。

    Note:
        入口层应该优先复用调用方传来的 trace_id，
        这样 Agent 的链路能和上游业务系统的链路串起来——
        **少了这一步，你只能看到 Agent 内部发生了什么，
        看不到它是被谁触发的。**
    """
    return existing or new_trace_id()
