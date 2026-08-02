"""统一异常体系。

设计要点：

1. **异常必须携带机器可读的 error_code**。上层的重试 / 补偿 / 转人工决策要靠错误码分流，
   靠 `str(exc)` 做字符串匹配是一种一改文案就崩的耦合。

2. **异常必须声明 retryable，且由被调方声明**。只有工具自己知道「文档缺失」重试也没用，
   而「向量库连接失败」重试就能好。上层编排器靠猜是嵌套编排里最常见的耦合来源。

3. `AgentError` 统一携带 `details`，用于把结构化上下文（客户号、上限值等）
   带回给认知层，让 LLM 知道「此路不通、以及为什么」，从而重新生成方案。
"""

from __future__ import annotations

from typing import Any

from app.core.enums import ErrorCode, is_retryable


class AgentError(Exception):
    """框架内所有业务异常的基类。

    Attributes:
        error_code: 机器可读错误码，决定后续走哪条路。
        message: 给人看的原因说明。
        details: 结构化补充信息（会被写入审计，注意不要放敏感原文）。
        retryable: 是否允许自动重试。默认按错误码白名单推导，也可由调用方显式覆盖。
    """

    default_code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        error_code: ErrorCode | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code: ErrorCode = error_code or self.default_code
        self.details: dict[str, Any] = details or {}
        # retryable 显式优先：被调方最清楚自己这次失败能不能靠重试解决。
        self.retryable: bool = is_retryable(self.error_code) if retryable is None else retryable

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入审计 / API 响应的字典。"""
        return {
            "error_code": str(self.error_code),
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"{type(self).__name__}(code={self.error_code}, message={self.message!r})"


# --------------------------------------------------------------------------------------
# 控制层相关
# --------------------------------------------------------------------------------------
class PolicyViolationError(AgentError):
    """控制层明确拒绝执行。不可重试——规则不会因为再试一次就变。"""

    default_code = ErrorCode.POLICY_DENIED


class PermissionDeniedError(AgentError):
    """权限不足。

    注意：对外话术要统一，不要泄漏「这个资源存在但你没权限」这类信息，
    真实原因写进审计即可。
    """

    default_code = ErrorCode.PERMISSION_DENIED


class ValidationError(AgentError):
    """参数校验失败。

    这是「结构化输出只是必要条件，不是充分条件」的落点：
    Pydantic 只能保证 `discount_rate` 是个 0~1 的 float，
    保证不了「这个客服有没有资格给这么大的折扣」——那是业务规则的事。
    """

    default_code = ErrorCode.INVALID_ARGUMENT


# --------------------------------------------------------------------------------------
# 状态机 / 运行时相关
# --------------------------------------------------------------------------------------
class IllegalStateTransitionError(AgentError):
    """非法状态转换。

    必须抛异常而不是「静默修正」：脏状态一旦落库，恢复逻辑就会做出错误决策
    （典型灾难：SUCCESS 被改回 PENDING，于是恢复时重复执行了一次写操作）。
    调用方在捕获后还必须写一条 ILLEGAL_STATE_TRANSITION 审计。
    """

    default_code = ErrorCode.ILLEGAL_STATE_TRANSITION

    def __init__(self, current: str, event: str, scope: str = "task") -> None:
        super().__init__(
            f"非法状态转换：{scope} 处于 {current} 时不允许事件 {event}",
            details={"scope": scope, "current_status": current, "event": event},
            retryable=False,
        )
        self.current = current
        self.event = event
        self.scope = scope


class TaskNotFoundError(AgentError):
    """任务不存在。"""

    default_code = ErrorCode.NOT_FOUND


class ApprovalNotFoundError(AgentError):
    """审批单不存在。"""

    default_code = ErrorCode.NOT_FOUND


# --------------------------------------------------------------------------------------
# 行动层相关
# --------------------------------------------------------------------------------------
class ToolNotRegisteredError(AgentError):
    """工具未注册。

    这是防「模型幻觉出一个工具名」的最后一道闸：
    注册表里没有的名字一律拒绝，绝不允许通过字符串动态 import 任意模块，
    更不允许 eval 模型生成的代码。
    """

    default_code = ErrorCode.TOOL_NOT_REGISTERED


class ToolExecutionError(AgentError):
    """工具执行明确失败（外部系统给出了确定的失败结论）。"""

    default_code = ErrorCode.INTERNAL_ERROR


class ToolTimeoutError(AgentError):
    """工具执行超时。

    **超时不等于失败。** 抛出这个异常只意味着「我们不知道结果」，
    执行器必须把步骤落成 TIMEOUT/UNKNOWN 并进入对账流程，
    绝不能直接按失败回滚，也绝不能直接重试写操作。
    """

    default_code = ErrorCode.TIMEOUT

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        # 显式 retryable=False：超时**本身**不可直接重试，必须先对账。
        super().__init__(message, details=details, retryable=False)


class IdempotencyConflictError(AgentError):
    """同一幂等键但参数不同。

    这是幂等设计里最容易被漏掉的一条：如果只按 key 去重而不校验参数，
    「用同一个 key 提交了不同金额」会被当成重复请求直接返回旧结果，
    于是第二笔业务被静默吞掉。必须显式拒绝。
    """

    default_code = ErrorCode.IDEMPOTENCY_CONFLICT


class BusinessRuleViolationError(AgentError):
    """违反业务规则（例如已有生效折扣、超过折扣上限）。"""

    default_code = ErrorCode.BUSINESS_RULE_VIOLATION


class CompensationError(AgentError):
    """补偿动作失败。

    补偿也会失败，它并不比正向动作安全。补偿失败必须能转人工跟进，
    而不是静默吞掉——否则系统会停在一个「补了一半」的状态里。
    """

    default_code = ErrorCode.INTERNAL_ERROR


# --------------------------------------------------------------------------------------
# 认知层相关
# --------------------------------------------------------------------------------------
class LLMOutputInvalidError(AgentError):
    """LLM 输出无法解析为要求的结构。

    处理策略是「带着错误信息回认知层重试」，而不是放行一个半成品结构——
    结构化校验失败时绝不能「尽力猜一个默认值」，那会把一个明显错误
    变成一个隐蔽错误。
    """

    default_code = ErrorCode.LLM_OUTPUT_INVALID


class LLMProviderError(AgentError):
    """LLM 供应商调用失败（网络、限流、鉴权等）。"""

    default_code = ErrorCode.LLM_PROVIDER_ERROR
