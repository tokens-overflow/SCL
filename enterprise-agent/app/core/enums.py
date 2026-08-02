"""核心枚举定义。

设计说明（为什么把枚举单独放一层）：

1. 枚举是整个系统的「共同语言」。状态层、控制层、行动层、运营层都要引用它们，
   如果散落在各自的模块里，很容易出现循环依赖（state 引 runtime、runtime 又引 state）。
   把它们收敛到 `core` 这个最底层、不依赖任何其它业务模块的包里，依赖方向就永远是单向的。

2. 状态值必须是**封闭集合**。企业级 Agent 里最贵的一类 Bug 是「状态被写成了一个
   没人认识的字符串」——比如某个业务模块随手写了 `task.status = "done"`，
   而恢复逻辑只认识 `COMPLETED`，于是这个任务永远悬着，没人知道它还在不在跑。
   用 `StrEnum` 强约束之后，非法值在写入前就会暴露。
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    """任务级状态。

    这些状态描述「一整次用户请求」的生命周期。
    终态（TERMINAL_TASK_STATUSES）非常重要：**没有终态的任务比失败的任务更麻烦**，
    因为没人知道它还在不在跑，恢复扫描会永远把它捞出来。
    """

    CREATED = "CREATED"                      # 任务已创建，尚未开始认知
    PLANNING = "PLANNING"                    # 正在调用 LLM 解析意图 / 生成计划
    RUNNING = "RUNNING"                      # 正在按计划推进步骤
    WAITING_APPROVAL = "WAITING_APPROVAL"    # 命中高风险规则，挂起等人工审批
    RETRYING = "RETRYING"                    # 有步骤处于可重试的失败状态
    COMPENSATING = "COMPENSATING"            # 正在执行 Saga 补偿（反向业务动作）
    COMPLETED = "COMPLETED"                  # 终态：全部成功
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"      # 终态：关键步骤成功，可选步骤失败
    FAILED = "FAILED"                        # 终态：失败且已收场
    CANCELLED = "CANCELLED"                  # 终态：被人工取消
    MANUAL_REVIEW = "MANUAL_REVIEW"          # 终态（人工接管）：程序无法自行决定


#: 任务终态集合。恢复扫描只捞「非终态」的任务，所以这个集合的正确性直接决定了
#: 会不会出现「已完成的任务被反复恢复」或「悬挂任务永远没人管」。
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.PARTIAL_SUCCESS,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.MANUAL_REVIEW,
    }
)


class StepStatus(StrEnum):
    """步骤级状态。

    这里最关键的一条设计：`TIMEOUT` 和 `UNKNOWN` **不是** `FAILED`。

    超时的真相有三种：请求根本没到达对方、已经执行成功只是响应没回来、对方还在处理中。
    如果把超时当失败处理并回滚，而外部系统其实已经执行了，就会凭空少一笔钱；
    如果当成功处理，而外部其实没执行，库里记着「已完成」钱却还在。
    唯一正确的处置是落成 UNKNOWN，拿幂等键去外部系统对账，查清楚再落定。
    """

    PENDING = "PENDING"                      # 已登记，前置步骤尚未完成
    READY = "READY"                          # 前置全部 SUCCESS，可以开始
    RUNNING = "RUNNING"                      # 已登记执行意图，正在执行
    SUCCESS = "SUCCESS"                      # 外部系统明确返回成功
    FAILED = "FAILED"                        # 明确失败（余额不足、参数非法、权限不足…）
    TIMEOUT = "TIMEOUT"                      # 超时：结果未知，等待对账
    UNKNOWN = "UNKNOWN"                      # 执行状态未知（进程崩溃 / 连接中断）
    WAITING_APPROVAL = "WAITING_APPROVAL"    # 挂起等待人工审批
    RETRY_SCHEDULED = "RETRY_SCHEDULED"      # 已安排重试，等待退避窗口
    COMPENSATING = "COMPENSATING"            # 正在补偿
    COMPENSATED = "COMPENSATED"              # 补偿完成
    SKIPPED = "SKIPPED"                      # 被跳过（条件分支未命中 / 前置失败）


#: 步骤终态。恢复时遇到这些状态直接跳过，不重做、也不重新问模型。
TERMINAL_STEP_STATUSES: frozenset[StepStatus] = frozenset(
    {
        StepStatus.SUCCESS,
        StepStatus.COMPENSATED,
        StepStatus.SKIPPED,
    }
)

#: 「可被 Runtime 推进」的步骤状态。
#:
#: 注意 **FAILED 不在其中**：一个步骤失败之后该怎么办（重试 / 补偿 / 跳过 / 终止），
#: 是在它失败的那一刻就决定好的，不是留给下一轮循环去猜。
#: 如果把 FAILED 也算作可推进，`_drive` 会反复捞起同一个已经判定为
#: 「不可重试」的步骤，形成死循环——而且第一轮就会撞上非法状态转换。
#:
#: 同理 RUNNING 也不在其中：它要么正在被本进程执行，
#: 要么是崩溃留下的悬挂记录，后者由恢复流程标记为 UNKNOWN 后再走对账。
ACTIONABLE_STEP_STATUSES: frozenset[StepStatus] = frozenset(
    {
        StepStatus.PENDING,
        StepStatus.READY,
        StepStatus.RETRY_SCHEDULED,
        StepStatus.TIMEOUT,
        StepStatus.UNKNOWN,
    }
)

#: 「结果未知」的状态集合。这类步骤**绝对不能**直接重试，必须先对账。
UNRESOLVED_STEP_STATUSES: frozenset[StepStatus] = frozenset(
    {
        StepStatus.TIMEOUT,
        StepStatus.UNKNOWN,
    }
)

#: 「正在等待外部条件」的步骤状态。遇到它们 Runtime 应当挂起而不是收尾——
#: 否则一个还在等审批的任务会被误判为「所有步骤都处理完了」。
BLOCKING_STEP_STATUSES: frozenset[StepStatus] = frozenset(
    {
        StepStatus.RUNNING,
        StepStatus.WAITING_APPROVAL,
        StepStatus.COMPENSATING,
    }
)


class StepType(StrEnum):
    """步骤类型。

    区分读 / 写非常重要：**只有写操作需要幂等键和对账**，读操作重试一百次也没有副作用。
    把这个信息放在步骤上（而不是靠工具名字硬猜），恢复逻辑才能自动做正确的事。
    """

    READ = "READ"                # 只读，可安全重试
    WRITE = "WRITE"              # 有副作用，必须幂等 + 可对账
    NOTIFY = "NOTIFY"            # 对外通知，不可撤回（所以排在链路最后）
    COMPUTE = "COMPUTE"          # 纯计算，无外部副作用
    COMPENSATION = "COMPENSATION"  # 补偿动作本身也是一个独立步骤，有独立状态和审计


class TaskEvent(StrEnum):
    """驱动任务状态机的事件。

    状态机的入参是 `(当前状态, 事件)`，而不是「目标状态」。
    这样调用方无法直接指定目标状态，只能声明「发生了什么」，
    真正的状态推导集中在状态机里——这就是「状态转换必须集中管理」的落地方式。
    """

    START_PLANNING = "START_PLANNING"
    PLAN_READY = "PLAN_READY"
    STEP_SUCCEEDED = "STEP_SUCCEEDED"
    NEED_APPROVAL = "NEED_APPROVAL"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    SCHEDULE_RETRY = "SCHEDULE_RETRY"
    RETRY_RESUMED = "RETRY_RESUMED"
    START_COMPENSATION = "START_COMPENSATION"
    COMPENSATION_DONE = "COMPENSATION_DONE"
    ALL_STEPS_DONE = "ALL_STEPS_DONE"
    PARTIALLY_DONE = "PARTIALLY_DONE"
    FATAL_ERROR = "FATAL_ERROR"
    CANCEL = "CANCEL"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    RESUME = "RESUME"


class StepEvent(StrEnum):
    """驱动步骤状态机的事件。"""

    MARK_READY = "MARK_READY"
    START_EXECUTION = "START_EXECUTION"
    EXECUTION_SUCCEEDED = "EXECUTION_SUCCEEDED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"
    RECONCILED_SUCCESS = "RECONCILED_SUCCESS"
    RECONCILED_FAILED = "RECONCILED_FAILED"
    NEED_APPROVAL = "NEED_APPROVAL"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    SCHEDULE_RETRY = "SCHEDULE_RETRY"
    RETRY_STARTED = "RETRY_STARTED"
    START_COMPENSATION = "START_COMPENSATION"
    COMPENSATION_SUCCEEDED = "COMPENSATION_SUCCEEDED"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"
    SKIP = "SKIP"


class RiskLevel(StrEnum):
    """风险等级。

    定义了 `order` 以支持比较：风险聚合时取「最高风险」，而不是最后一个策略说了算。
    """

    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def order(self) -> int:
        """返回可比较的数值序，用于「取最高风险」。"""
        return _RISK_ORDER[self]

    # ------------------------------------------------------------------
    # 必须把四个比较运算**全部**重写。
    #
    # 这里有一个非常容易踩的坑：StrEnum 继承自 str，而 str 自带一整套
    # 字典序比较。如果只重写 `__lt__`，那么 `max(HIGH, MEDIUM)` 会走
    # str 的 `__gt__`，按字典序判定 "MEDIUM" > "HIGH" → 返回 MEDIUM。
    # 于是「取最高风险」变成了「取字母序最大的风险」，
    # 一个 HIGH 风险的写操作会被静默降级成 MEDIUM，从而绕过审批阈值。
    #
    # 这类 Bug 不会报错、不会有异常栈，只会让审批悄悄失效——
    # 正是控制层最典型的失效方式：不报错，只是安静地造成损失。
    # ------------------------------------------------------------------
    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, RiskLevel):
            return self.order < other.order
        return NotImplemented

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, RiskLevel):
            return self.order <= other.order
        return NotImplemented

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, RiskLevel):
            return self.order > other.order
        return NotImplemented

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, RiskLevel):
            return self.order >= other.order
        return NotImplemented


_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.NONE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


class DecisionType(StrEnum):
    """控制层的最终裁决类型。

    注意 `ALLOW` 是**最弱**的决策：任何一条策略给出更强的决策都会覆盖它。
    优先级见 `app.control.policy_engine.DECISION_PRIORITY`。
    """

    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    RETRY = "RETRY"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class ApprovalStatus(StrEnum):
    """审批单状态。

    `EXPIRED` 必须存在：否则「等审批的任务」会永远悬着，没有终态。
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class ApprovalType(StrEnum):
    """审批类型：决定该找谁批。"""

    NONE = "NONE"
    MANAGER = "MANAGER"          # 经理审批（例如 5%~15% 折扣）
    COMPLIANCE = "COMPLIANCE"    # 合规审批
    SECURITY = "SECURITY"        # 安全审批


class ToolExecutionStatus(StrEnum):
    """单次工具执行的结果状态。

    和 StepStatus 分开的原因：一个步骤可能包含**多次**工具执行（重试），
    每次执行都要有独立记录，否则「重试了几次、每次错在哪」这种问题事后查不出来。
    """

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"
    SKIPPED_IDEMPOTENT = "SKIPPED_IDEMPOTENT"   # 幂等命中，直接返回历史结果
    IN_FLIGHT = "IN_FLIGHT"                     # 执行前占位，用于崩溃后识别悬挂记录


class CompensationStatus(StrEnum):
    """补偿状态。

    请特别注意：**数据库事务回滚 ≠ 业务补偿**（详见 `app/actions/compensation.py` 的注释）。
    """

    NOT_REQUIRED = "NOT_REQUIRED"
    REQUIRED = "REQUIRED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPENSATED = "COMPENSATED"
    FAILED = "FAILED"
    NOT_SUPPORTED = "NOT_SUPPORTED"   # 动作不可逆（例如已发出的短信）


class ActorType(StrEnum):
    """审计事件的发起者类型。

    区分 USER / AGENT / LLM / SYSTEM / TOOL 非常关键：
    审计要能回答「这一步到底是人干的、程序干的、还是模型建议的」。
    """

    USER = "USER"
    AGENT = "AGENT"
    LLM = "LLM"
    SYSTEM = "SYSTEM"
    TOOL = "TOOL"
    APPROVER = "APPROVER"


class AuditEventType(StrEnum):
    """审计事件类型。覆盖架构文档要求的全部关键动作。"""

    TASK_CREATED = "TASK_CREATED"
    REQUEST_RECEIVED = "REQUEST_RECEIVED"
    CONTEXT_BUILT = "CONTEXT_BUILT"
    LLM_CALL_STARTED = "LLM_CALL_STARTED"
    LLM_CALL_FINISHED = "LLM_CALL_FINISHED"
    LLM_CALL_FAILED = "LLM_CALL_FAILED"
    PROPOSAL_GENERATED = "PROPOSAL_GENERATED"
    POLICY_DECISION = "POLICY_DECISION"
    TOOL_EXECUTION_STARTED = "TOOL_EXECUTION_STARTED"
    TOOL_EXECUTION_FINISHED = "TOOL_EXECUTION_FINISHED"
    STATE_TRANSITION = "STATE_TRANSITION"
    ILLEGAL_STATE_TRANSITION = "ILLEGAL_STATE_TRANSITION"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    RECONCILIATION = "RECONCILIATION"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_DECIDED = "APPROVAL_DECIDED"
    COMPENSATION_STARTED = "COMPENSATION_STARTED"
    COMPENSATION_FINISHED = "COMPENSATION_FINISHED"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"
    TASK_RESUMED = "TASK_RESUMED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_CANCELLED = "TASK_CANCELLED"
    SUB_AGENT_FINISHED = "SUB_AGENT_FINISHED"


class AgentResultStatus(StrEnum):
    """子 Agent 对上层编排器暴露的统一状态。

    这就是「嵌套 Agent 的对外契约」：内部可以有自己的 LLM、工具、状态机和子工作流，
    但对外只暴露这几个状态。这样子 Agent 之后被替换成一段纯规则代码，上层完全无感。
    """

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SKIPPED = "SKIPPED"


class ErrorCode(StrEnum):
    """机器可读的错误码。

    为什么必须有错误码，而不是只有 error_message：
    **错误码决定走哪条路**（重试 / 回认知层 / 补偿 / 转人工），
    而 message 只是给人看的。用字符串匹配 message 来决定重试策略，
    是一种一改文案就全线崩溃的耦合。
    """

    # —— 不可重试：业务语义明确，重试一万次也是同一个结果 ——
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    BUSINESS_RULE_VIOLATION = "BUSINESS_RULE_VIOLATION"
    DUPLICATE_ACTIVE_DISCOUNT = "DUPLICATE_ACTIVE_DISCOUNT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    TOOL_NOT_REGISTERED = "TOOL_NOT_REGISTERED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    POLICY_DENIED = "POLICY_DENIED"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"

    # —— 可重试：瞬时故障 ——
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_ERROR = "NETWORK_ERROR"
    TIMEOUT = "TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # —— 状态未知：既不能重试也不能当失败，必须先对账 ——
    UNKNOWN_EXECUTION_STATE = "UNKNOWN_EXECUTION_STATE"

    # —— 流程控制 ——
    ILLEGAL_STATE_TRANSITION = "ILLEGAL_STATE_TRANSITION"
    LLM_OUTPUT_INVALID = "LLM_OUTPUT_INVALID"
    LLM_PROVIDER_ERROR = "LLM_PROVIDER_ERROR"


#: 可重试错误白名单。
#: 用**白名单**而不是黑名单，是因为「未知错误默认不重试」比「未知错误默认重试」安全得多——
#: 对写操作来说，多重试一次可能就是多扣一笔钱。
RETRYABLE_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.UPSTREAM_UNAVAILABLE,
        ErrorCode.RATE_LIMITED,
        ErrorCode.NETWORK_ERROR,
        ErrorCode.INTERNAL_ERROR,
    }
)

#: 明确不可重试的错误黑名单（用于断言与文档目的；实际判定以白名单为准）。
NON_RETRYABLE_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.INVALID_ARGUMENT,
        ErrorCode.PERMISSION_DENIED,
        ErrorCode.NOT_FOUND,
        ErrorCode.BUSINESS_RULE_VIOLATION,
        ErrorCode.DUPLICATE_ACTIVE_DISCOUNT,
        ErrorCode.IDEMPOTENCY_CONFLICT,
        ErrorCode.TOOL_NOT_REGISTERED,
        ErrorCode.APPROVAL_REJECTED,
        ErrorCode.POLICY_DENIED,
        ErrorCode.UNSUPPORTED_OPERATION,
    }
)


def is_retryable(error_code: ErrorCode | str | None) -> bool:
    """判断某个错误码是否允许自动重试。

    Args:
        error_code: 错误码，可以是 :class:`ErrorCode`、等价字符串或 ``None``。

    Returns:
        ``True`` 表示允许自动重试。注意 ``TIMEOUT`` / ``UNKNOWN_EXECUTION_STATE``
        一律返回 ``False``——它们必须先走对账流程确认外部系统的真实状态，
        对账之后才可能被安全地重新执行。**超时不等于失败，也不等于可以直接重试。**
    """
    if error_code is None:
        return False
    try:
        code = ErrorCode(error_code)
    except ValueError:
        # 无法识别的错误码一律不重试：默认保守，避免对写操作造成重复副作用。
        return False
    return code in RETRYABLE_ERROR_CODES
