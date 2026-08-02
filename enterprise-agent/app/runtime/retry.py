"""重试策略。

**核心区分：六种失败不是一回事。**

| 类型 | 例子 | 该走的路 |
|------|------|----------|
| 明确业务失败 | 余额不足、参数非法 | 不重试，回认知层或如实告知 |
| 技术失败（瞬时） | 下游 5xx、连接失败 | 指数退避重试，写操作必须带幂等键 |
| 限流 | 429 | 按 Retry-After 退避 |
| 网络超时 | 请求超时 | **不重试**，先对账 |
| 未知执行状态 | 进程崩在执行中途 | **不重试**，先对账 |
| 不可重试失败 | 权限不足、订单状态不允许 | 终止或转人工 |

最容易犯的错误是把「超时」当成「可重试的技术失败」。
超时意味着**结果未知**——如果写操作其实已经成功，重试就是第二笔。
所以本模块的 :func:`should_retry` 对 TIMEOUT / UNKNOWN 一律返回 False，
它们必须走 :mod:`app.runtime.recovery` 的对账通道。

关于退避 + 抖动：没有抖动的话，同时失败的一批任务会在同一时刻
一起重试，形成重试风暴把刚恢复的下游再次打挂。抖动是必需的，不是可选的。

关于 tenacity：本模块**不依赖** tenacity，而是自己实现了清晰的退避计算。
原因是这套逻辑需要**状态持久化**（重试次数、下次重试时间都要落库），
而 tenacity 的模型是进程内的循环重试——进程一挂，重试计划就没了。
在企业级场景里，重试必须能跨进程重启存活。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.config import Settings, get_settings
from app.core.enums import ErrorCode, StepStatus, is_retryable
from app.core.ids import utcnow


@dataclass
class RetryDecision:
    """重试判定结果。

    Attributes:
        should_retry: 是否安排重试。
        delay_seconds: 退避时长。
        next_retry_at: 下次重试时间（**要落库**，否则进程重启后重试计划就丢了）。
        reason: 判定理由，写入审计。
    """

    should_retry: bool
    delay_seconds: float = 0.0
    next_retry_at: datetime | None = None
    reason: str = ""


class RetryPolicy:
    """指数退避 + 抖动的重试策略。

    Args:
        settings: 配置对象。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def compute_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """计算退避时长。

        Args:
            attempt: 已经尝试过的次数（从 1 开始）。
            retry_after: 下游明确给出的 Retry-After（秒）。
                **有它就优先用它**——下游最清楚自己什么时候能缓过来，
                我们自己算出来的退避只是猜测。

        Returns:
            退避秒数，含随机抖动，并受 `retry_max_delay_seconds` 上限约束。
        """
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self.settings.retry_max_delay_seconds)

        base = self.settings.retry_base_delay_seconds * (2 ** max(0, attempt - 1))
        base = min(base, self.settings.retry_max_delay_seconds)

        # 抖动：在 [base*(1-j), base*(1+j)] 之间取值。
        # 没有抖动的话，同时失败的一批任务会在同一毫秒一起重试，
        # 把刚缓过来的下游再打挂一次。
        jitter = base * self.settings.retry_jitter_ratio
        delayed = base + random.uniform(-jitter, jitter)
        return max(0.0, round(delayed, 3))

    def decide(
        self,
        *,
        step_status: StepStatus,
        error_code: str | None,
        retryable_hint: bool | None,
        retry_count: int,
        max_retries: int,
        retry_after: float | None = None,
        now: datetime | None = None,
    ) -> RetryDecision:
        """判定是否重试以及何时重试。

        Args:
            step_status: 步骤当前状态。
            error_code: 失败的错误码。
            retryable_hint: **被调方声明**的可重试性。
                优先级高于错误码白名单——只有工具自己知道这次失败的性质。
            retry_count: 已重试次数。
            max_retries: 最大重试次数。
            retry_after: 下游给出的 Retry-After。
            now: 当前时间（测试可注入）。

        Returns:
            :class:`RetryDecision`。

        Note:
            判定顺序刻意如此：

            1. **未知状态优先**：TIMEOUT / UNKNOWN 一律不重试，先对账。
               这条必须放最前面，因为它会覆盖后面所有判断。
            2. 次数用尽 → 不重试。
            3. 被调方明确说不可重试 → 不重试（**信被调方，不猜**）。
            4. 错误码白名单 → 重试。
            5. 其余一律不重试（**未知错误默认不重试**：
               对写操作来说，多重试一次可能就是多扣一笔钱）。
        """
        now = now or utcnow()

        # 1. 结果未知 —— 这是最重要的一条分支。
        if step_status in (StepStatus.TIMEOUT, StepStatus.UNKNOWN):
            return RetryDecision(
                should_retry=False,
                reason=(
                    "步骤结果未知（超时或崩溃），必须先通过幂等键对账确认外部系统真实状态，"
                    "确认未发生后才能安全重试"
                ),
            )

        # 2. 重试次数用尽
        if retry_count >= max_retries:
            return RetryDecision(
                should_retry=False,
                reason=f"已达到最大重试次数 {max_retries}",
            )

        # 3. 被调方明确声明不可重试
        if retryable_hint is False:
            return RetryDecision(
                should_retry=False,
                reason=f"被调方声明该错误不可重试（error_code={error_code}）",
            )

        # 4. 白名单 / 被调方声明可重试
        allowed = retryable_hint is True or is_retryable(error_code)
        if not allowed:
            return RetryDecision(
                should_retry=False,
                reason=f"错误码 {error_code} 不在可重试白名单中，默认不重试",
            )

        delay = self.compute_delay(retry_count + 1, retry_after)
        return RetryDecision(
            should_retry=True,
            delay_seconds=delay,
            next_retry_at=now + timedelta(seconds=delay),
            reason=f"错误可重试，安排第 {retry_count + 1} 次重试，退避 {delay:.2f} 秒",
        )


def classify_error(error_code: str | None) -> str:
    """把错误码归入一个大类，用于指标和告警分组。

    Returns:
        ``"business"`` / ``"transient"`` / ``"unknown_state"`` / ``"fatal"``。
    """
    if error_code is None:
        return "fatal"
    try:
        code = ErrorCode(error_code)
    except ValueError:
        return "fatal"

    if code in (ErrorCode.TIMEOUT, ErrorCode.UNKNOWN_EXECUTION_STATE):
        return "unknown_state"
    if is_retryable(code):
        return "transient"
    if code in (
        ErrorCode.BUSINESS_RULE_VIOLATION,
        ErrorCode.DUPLICATE_ACTIVE_DISCOUNT,
        ErrorCode.INVALID_ARGUMENT,
        ErrorCode.NOT_FOUND,
        ErrorCode.POLICY_DENIED,
        ErrorCode.PERMISSION_DENIED,
        ErrorCode.APPROVAL_REJECTED,
    ):
        return "business"
    return "fatal"


#: 默认策略实例。
default_retry_policy = RetryPolicy()
