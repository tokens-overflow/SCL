"""Saga 补偿管理器。

═══════════════════════════════════════════════════════════════════════════
**数据库事务回滚 ≠ 业务补偿**
═══════════════════════════════════════════════════════════════════════════

这是本模块最重要的一句话，值得单独框起来。

* **事务回滚**：撤销的是「我们自己这个事务里、还没提交的写入」。
  它由数据库保证，是原子的、瞬时的、必定成功的。
  一旦事务提交，回滚就不可能了。

* **业务补偿**：撤销的是「已经提交、已经对外生效的业务动作」。
  它是一次**新的正向业务动作**——发一笔退款、撤一张单据、释放一笔预算。
  它有自己的执行时间、自己的状态、自己的幂等键、自己的审计记录，
  而且**它自己也会失败**。

把两者混为一谈的后果非常具体：你写了 `try/except: session.rollback()`，
以为「出错了就撤销」，但外部系统那笔折扣已经生效了，rollback 对它毫无作用。
库里干干净净，客户那边白拿了一个折扣，而且没有任何记录。

───────────────────────────────────────────────────────────────────────────
**补偿的三条纪律**

1. **补偿动作本身必须幂等。** 补偿也会失败、也会重试，
   它不比正向动作安全。所以补偿用独立的幂等键（原键 + ``:comp``）。

2. **不可补偿的动作排在链路最后。** 发通知、发短信、对外公告这类
   收不回的动作放前面，后面任何一步失败你都收不了场。

3. **补偿不给模型调用权。** 它由 Runtime 在失败路径上调用。
   让模型能自由触发「撤销」，等于给了它一把可以来回拨的开关——
   而「是否撤销一笔已生效的业务」必须由明确规则或人来决定。

───────────────────────────────────────────────────────────────────────────
**关于「恰好一次」**

分布式系统里不存在恰好一次。能真正做到的是
**「至多一次副作用 + 可判定的最终状态」**：
每个副作用带幂等键保证不重复；每个任务有明确终态
（COMPLETED / FAILED / COMPENSATED / MANUAL_REVIEW）保证不会悬着。
"""

from __future__ import annotations

from typing import Any

from app.actions.base import AgentTool, ToolExecutionContext, ToolExecutionResult
from app.actions.registry import ToolRegistry
from app.core.enums import (
    CompensationStatus,
    ErrorCode,
    StepEvent,
    StepStatus,
    ToolExecutionStatus,
)
from app.core.ids import build_idempotency_key, new_execution_id
from app.operations.audit import AuditService
from app.operations.logging import get_logger
from app.operations.metrics import metrics
from app.runtime.models import AgentTask, TaskStep
from app.runtime.state_machine import step_state_machine
from app.state.repositories import TaskRepository

logger = get_logger(__name__)


class CompensationResult:
    """一次补偿流程的汇总结果。

    Attributes:
        compensated: 成功补偿的步骤名。
        failed: 补偿失败的步骤名（**需要人工跟进**）。
        not_supported: 不可补偿的步骤名（**需要人工善后**）。
    """

    __slots__ = ("compensated", "failed", "not_supported")

    def __init__(self) -> None:
        self.compensated: list[str] = []
        self.failed: list[str] = []
        self.not_supported: list[str] = []

    @property
    def needs_manual_followup(self) -> bool:
        """是否需要人工跟进。

        补偿失败或存在不可补偿动作时为 True。此时任务应落 MANUAL_REVIEW，
        **而不是 FAILED**——FAILED 意味着「已经收场了」，
        但这里明明还有东西没收拾干净。
        """
        return bool(self.failed or self.not_supported)

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "compensated": self.compensated,
            "failed": self.failed,
            "not_supported": self.not_supported,
            "needs_manual_followup": self.needs_manual_followup,
        }


class CompensationManager:
    """按 Saga 模式逆序执行补偿。

    Args:
        registry: 工具注册表。
        task_repo: 任务仓库。
        audit: 审计服务。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        task_repo: TaskRepository,
        audit: AuditService,
    ) -> None:
        self.registry = registry
        self.task_repo = task_repo
        self.audit = audit

    async def compensate_task(
        self,
        task: AgentTask,
        *,
        session: Any,
        upto_sequence: int | None = None,
        reason: str = "",
    ) -> CompensationResult:
        """对任务已成功的副作用步骤执行补偿。

        Args:
            task: 目标任务。
            session: 数据库会话。
            upto_sequence: 只补偿 sequence 小于该值的步骤（缺省全部）。
            reason: 补偿原因，写入审计。

        Returns:
            :class:`CompensationResult`。

        Note:
            **逆序执行**：后做的先撤。
            例子：冻结预算 → 创建采购单 → 提交订单失败，
            补偿顺序必须是「取消采购单 → 释放预算」。
            反过来先释放预算，采购单就成了没有预算支撑的孤儿单据。
        """
        result = CompensationResult()
        candidates = task.completed_side_effect_steps()
        if upto_sequence is not None:
            candidates = [s for s in candidates if s.sequence < upto_sequence]

        # 逆序：后做的先撤。
        for step in reversed(candidates):
            await self._compensate_step(task, step, session=session, result=result, reason=reason)

        logger.info(
            "compensation_finished",
            task_id=task.task_id,
            compensated=result.compensated,
            failed=result.failed,
            not_supported=result.not_supported,
        )
        return result

    async def _compensate_step(
        self,
        task: AgentTask,
        step: TaskStep,
        *,
        session: Any,
        result: CompensationResult,
        reason: str,
    ) -> None:
        if not step.tool_name:
            return

        tool: AgentTool = self.registry.get(step.tool_name)

        # —— 不可补偿：如实标记，转人工跟进 ——
        # 绝不假装补偿成功。「不能简单假设所有动作都可逆」。
        if not tool.supports_compensation:
            await self.task_repo.update_step(
                step.step_id, compensation_status=CompensationStatus.NOT_SUPPORTED
            )
            result.not_supported.append(step.step_name)
            await self.audit.compensation(
                task_id=task.task_id,
                step_id=step.step_id,
                trace_id=task.trace_id,
                tool_name=step.tool_name,
                started=False,
                payload={
                    "outcome": "NOT_SUPPORTED",
                    "reason": f"{tool.name} 是不可逆动作（如已发出的通知），需人工善后",
                },
            )
            logger.warning(
                "compensation_not_supported",
                task_id=task.task_id,
                step_name=step.step_name,
                tool_name=step.tool_name,
            )
            return

        await self.audit.compensation(
            task_id=task.task_id,
            step_id=step.step_id,
            trace_id=task.trace_id,
            tool_name=step.tool_name,
            started=True,
            payload={"reason": reason, "original_ref": step.external_reference_id},
        )

        # 状态机：SUCCESS → COMPENSATING。
        # 补偿有**独立的状态**，不是把步骤改回 FAILED 了事。
        new_status = step_state_machine.transition(step.status, StepEvent.START_COMPENSATION)
        await self.task_repo.update_step(
            step.step_id,
            status=new_status,
            compensation_status=CompensationStatus.IN_PROGRESS,
        )

        # 补偿动作有**自己的幂等键**：原键 + ":comp" 后缀。
        # 这样正向动作和补偿动作互不干扰，补偿本身也天然幂等。
        comp_key = build_idempotency_key(
            task_id=task.task_id,
            step_name=step.step_name,
            tool_name=step.tool_name,
            arguments=step.input_payload or {},
            suffix="comp",
        )
        exec_ctx = ToolExecutionContext(
            task_id=task.task_id,
            step_id=step.step_id,
            step_name=f"{step.step_name}__compensate",
            execution_id=new_execution_id(),
            idempotency_key=comp_key,
            trace_id=task.trace_id,
            user_id=task.user_id,
            agent_id=task.agent_id,
            session=session,
        )
        previous = ToolExecutionResult(
            tool_name=step.tool_name,
            execution_id=step.step_id,
            status=ToolExecutionStatus.SUCCESS,
            result=step.output_payload,
            external_reference_id=step.external_reference_id,
            idempotency_key=step.idempotency_key or "",
        )

        try:
            comp_result = await tool.compensate(previous, exec_ctx)
        except NotImplementedError:
            # 工具声明了 supports_compensation 但没实现——配置与实现不一致。
            comp_result = None
            outcome = "NOT_SUPPORTED"
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "compensation_crashed",
                task_id=task.task_id,
                step_name=step.step_name,
                tool_name=step.tool_name,
            )
            comp_result = ToolExecutionResult(
                tool_name=step.tool_name,
                execution_id=exec_ctx.execution_id,
                status=ToolExecutionStatus.FAILED,
                error_code=ErrorCode.INTERNAL_ERROR,
                error_message=f"{type(exc).__name__}: {exc}",
                idempotency_key=comp_key,
            )
            outcome = "FAILED"
        else:
            outcome = "SUCCESS" if comp_result.succeeded else "FAILED"

        if comp_result is not None and comp_result.succeeded:
            final_status = step_state_machine.transition(
                StepStatus.COMPENSATING, StepEvent.COMPENSATION_SUCCEEDED
            )
            await self.task_repo.update_step(
                step.step_id,
                status=final_status,
                compensation_status=CompensationStatus.COMPENSATED,
                output_payload={**(step.output_payload or {}), "compensation": comp_result.result},
            )
            result.compensated.append(step.step_name)
            metrics.increment("agent_compensations_total", tool=step.tool_name, outcome="success")
        else:
            # 补偿失败：回到 FAILED 并标记补偿失败，由 Runtime 升级为人工跟进。
            # **绝不静默吞掉**——否则系统会停在一个「补了一半」的状态里。
            final_status = step_state_machine.transition(
                StepStatus.COMPENSATING, StepEvent.COMPENSATION_FAILED
            )
            await self.task_repo.update_step(
                step.step_id,
                status=final_status,
                compensation_status=(
                    CompensationStatus.NOT_SUPPORTED
                    if outcome == "NOT_SUPPORTED"
                    else CompensationStatus.FAILED
                ),
                error_code=comp_result.error_code if comp_result else ErrorCode.UNSUPPORTED_OPERATION,
                error_message=(
                    comp_result.error_message if comp_result else "工具未实现补偿逻辑"
                ),
            )
            if outcome == "NOT_SUPPORTED":
                result.not_supported.append(step.step_name)
            else:
                result.failed.append(step.step_name)
            metrics.increment("agent_compensations_total", tool=step.tool_name, outcome="failed")

        await self.audit.compensation(
            task_id=task.task_id,
            step_id=step.step_id,
            trace_id=task.trace_id,
            tool_name=step.tool_name,
            started=False,
            payload={
                "outcome": outcome,
                "compensation_idempotency_key": comp_key,
                "external_reference_id": (
                    comp_result.external_reference_id if comp_result else None
                ),
            },
        )
