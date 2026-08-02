"""状态机单元测试。

重点验证「非法状态转换必须抛出明确异常」——
静默修正会把一个明显的 Bug 变成一个隐蔽的数据不一致。
"""

from __future__ import annotations

import pytest

from app.core.enums import StepEvent, StepStatus, TaskEvent, TaskStatus
from app.core.errors import IllegalStateTransitionError
from app.runtime.state_machine import (
    TASK_TRANSITIONS,
    StepStateMachine,
    TaskStateMachine,
)


class TestTaskStateMachine:
    def test_normal_flow(self) -> None:
        sm = TaskStateMachine()
        assert sm.transition(TaskStatus.CREATED, TaskEvent.START_PLANNING) == TaskStatus.PLANNING
        assert sm.transition(TaskStatus.PLANNING, TaskEvent.PLAN_READY) == TaskStatus.RUNNING
        assert sm.transition(TaskStatus.RUNNING, TaskEvent.ALL_STEPS_DONE) == TaskStatus.COMPLETED

    def test_partial_success_is_a_normal_outcome(self) -> None:
        """折扣成功 + 通知失败 → PARTIAL_SUCCESS，这是正常结局不是失败。"""
        sm = TaskStateMachine()
        assert (
            sm.transition(TaskStatus.RUNNING, TaskEvent.PARTIALLY_DONE)
            == TaskStatus.PARTIAL_SUCCESS
        )

    def test_approval_has_three_exits(self) -> None:
        """等待审批必须有三条出路，否则任务会永远悬着。"""
        sm = TaskStateMachine()
        assert sm.transition(TaskStatus.WAITING_APPROVAL, TaskEvent.APPROVAL_GRANTED) == TaskStatus.RUNNING
        assert sm.transition(TaskStatus.WAITING_APPROVAL, TaskEvent.APPROVAL_REJECTED) == TaskStatus.FAILED
        assert (
            sm.transition(TaskStatus.WAITING_APPROVAL, TaskEvent.ESCALATE_TO_HUMAN)
            == TaskStatus.MANUAL_REVIEW
        )

    def test_illegal_transition_raises(self) -> None:
        """已完成的任务不能被重新规划。"""
        sm = TaskStateMachine()
        with pytest.raises(IllegalStateTransitionError) as exc:
            sm.transition(TaskStatus.COMPLETED, TaskEvent.START_PLANNING)
        assert exc.value.error_code == "ILLEGAL_STATE_TRANSITION"
        assert exc.value.details["current_status"] == "COMPLETED"
        assert exc.value.retryable is False

    def test_terminal_states_have_no_outgoing_transitions(self) -> None:
        """终态不能再迁出——否则「终态」这个概念就没有意义了。"""
        from app.core.enums import TERMINAL_TASK_STATUSES

        for status in TERMINAL_TASK_STATUSES:
            outgoing = [k for k in TASK_TRANSITIONS if k[0] == status]
            assert outgoing == [], f"终态 {status} 不应有迁出转换：{outgoing}"

    def test_can_transition_does_not_raise(self) -> None:
        sm = TaskStateMachine()
        assert sm.can_transition(TaskStatus.CREATED, TaskEvent.START_PLANNING) is True
        assert sm.can_transition(TaskStatus.COMPLETED, TaskEvent.START_PLANNING) is False


class TestStepStateMachine:
    def test_timeout_is_not_failure(self) -> None:
        """超时落 TIMEOUT，不是 FAILED —— 这是最重要的一条。"""
        sm = StepStateMachine()
        assert sm.transition(StepStatus.RUNNING, StepEvent.EXECUTION_TIMEOUT) == StepStatus.TIMEOUT
        assert sm.transition(StepStatus.RUNNING, StepEvent.EXECUTION_UNKNOWN) == StepStatus.UNKNOWN

    def test_unknown_can_only_be_settled_by_reconciliation(self) -> None:
        """未知状态只能被对账落定，不能被「我猜它失败了」落定。"""
        sm = StepStateMachine()
        assert sm.transition(StepStatus.TIMEOUT, StepEvent.RECONCILED_SUCCESS) == StepStatus.SUCCESS
        assert sm.transition(StepStatus.TIMEOUT, StepEvent.RECONCILED_FAILED) == StepStatus.FAILED
        # TIMEOUT 不能直接标成成功
        with pytest.raises(IllegalStateTransitionError):
            sm.transition(StepStatus.TIMEOUT, StepEvent.EXECUTION_SUCCEEDED)

    def test_success_can_only_be_compensated(self) -> None:
        """成功的步骤不能被重新执行——这是防重复副作用的最后一道语义闸门。"""
        sm = StepStateMachine()
        assert (
            sm.transition(StepStatus.SUCCESS, StepEvent.START_COMPENSATION)
            == StepStatus.COMPENSATING
        )
        with pytest.raises(IllegalStateTransitionError):
            sm.transition(StepStatus.SUCCESS, StepEvent.START_EXECUTION)
        with pytest.raises(IllegalStateTransitionError):
            sm.transition(StepStatus.SUCCESS, StepEvent.EXECUTION_FAILED)

    def test_compensation_can_fail(self) -> None:
        """补偿也会失败，它不比正向动作安全。"""
        sm = StepStateMachine()
        assert (
            sm.transition(StepStatus.COMPENSATING, StepEvent.COMPENSATION_SUCCEEDED)
            == StepStatus.COMPENSATED
        )
        assert (
            sm.transition(StepStatus.COMPENSATING, StepEvent.COMPENSATION_FAILED)
            == StepStatus.FAILED
        )
