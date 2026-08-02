"""ID 与幂等键生成。

这个模块只有几十行，却是整套可靠性设计的地基。核心是 `build_idempotency_key`。

**为什么幂等键要按「动作」生成，而不是按「请求」生成？**

普通 HTTP 接口用请求 ID 做幂等键就够了。但在 Agent 里，模型可能在第 3 步和第 7 步
提出**同一个动作**（它忘了自己做过）。这是两个不同的请求、不同的时间戳——
用请求 ID 会被判为两笔，于是真的执行两次，客户被打了两次折。

所以幂等键必须由「这次动作的语义」决定：

    task_id + step_name + tool_name + normalized_arguments_hash

其中参数归一化（canonical_json）必须做三件事，否则「同一个动作」会因格式差异被算成两个：

    键排序      {"a":1,"b":2} 与 {"b":2,"a":1} 等价
    数字归一    328 与 328.00 等价
    字符串 trim " C001 " 与 "C001" 等价
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


def new_id(prefix: str) -> str:
    """生成带前缀的短 ID。

    Args:
        prefix: 语义前缀，例如 ``"task"`` / ``"step"``。前缀让日志肉眼可读，
            出问题时不用查表就知道这个 ID 是什么东西。

    Returns:
        形如 ``task_9f2c1a4b8e6d47f0`` 的字符串。
    """
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def new_task_id() -> str:
    """生成任务 ID。"""
    return new_id("task")


def new_step_id() -> str:
    """生成步骤 ID。"""
    return new_id("step")


def new_execution_id() -> str:
    """生成单次工具执行的 ID（同一步骤重试多次会有多个 execution_id）。"""
    return new_id("exec")


def new_approval_id() -> str:
    """生成审批单 ID。"""
    return new_id("appr")


def new_event_id() -> str:
    """生成审计事件 ID。"""
    return new_id("evt")


def new_trace_id() -> str:
    """生成 trace ID。

    格式对齐 W3C traceparent 的 trace-id（32 位十六进制），
    这样将来接入 OpenTelemetry 时可以直接复用，不需要改数据。
    """
    return uuid.uuid4().hex


def new_span_id() -> str:
    """生成 span ID（16 位十六进制，对齐 W3C traceparent）。"""
    return uuid.uuid4().hex[:16]


def utcnow() -> datetime:
    """返回当前 UTC 时间（带时区）。

    全系统统一用带时区的 UTC：跨时区部署时，naive datetime 是重放和对账的噩梦。
    """
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    """把可能是 naive 的 datetime 补成带 UTC 时区。

    **为什么必须有这个函数：**

    SQLite 没有原生的时区类型，`DateTime(timezone=True)` 存进去的
    aware datetime 读出来是 **naive** 的。于是任何
    ``utcnow() >= expires_at`` 这样的比较都会抛
    ``TypeError: can't compare offset-naive and offset-aware datetimes``。

    这个 Bug 的恶劣之处在于它只在**读回已落库的数据**时才出现——
    单元测试里用内存构造的对象全是 aware 的，跑得好好的；
    一旦经过一次数据库往返就炸。审批超时判定、悬挂步骤判定
    全都踩在这条路径上。

    PostgreSQL 的 ``timestamptz`` 没有这个问题，但正因为如此，
    只在 PG 上测过的代码换到 SQLite 会立刻挂——所以统一在读取侧归一化，
    让业务代码永远只面对 aware datetime。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize(value: Any) -> Any:
    """递归归一化参数值，用于生成稳定的幂等键。

    归一化规则（每一条都对应一个真实踩过的坑）：

    * ``dict``：按键排序，保证 ``{"a":1,"b":2}`` 与 ``{"b":2,"a":1}`` 得到同一个键。
    * ``float`` / ``Decimal``：统一成规范化的十进制字符串，保证 ``328`` 与 ``328.00`` 等价。
    * ``str``：去掉首尾空白，保证 ``" C001 "`` 与 ``"C001"`` 等价。
    * ``None``：保留（``{"a": None}`` 和 ``{}`` 语义不同，不能合并）。
    """
    if isinstance(value, dict):
        return {str(k): _normalize(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, bool):
        # bool 必须在 int 之前判断：Python 里 True 是 int 的子类。
        return value
    if isinstance(value, Decimal):
        return _normalize_number(value)
    if isinstance(value, float):
        return _normalize_number(Decimal(str(value)))
    if isinstance(value, int):
        return _normalize_number(Decimal(value))
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if value is None:
        return None
    return str(value)


def _normalize_number(value: Decimal) -> str:
    """把数字归一化成不含无意义尾零的十进制字符串。

    ``328`` / ``328.0`` / ``328.00`` / ``Decimal("328.000")`` 全部得到 ``"328"``。
    """
    normalized = value.normalize()
    # normalize() 对 1E+2 这类会用科学计数法，转成定点表示避免 "100" 与 "1E+2" 不一致。
    sign, digits, exponent = normalized.as_tuple()
    if isinstance(exponent, int) and exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    return format(normalized, "f")


def canonical_json(payload: Any) -> str:
    """把任意参数结构序列化成规范化 JSON 字符串。

    Args:
        payload: 待序列化的参数（通常是工具入参 dict 或 Pydantic ``model_dump()``）。

    Returns:
        排序、归一化后的紧凑 JSON 字符串。相同语义的参数一定得到相同字符串。
    """
    return json.dumps(_normalize(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def arguments_hash(arguments: Any) -> str:
    """计算参数指纹。

    用途有两个：
    1. 参与幂等键生成；
    2. 幂等命中时校验「同一个键的参数是不是真的一样」——
       如果不一样必须报 :class:`IdempotencyConflictError`，而不是返回旧结果。
    """
    return hashlib.sha256(canonical_json(arguments).encode("utf-8")).hexdigest()


def build_idempotency_key(
    *,
    task_id: str,
    step_name: str,
    tool_name: str,
    arguments: Any,
    suffix: str | None = None,
) -> str:
    """生成幂等键。

    Args:
        task_id: 任务 ID。同一个任务内的重复动作要被识别为同一笔。
        step_name: 步骤名（用 name 而非 step_id：恢复重建步骤时 step_id 可能变，
            但「这是第几步、干什么的」不会变）。
        tool_name: 工具名。
        arguments: 工具入参，会经过 :func:`canonical_json` 归一化。
        suffix: 可选后缀。补偿动作用 ``"comp"``，
            这样正向动作和补偿动作各有各的幂等键，互不干扰。

    Returns:
        64 位十六进制字符串（sha256 全长），落库时带唯一约束。

    Note:
        这个键必须**落库**。只在内存里维护去重表意味着进程一重启就失忆，
        而恢复时正是最需要它的时刻——对账靠的就是这把钥匙。
    """
    raw = f"{task_id}|{step_name}|{tool_name}|{canonical_json(arguments)}"
    if suffix:
        raw = f"{raw}|{suffix}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
