"""状态机：断点续跑的地基。

**为什么状态转换必须集中管理？**

因为状态是恢复逻辑的唯一依据。如果任何业务模块都能随手改状态，
就一定会出现「SUCCESS 被改回 PENDING」这类脏数据，
而恢复时看到 PENDING 就会重新执行——于是重复下单、重复付款、重复发通知。

把合法迁移写成**表**而不是散落的 if-else，好处有三个：

1. 它可以直接变成代码里的断言（本模块做的就是这件事）；
2. 它可以被测试穷举（见 `tests/unit/test_state_machine.py`）；
3. 它可以被人一眼读完——文档和实现是同一份东西，不会漂移。

非法转换一律抛异常并写审计。**不静默修正**：
静默修正会把一个明显的 Bug 变成一个隐蔽的数据不一致。
"""

from __future__ import annotations

from app.core.enums import StepEvent, StepStatus, TaskEvent, TaskStatus
from app.core.errors import IllegalStateTransitionError

#: 任务状态迁移表：``(当前状态, 事件) -> 目标状态``。
#:
#: 几条值得注意的设计：
#:
#: * ``WAITING_APPROVAL`` 既可以被批准（→ RUNNING）也可以被驳回（→ FAILED），
#:   还必须能被超时回收（→ MANUAL_REVIEW）。**没有超时那条，任务会永远悬着。**
#: * ``RUNNING`` 可以直接 ``PARTIALLY_DONE``：这是「折扣成功但通知失败」的落点，
#:   它是一个**正常的业务结局**，不是失败。
#: * 任何非终态都能被 ``CANCEL``：人工干预必须永远有一条出路。
TASK_TRANSITIONS: dict[tuple[TaskStatus, TaskEvent], TaskStatus] = {
    # 创建 → 规划
    (TaskStatus.CREATED, TaskEvent.START_PLANNING): TaskStatus.PLANNING,
    (TaskStatus.CREATED, TaskEvent.FATAL_ERROR): TaskStatus.FAILED,
    (TaskStatus.CREATED, TaskEvent.CANCEL): TaskStatus.CANCELLED,
    # 规划 → 运行
    (TaskStatus.PLANNING, TaskEvent.PLAN_READY): TaskStatus.RUNNING,
    (TaskStatus.PLANNING, TaskEvent.FATAL_ERROR): TaskStatus.FAILED,
    (TaskStatus.PLANNING, TaskEvent.ESCALATE_TO_HUMAN): TaskStatus.MANUAL_REVIEW,
    (TaskStatus.PLANNING, TaskEvent.CANCEL): TaskStatus.CANCELLED,
    # 运行中
    (TaskStatus.RUNNING, TaskEvent.STEP_SUCCEEDED): TaskStatus.RUNNING,
    (TaskStatus.RUNNING, TaskEvent.NEED_APPROVAL): TaskStatus.WAITING_APPROVAL,
    (TaskStatus.RUNNING, TaskEvent.SCHEDULE_RETRY): TaskStatus.RETRYING,
    (TaskStatus.RUNNING, TaskEvent.START_COMPENSATION): TaskStatus.COMPENSATING,
    (TaskStatus.RUNNING, TaskEvent.ALL_STEPS_DONE): TaskStatus.COMPLETED,
    (TaskStatus.RUNNING, TaskEvent.PARTIALLY_DONE): TaskStatus.PARTIAL_SUCCESS,
    (TaskStatus.RUNNING, TaskEvent.FATAL_ERROR): TaskStatus.FAILED,
    (TaskStatus.RUNNING, TaskEvent.ESCALATE_TO_HUMAN): TaskStatus.MANUAL_REVIEW,
    (TaskStatus.RUNNING, TaskEvent.CANCEL): TaskStatus.CANCELLED,
    (TaskStatus.RUNNING, TaskEvent.RESUME): TaskStatus.RUNNING,
    # 等待审批：三条出路缺一不可
    (TaskStatus.WAITING_APPROVAL, TaskEvent.APPROVAL_GRANTED): TaskStatus.RUNNING,
    (TaskStatus.WAITING_APPROVAL, TaskEvent.APPROVAL_REJECTED): TaskStatus.FAILED,
    (TaskStatus.WAITING_APPROVAL, TaskEvent.ESCALATE_TO_HUMAN): TaskStatus.MANUAL_REVIEW,
    (TaskStatus.WAITING_APPROVAL, TaskEvent.CANCEL): TaskStatus.CANCELLED,
    (TaskStatus.WAITING_APPROVAL, TaskEvent.RESUME): TaskStatus.WAITING_APPROVAL,
    # 重试中
    (TaskStatus.RETRYING, TaskEvent.RETRY_RESUMED): TaskStatus.RUNNING,
    (TaskStatus.RETRYING, TaskEvent.RESUME): TaskStatus.RUNNING,
    (TaskStatus.RETRYING, TaskEvent.START_COMPENSATION): TaskStatus.COMPENSATING,
    (TaskStatus.RETRYING, TaskEvent.FATAL_ERROR): TaskStatus.FAILED,
    (TaskStatus.RETRYING, TaskEvent.ESCALATE_TO_HUMAN): TaskStatus.MANUAL_REVIEW,
    (TaskStatus.RETRYING, TaskEvent.CANCEL): TaskStatus.CANCELLED,
    # 补偿中
    (TaskStatus.COMPENSATING, TaskEvent.COMPENSATION_DONE): TaskStatus.FAILED,
    (TaskStatus.COMPENSATING, TaskEvent.ESCALATE_TO_HUMAN): TaskStatus.MANUAL_REVIEW,
    (TaskStatus.COMPENSATING, TaskEvent.FATAL_ERROR): TaskStatus.MANUAL_REVIEW,
    (TaskStatus.COMPENSATING, TaskEvent.RESUME): TaskStatus.COMPENSATING,
}

#: 步骤状态迁移表。
#:
#: 最关键的两组：
#:
#: * ``RUNNING → TIMEOUT / UNKNOWN``：**超时和崩溃不是失败**，
#:   它们只意味着「结果未知」，必须先对账。
#: * ``TIMEOUT/UNKNOWN → SUCCESS/FAILED``：**只能通过 RECONCILED_* 事件到达**。
#:   也就是说，一个未知状态只能被「查明真相」这一个动作落定，
#:   不能被「重试成功了」或「我猜它失败了」落定。
STEP_TRANSITIONS: dict[tuple[StepStatus, StepEvent], StepStatus] = {
    (StepStatus.PENDING, StepEvent.MARK_READY): StepStatus.READY,
    (StepStatus.PENDING, StepEvent.SKIP): StepStatus.SKIPPED,
    (StepStatus.PENDING, StepEvent.NEED_APPROVAL): StepStatus.WAITING_APPROVAL,
    (StepStatus.PENDING, StepEvent.EXECUTION_FAILED): StepStatus.FAILED,
    (StepStatus.PENDING, StepEvent.START_EXECUTION): StepStatus.RUNNING,
    (StepStatus.READY, StepEvent.START_EXECUTION): StepStatus.RUNNING,
    (StepStatus.READY, StepEvent.NEED_APPROVAL): StepStatus.WAITING_APPROVAL,
    (StepStatus.READY, StepEvent.SKIP): StepStatus.SKIPPED,
    (StepStatus.READY, StepEvent.EXECUTION_FAILED): StepStatus.FAILED,
    # 执行中的四种结局：成功、明确失败、超时、未知
    (StepStatus.RUNNING, StepEvent.EXECUTION_SUCCEEDED): StepStatus.SUCCESS,
    (StepStatus.RUNNING, StepEvent.EXECUTION_FAILED): StepStatus.FAILED,
    (StepStatus.RUNNING, StepEvent.EXECUTION_TIMEOUT): StepStatus.TIMEOUT,
    (StepStatus.RUNNING, StepEvent.EXECUTION_UNKNOWN): StepStatus.UNKNOWN,
    (StepStatus.RUNNING, StepEvent.NEED_APPROVAL): StepStatus.WAITING_APPROVAL,
    # 未知状态只能被对账落定
    (StepStatus.TIMEOUT, StepEvent.RECONCILED_SUCCESS): StepStatus.SUCCESS,
    (StepStatus.TIMEOUT, StepEvent.RECONCILED_FAILED): StepStatus.FAILED,
    (StepStatus.TIMEOUT, StepEvent.SCHEDULE_RETRY): StepStatus.RETRY_SCHEDULED,
    (StepStatus.UNKNOWN, StepEvent.RECONCILED_SUCCESS): StepStatus.SUCCESS,
    (StepStatus.UNKNOWN, StepEvent.RECONCILED_FAILED): StepStatus.FAILED,
    (StepStatus.UNKNOWN, StepEvent.SCHEDULE_RETRY): StepStatus.RETRY_SCHEDULED,
    (StepStatus.UNKNOWN, StepEvent.EXECUTION_FAILED): StepStatus.FAILED,
    # 失败后的两条路：重试 或 补偿
    (StepStatus.FAILED, StepEvent.SCHEDULE_RETRY): StepStatus.RETRY_SCHEDULED,
    (StepStatus.FAILED, StepEvent.START_COMPENSATION): StepStatus.COMPENSATING,
    (StepStatus.FAILED, StepEvent.SKIP): StepStatus.SKIPPED,
    (StepStatus.RETRY_SCHEDULED, StepEvent.RETRY_STARTED): StepStatus.RUNNING,
    (StepStatus.RETRY_SCHEDULED, StepEvent.EXECUTION_FAILED): StepStatus.FAILED,
    (StepStatus.RETRY_SCHEDULED, StepEvent.SKIP): StepStatus.SKIPPED,
    # 审批
    (StepStatus.WAITING_APPROVAL, StepEvent.APPROVAL_GRANTED): StepStatus.READY,
    (StepStatus.WAITING_APPROVAL, StepEvent.APPROVAL_REJECTED): StepStatus.FAILED,
    (StepStatus.WAITING_APPROVAL, StepEvent.SKIP): StepStatus.SKIPPED,
    # 补偿：成功的步骤也可能需要被补偿（这正是 Saga 的核心）
    (StepStatus.SUCCESS, StepEvent.START_COMPENSATION): StepStatus.COMPENSATING,
    (StepStatus.COMPENSATING, StepEvent.COMPENSATION_SUCCEEDED): StepStatus.COMPENSATED,
    # 补偿失败 → 回到 FAILED，由 Runtime 升级为人工跟进。
    # 补偿也会失败，它不比正向动作安全。
    (StepStatus.COMPENSATING, StepEvent.COMPENSATION_FAILED): StepStatus.FAILED,
}


class TaskStateMachine:
    """任务状态机。

    Example:
        >>> sm = TaskStateMachine()
        >>> sm.transition(TaskStatus.CREATED, TaskEvent.START_PLANNING)
        <TaskStatus.PLANNING: 'PLANNING'>
    """

    def transition(self, current_status: TaskStatus, event: TaskEvent) -> TaskStatus:
        """执行一次状态转换。

        Args:
            current_status: 当前状态。
            event: 发生的事件。

        Returns:
            目标状态。

        Raises:
            IllegalStateTransitionError: 该状态下不允许这个事件。
                调用方**必须**在捕获后写一条 ILLEGAL_STATE_TRANSITION 审计——
                非法转换往往意味着代码里有并发或逻辑 Bug，
                静默吞掉它等于放弃了唯一的发现机会。
        """
        key = (TaskStatus(current_status), TaskEvent(event))
        target = TASK_TRANSITIONS.get(key)
        if target is None:
            raise IllegalStateTransitionError(str(current_status), str(event), scope="task")
        return target

    def can_transition(self, current_status: TaskStatus, event: TaskEvent) -> bool:
        """判断某次转换是否合法（不抛异常）。"""
        return (TaskStatus(current_status), TaskEvent(event)) in TASK_TRANSITIONS

    def allowed_events(self, current_status: TaskStatus) -> list[TaskEvent]:
        """列出当前状态下允许的全部事件（用于调试与 API 展示）。"""
        return [
            event for (status, event) in TASK_TRANSITIONS if status == TaskStatus(current_status)
        ]


class StepStateMachine:
    """步骤状态机。接口与 :class:`TaskStateMachine` 一致。"""

    def transition(self, current_status: StepStatus, event: StepEvent) -> StepStatus:
        """执行一次步骤状态转换。

        Raises:
            IllegalStateTransitionError: 该状态下不允许这个事件。

        Note:
            特别注意：``SUCCESS`` 状态下**只允许** ``START_COMPENSATION``。
            也就是说，一个已经成功的步骤不可能被重新执行——
            这是防重复副作用的最后一道语义闸门。
        """
        key = (StepStatus(current_status), StepEvent(event))
        target = STEP_TRANSITIONS.get(key)
        if target is None:
            raise IllegalStateTransitionError(str(current_status), str(event), scope="step")
        return target

    def can_transition(self, current_status: StepStatus, event: StepEvent) -> bool:
        """判断某次转换是否合法（不抛异常）。"""
        return (StepStatus(current_status), StepEvent(event)) in STEP_TRANSITIONS

    def allowed_events(self, current_status: StepStatus) -> list[StepEvent]:
        """列出当前状态下允许的全部事件。"""
        return [
            event for (status, event) in STEP_TRANSITIONS if status == StepStatus(current_status)
        ]


#: 进程级单例。状态机本身无状态，可安全共享。
task_state_machine = TaskStateMachine()
step_state_machine = StepStateMachine()
