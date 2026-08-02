"""重试策略单元测试。"""

from __future__ import annotations

from app.core.enums import ErrorCode, StepStatus, is_retryable
from app.runtime.retry import RetryPolicy, classify_error


class TestErrorClassification:
    def test_retryable_whitelist(self) -> None:
        assert is_retryable(ErrorCode.UPSTREAM_UNAVAILABLE) is True
        assert is_retryable(ErrorCode.RATE_LIMITED) is True
        assert is_retryable(ErrorCode.NETWORK_ERROR) is True

    def test_business_errors_are_not_retryable(self) -> None:
        """余额不足、参数非法、权限不足——重试一万次也是同一个结果。"""
        assert is_retryable(ErrorCode.INVALID_ARGUMENT) is False
        assert is_retryable(ErrorCode.PERMISSION_DENIED) is False
        assert is_retryable(ErrorCode.BUSINESS_RULE_VIOLATION) is False
        assert is_retryable(ErrorCode.DUPLICATE_ACTIVE_DISCOUNT) is False

    def test_timeout_is_not_retryable_without_reconciliation(self) -> None:
        """**超时不等于可重试**——必须先对账。"""
        assert is_retryable(ErrorCode.TIMEOUT) is False
        assert is_retryable(ErrorCode.UNKNOWN_EXECUTION_STATE) is False

    def test_unknown_error_code_defaults_to_not_retryable(self) -> None:
        """未知错误默认不重试：对写操作来说多重试一次可能就是多扣一笔钱。"""
        assert is_retryable("SOME_NEW_ERROR") is False
        assert is_retryable(None) is False

    def test_classification_buckets(self) -> None:
        assert classify_error(ErrorCode.TIMEOUT) == "unknown_state"
        assert classify_error(ErrorCode.RATE_LIMITED) == "transient"
        assert classify_error(ErrorCode.BUSINESS_RULE_VIOLATION) == "business"


class TestRetryPolicy:
    def test_timeout_never_auto_retries(self, settings) -> None:
        """超时状态下**永远不安排自动重试**，必须先对账。"""
        policy = RetryPolicy(settings)
        for status in (StepStatus.TIMEOUT, StepStatus.UNKNOWN):
            decision = policy.decide(
                step_status=status, error_code=ErrorCode.TIMEOUT,
                retryable_hint=True, retry_count=0, max_retries=3,
            )
            assert decision.should_retry is False
            assert "对账" in decision.reason

    def test_retryable_error_gets_scheduled(self, settings) -> None:
        policy = RetryPolicy(settings)
        decision = policy.decide(
            step_status=StepStatus.FAILED, error_code=ErrorCode.UPSTREAM_UNAVAILABLE,
            retryable_hint=True, retry_count=0, max_retries=3,
        )
        assert decision.should_retry is True
        assert decision.delay_seconds > 0
        assert decision.next_retry_at is not None

    def test_callee_declaration_wins(self, settings) -> None:
        """retryable 由**被调方**声明，优先于错误码白名单。"""
        policy = RetryPolicy(settings)
        decision = policy.decide(
            step_status=StepStatus.FAILED, error_code=ErrorCode.UPSTREAM_UNAVAILABLE,
            retryable_hint=False, retry_count=0, max_retries=3,
        )
        assert decision.should_retry is False

    def test_max_retries_exhausted(self, settings) -> None:
        policy = RetryPolicy(settings)
        decision = policy.decide(
            step_status=StepStatus.FAILED, error_code=ErrorCode.UPSTREAM_UNAVAILABLE,
            retryable_hint=True, retry_count=3, max_retries=3,
        )
        assert decision.should_retry is False
        assert "最大重试次数" in decision.reason

    def test_exponential_backoff_grows(self, settings) -> None:
        policy = RetryPolicy(settings)
        # 带抖动，所以比较均值区间而不是精确值。
        d1 = [policy.compute_delay(1) for _ in range(20)]
        d3 = [policy.compute_delay(3) for _ in range(20)]
        assert sum(d3) / len(d3) > sum(d1) / len(d1)

    def test_retry_after_takes_precedence(self, settings) -> None:
        """下游明确给出 Retry-After 时优先采纳——它最清楚自己什么时候能缓过来。"""
        policy = RetryPolicy(settings)
        assert policy.compute_delay(1, retry_after=7.0) == 7.0

    def test_delay_capped(self, settings) -> None:
        policy = RetryPolicy(settings)
        assert policy.compute_delay(50) <= settings.retry_max_delay_seconds * 1.3
