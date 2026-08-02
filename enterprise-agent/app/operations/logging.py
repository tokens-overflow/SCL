"""结构化日志。

三条要求，每一条都对应一个真实的排查痛点：

1. **每条日志必须能被 task_id / trace_id 串起来。**
   Agent 的行为是非确定性的，出问题时你需要的不是「哪一行报错」，
   而是「这一单从头到尾发生了什么」。没有关联字段，日志就是一堆碎片。

2. **绝不打印密钥与未脱敏个人信息。**
   所有日志值都会过一遍 :func:`app.security.secrets.redact`。
   这是兜底，不是许可——调用方本来就不该把这些东西传进来。

3. **structlog 可选。** 没装 structlog 时自动退化到标准 logging，
   保持同样的调用方式。骨架项目不应该因为一个可观测性库装不上就跑不起来。
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

from app.security.secrets import redact

#: 请求级上下文。用 ContextVar 而不是参数透传，是为了让日志字段自动跟随异步调用链，
#: 不需要在每个函数签名里塞一个 trace_id。
# 注意 default 用不可变的空元组转 dict——ContextVar 的默认值会被所有
# 上下文共享，用可变 dict 做默认值意味着一次意外的原地修改会污染全局。
_log_context: ContextVar[dict[str, Any]] = ContextVar("log_context")

#: 每条日志都应该带上的关联字段（架构文档明确要求的那一组）。
CORRELATION_FIELDS = (
    "task_id",
    "step_id",
    "trace_id",
    "user_id",
    "agent_id",
    "tool_name",
    "event_type",
)

try:  # pragma: no cover - 取决于是否安装了 structlog
    import structlog

    _HAS_STRUCTLOG = True
except ImportError:  # pragma: no cover
    structlog = None  # type: ignore[assignment]
    _HAS_STRUCTLOG = False


def bind_context(**kwargs: Any) -> dict[str, Any]:
    """把关联字段绑定到当前异步上下文。

    Args:
        **kwargs: 要绑定的字段，通常是 task_id / trace_id / user_id。

    Returns:
        绑定前的上下文快照，便于调用方在结束时恢复。

    Example:
        >>> token = bind_context(task_id="task_1", trace_id="abc")
        >>> get_logger(__name__).info("something_happened")
    """
    current = dict(_log_context.get({}))
    previous = dict(current)
    current.update({k: v for k, v in kwargs.items() if v is not None})
    _log_context.set(current)
    return previous


def reset_context(previous: dict[str, Any] | None = None) -> None:
    """恢复上下文到指定快照（缺省清空）。"""
    _log_context.set(dict(previous or {}))


def get_context() -> dict[str, Any]:
    """返回当前绑定的关联字段。"""
    return dict(_log_context.get({}))


class _StdlibLoggerAdapter:
    """标准 logging 的适配器，提供与 structlog 一致的 kwargs 风格接口。

    这样业务代码写 `logger.info("event_name", task_id=...)` 时，
    装没装 structlog 都能跑，且输出内容一致。
    """

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _emit(self, level: int, event: str, **kwargs: Any) -> None:
        payload = {**get_context(), **kwargs, "event": event}
        # 统一脱敏：即使调用方不小心传了密钥，也不会落到磁盘上。
        safe = redact(payload)
        try:
            rendered = json.dumps(safe, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover - 兜底
            rendered = str(safe)
        self._logger.log(level, rendered)

    def debug(self, event: str, **kwargs: Any) -> None:
        """DEBUG 级日志。"""
        self._emit(logging.DEBUG, event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        """INFO 级日志。"""
        self._emit(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        """WARNING 级日志。"""
        self._emit(logging.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        """ERROR 级日志。"""
        self._emit(logging.ERROR, event, **kwargs)

    def exception(self, event: str, **kwargs: Any) -> None:
        """ERROR 级日志并附带异常栈。"""
        payload = {**get_context(), **kwargs, "event": event}
        self._logger.exception(json.dumps(redact(payload), ensure_ascii=False, default=str))


def _structlog_context_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor：注入关联字段。"""
    for key, value in get_context().items():
        event_dict.setdefault(key, value)
    return event_dict


def _structlog_redact_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor：统一脱敏。"""
    return redact(event_dict)  # type: ignore[return-value]


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """初始化日志系统。应用启动时调用一次。

    Args:
        level: 日志级别。
        json_output: 是否输出 JSON（生产环境建议 True，便于日志系统采集）。
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )

    if _HAS_STRUCTLOG:  # pragma: no branch
        renderer = (
            structlog.processors.JSONRenderer(ensure_ascii=False)
            if json_output
            else structlog.dev.ConsoleRenderer(colors=False)
        )
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                _structlog_context_processor,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                _structlog_redact_processor,
                renderer,
            ],
            wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )


def get_logger(name: str) -> Any:
    """获取一个结构化 logger。

    Args:
        name: 通常传 ``__name__``。

    Returns:
        structlog logger 或标准库适配器，两者接口一致。
    """
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return _StdlibLoggerAdapter(name)
