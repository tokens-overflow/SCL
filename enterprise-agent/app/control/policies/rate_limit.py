"""RateLimitPolicy：频率限制与熔断。

两个不同的目的：

1. **限流**：防止单个用户或 Agent 在短时间内发起过多写操作。
   Agent 的调用序列由模型即兴决定，一个循环没写好就可能在几秒内
   打出上百次调用——传统系统里人手点不了那么快，Agent 可以。

2. **熔断**：如果短时间内失败率异常高，大概率不是单笔请求的问题，
   而是上游挂了、或者刚部署的 Prompt 改坏了。这时候应该**停下来告警**，
   而不是继续把几百笔请求全变成失败记录淹没运维。

实现用的是**内存滑动窗口**。这在单进程 Demo 里够用，
但请注意它的局限：多副本部署时每个副本各算各的，实际限额会被放大 N 倍。
生产环境应该换成 Redis 或专门的限流服务——接口不变，只换实现。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from app.control.models import PolicyEvaluationRequest, PolicyEvaluationResult
from app.core.enums import RiskLevel


class RateLimitPolicy:
    """滑动窗口限流 + 简易熔断。

    Args:
        max_writes_per_minute: 每个用户每分钟允许的写操作次数。
        circuit_failure_threshold: 熔断触发所需的失败次数。
        circuit_window_seconds: 熔断统计窗口。
    """

    name = "RateLimitPolicy"

    def __init__(
        self,
        *,
        max_writes_per_minute: int = 20,
        circuit_failure_threshold: int = 10,
        circuit_window_seconds: int = 60,
    ) -> None:
        self.max_writes_per_minute = max_writes_per_minute
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_window_seconds = circuit_window_seconds
        self._writes: dict[str, deque[float]] = defaultdict(deque)
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def record_write(self, user_id: str) -> None:
        """记录一次写操作。由执行器在放行后调用。"""
        self._writes[user_id].append(time.monotonic())

    def record_failure(self, tool_name: str) -> None:
        """记录一次工具失败。用于熔断判定。"""
        self._failures[tool_name].append(time.monotonic())

    def reset(self) -> None:
        """清空所有计数（测试用）。"""
        self._writes.clear()
        self._failures.clear()

    def _prune(self, bucket: deque[float], window: float) -> None:
        cutoff = time.monotonic() - window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    async def evaluate(self, request: PolicyEvaluationRequest) -> PolicyEvaluationResult:
        """评估是否触发限流或熔断。

        Returns:
            * 熔断中 → MANUAL_REVIEW（**不是 DENY**：
              这不是这笔请求的错，是系统状态有问题，需要人来看）；
            * 超限 → DENY 且 `retry_after` 写进 metadata；
            * 正常 → ALLOW。
        """
        # —— 熔断检查 ——
        failures = self._failures[request.tool_name]
        self._prune(failures, self.circuit_window_seconds)
        if len(failures) >= self.circuit_failure_threshold:
            return PolicyEvaluationResult.manual_review(
                self.name,
                "CIRCUIT_BREAKER_OPEN",
                (
                    f"工具 {request.tool_name} 在 {self.circuit_window_seconds} 秒内"
                    f"失败 {len(failures)} 次，已熔断，请人工确认上游状态"
                ),
                risk_level=RiskLevel.HIGH,
                metadata={
                    "failure_count": len(failures),
                    "window_seconds": self.circuit_window_seconds,
                },
            )

        # —— 限流检查（只针对写操作）——
        # 读操作限流意义不大，还会影响正常的信息查询。
        if not request.tool_is_write:
            return PolicyEvaluationResult.allow(self.name)

        user_id = request.identity.user.user_id
        writes = self._writes[user_id]
        self._prune(writes, 60.0)
        if len(writes) >= self.max_writes_per_minute:
            # Retry-After：告诉调用方**什么时候可以再来**，
            # 而不是让它盲目地立刻重试。
            retry_after = max(1, int(60 - (time.monotonic() - writes[0])))
            return PolicyEvaluationResult.deny(
                self.name,
                "RATE_LIMIT_EXCEEDED",
                f"操作过于频繁，请 {retry_after} 秒后重试",
                risk_level=RiskLevel.MEDIUM,
                metadata={
                    "retry_after_seconds": retry_after,
                    "limit": self.max_writes_per_minute,
                },
            )

        return PolicyEvaluationResult.allow(self.name)
