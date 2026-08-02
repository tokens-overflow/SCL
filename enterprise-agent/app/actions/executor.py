"""Action Executor：工具执行的唯一入口。

**这是「工具不能绕过控制层被直接调用」这条规则的物理执行点。**

`execute()` 的签名要求传入一个 :class:`~app.control.models.PolicyDecision`，
并且会断言 `decision.allowed`。也就是说，在类型和运行时两个层面，
都不存在「不带裁决就调用工具」这条路径。模型手里从头到尾没有一个能触发副作用的入口。

执行流程（顺序至关重要）：

    1. 断言控制层已放行
    2. 生成幂等键（按动作，不按请求）
    3. **先占位**（写 IN_FLIGHT 记录）—— 这一步必须在执行之前
    4. 幂等命中检查（已成功 → 直接返回，不重复执行）
    5. 用 args_model 二次校验参数
    6. 带超时执行
    7. 落盘结果

第 3 步的顺序是最容易写错的地方。很多实现把「写去重记录」放在执行成功之后，
这留了一个致命窗口：如果在「执行成功」和「写记录」之间进程崩了，
去重记录就丢了，重试必然产生第二笔。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.actions.base import AgentTool, ToolExecutionContext, ToolExecutionResult
from app.actions.registry import ToolRegistry
from app.control.models import PolicyDecision
from app.control.parameter_validator import validate_tool_arguments
from app.core.enums import ErrorCode, StepType, ToolExecutionStatus
from app.core.errors import (
    AgentError,
    IdempotencyConflictError,
    ToolTimeoutError,
    ValidationError,
)
from app.core.ids import arguments_hash, build_idempotency_key, new_execution_id, utcnow
from app.operations.logging import get_logger
from app.operations.metrics import metrics
from app.operations.tracing import tracer
from app.runtime.models import TaskStep
from app.state.repositories import ToolExecutionRepository

logger = get_logger(__name__)


class ActionExecutor:
    """执行工具调用，负责幂等、超时与结果落盘。

    Args:
        registry: 工具注册表。
        execution_repo: 工具执行记录仓库（同时是幂等去重表）。
    """

    def __init__(self, registry: ToolRegistry, execution_repo: ToolExecutionRepository) -> None:
        self.registry = registry
        self.execution_repo = execution_repo

    def build_key(self, task_id: str, step: TaskStep, tool_name: str, arguments: dict[str, Any]) -> str:
        """生成这一步的幂等键。

        Note:
            键由 ``task_id + step_name + tool_name + 归一化参数`` 决定，
            **不含时间戳、不含请求 ID**。
            这样即使模型在第 3 步和第 7 步提出同一个动作，
            也会被识别为同一笔，不会真的执行两次。
        """
        return build_idempotency_key(
            task_id=task_id,
            step_name=step.step_name,
            tool_name=tool_name,
            arguments=arguments,
        )

    async def execute(
        self,
        *,
        task_id: str,
        step: TaskStep,
        tool_name: str,
        decision: PolicyDecision,
        session: Any,
        user_id: str = "",
        agent_id: str = "",
        trace_id: str = "",
        timeout_seconds: float | None = None,
    ) -> ToolExecutionResult:
        """执行一次工具调用。

        Args:
            task_id: 任务 ID。
            step: 目标步骤。
            tool_name: 工具名。
            decision: **控制层裁决**。必须是 ALLOW，且执行使用的是
                `decision.validated_arguments` 而不是模型的原始输出。
            session: 数据库会话。
            user_id / agent_id / trace_id: 关联标识。
            timeout_seconds: 超时时长，缺省用工具自己的默认值。

        Returns:
            :class:`ToolExecutionResult`。

        Raises:
            PermissionError: 控制层未放行却调用了本方法（编程错误）。
            IdempotencyConflictError: 同一幂等键但参数不同。
        """
        # —— 第 0 道：绝不执行未放行的动作 ——
        # 这是一个编程错误检查而不是业务校验：正常流程里 Orchestrator
        # 只会在 ALLOW 时调用这里。如果它被触发，说明代码有 Bug，
        # 必须立刻暴露而不是静默拒绝。
        if not decision.allowed:
            raise PermissionError(
                f"ActionExecutor 只接受 ALLOW 裁决，当前为 {decision.decision}。"
                "工具绝不允许绕过控制层被调用。"
            )

        tool: AgentTool = self.registry.get(tool_name)
        # 只用控制层校验过的参数。模型的 proposal.arguments 到这里已经无关紧要。
        arguments = dict(decision.validated_arguments)
        idem_key = self.build_key(task_id, step, tool_name, arguments)
        args_hash = arguments_hash(arguments)
        timeout = timeout_seconds or tool.default_timeout_seconds

        with tracer.span(
            f"tool.{tool_name}",
            trace_id=trace_id,
            task_id=task_id,
            step_id=step.step_id,
            tool_name=tool_name,
            idempotency_key=idem_key,
        ) as span:
            # —— 第 1 步：先占位，再执行 ——
            # 顺序不能反。占位记录的存在意味着「我们打算做这件事」，
            # 即使随后进程崩溃，恢复时也能看到这条悬挂记录并去对账。
            record, is_new = await self.execution_repo.reserve(
                task_id=task_id,
                step_id=step.step_id,
                tool_name=tool_name,
                idempotency_key=idem_key,
                arguments=arguments,
                arguments_hash=args_hash,
                attempt=step.retry_count + 1,
                trace_id=trace_id,
            )

            # —— 第 2 步：幂等命中处理 ——
            if not is_new:
                hit = self._handle_idempotent_hit(record, tool_name, idem_key)
                if hit is not None:
                    span.set_attribute("idempotent_hit", str(record.status))
                    metrics.increment(
                        "agent_tool_idempotent_hits_total", tool=tool_name, status=str(record.status)
                    )
                    return hit

            execution_id = record.execution_id
            exec_ctx = ToolExecutionContext(
                task_id=task_id,
                step_id=step.step_id,
                step_name=step.step_name,
                execution_id=execution_id,
                idempotency_key=idem_key,
                trace_id=trace_id,
                user_id=user_id,
                agent_id=agent_id,
                attempt=step.retry_count + 1,
                timeout_seconds=timeout,
                session=session,
            )

            # —— 第 3 步：参数二次校验（纵深防御）——
            # ParameterPolicy 已经校验过一次；这里再校验一次，
            # 是为了防止将来有人改动策略链顺序而不自知。
            # 代价是一次 Pydantic 构造，收益是「工具永远拿不到未校验的字典」。
            try:
                validated_args = validate_tool_arguments(tool, arguments)
            except ValidationError as exc:
                return await self._finalize(
                    execution_id,
                    tool.build_result(
                        exec_ctx,
                        status=ToolExecutionStatus.FAILED,
                        error_code=ErrorCode.INVALID_ARGUMENT,
                        error_message=exc.message,
                        retryable=False,
                    ),
                )

            # —— 第 4 步：带超时执行 ——
            started = utcnow()
            try:
                with metrics.timer("agent_tool_latency_seconds", tool=tool_name):
                    result = await asyncio.wait_for(
                        tool.execute(validated_args, exec_ctx), timeout=timeout
                    )
            except TimeoutError:
                # 超时：**结果未知**。绝不当作失败，也绝不直接重试。
                logger.warning(
                    "tool_execution_timeout",
                    tool_name=tool_name,
                    task_id=task_id,
                    step_id=step.step_id,
                    idempotency_key=idem_key,
                    timeout_seconds=timeout,
                )
                span.set_attribute("outcome", "timeout")
                metrics.increment("agent_tool_timeouts_total", tool=tool_name)
                return await self._finalize(
                    execution_id,
                    tool.build_result(
                        exec_ctx,
                        status=ToolExecutionStatus.TIMEOUT,
                        error_code=ErrorCode.TIMEOUT,
                        error_message=f"工具执行超过 {timeout} 秒未返回，结果未知，需对账",
                        retryable=False,  # 未对账前不可重试
                        started_at=started,
                    ),
                )
            except ToolTimeoutError as exc:
                # 工具主动声明「我超时了，结果未知」。处理方式与上面一致。
                span.set_attribute("outcome", "timeout")
                metrics.increment("agent_tool_timeouts_total", tool=tool_name)
                return await self._finalize(
                    execution_id,
                    tool.build_result(
                        exec_ctx,
                        status=ToolExecutionStatus.TIMEOUT,
                        error_code=ErrorCode.TIMEOUT,
                        error_message=exc.message,
                        retryable=False,
                        started_at=started,
                    ),
                )
            except IdempotencyConflictError:
                # 参数冲突必须往上抛：静默返回旧结果会吞掉第二笔业务。
                raise
            except AgentError as exc:
                # 框架内异常：错误码和 retryable 都由被调方声明，直接采信。
                span.set_attribute("outcome", "error")
                return await self._finalize(
                    execution_id,
                    tool.build_result(
                        exec_ctx,
                        status=ToolExecutionStatus.FAILED,
                        error_code=exc.error_code,
                        error_message=exc.message,
                        retryable=exc.retryable,
                        started_at=started,
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                # 未预期异常（含模拟的进程崩溃）。
                # 对**写操作**来说这属于「结果未知」，不是「失败」——
                # 异常可能发生在下游已经生效之后。所以写操作落 UNKNOWN 去对账，
                # 读操作才落 FAILED。这个区分是防重复副作用的关键一环。
                is_write = tool.step_type in (StepType.WRITE, StepType.NOTIFY)
                logger.exception(
                    "tool_execution_crashed",
                    tool_name=tool_name,
                    task_id=task_id,
                    step_id=step.step_id,
                    is_write=is_write,
                )
                span.set_attribute("outcome", "crash")
                return await self._finalize(
                    execution_id,
                    tool.build_result(
                        exec_ctx,
                        status=(
                            ToolExecutionStatus.UNKNOWN if is_write else ToolExecutionStatus.FAILED
                        ),
                        error_code=(
                            ErrorCode.UNKNOWN_EXECUTION_STATE
                            if is_write
                            else ErrorCode.INTERNAL_ERROR
                        ),
                        error_message=f"{type(exc).__name__}: {exc}",
                        retryable=False,
                        started_at=started,
                    ),
                )

            span.set_attribute("outcome", str(result.status))
            metrics.increment(
                "agent_tool_executions_total", tool=tool_name, status=str(result.status)
            )
            return await self._finalize(execution_id, result)

    async def reconcile(
        self,
        *,
        task_id: str,
        step: TaskStep,
        tool_name: str,
        session: Any,
        trace_id: str = "",
        user_id: str = "",
        agent_id: str = "",
    ) -> ToolExecutionResult | None:
        """对账：查询外部系统里这个幂等键的真实状态。

        **这是 TIMEOUT / UNKNOWN 状态的唯一出路。**

        Args:
            task_id: 任务 ID。
            step: 目标步骤（必须已有 `idempotency_key`）。
            tool_name: 工具名。
            session: 数据库会话。

        Returns:
            * 查明已成功 → SUCCESS 结果，调用方只需**补写状态和 external_ref**，
              绝不重复执行；
            * 查明未发生 → FAILED 且 retryable=True，可以带同一个幂等键安全重试；
            * ``None`` → 查无可查，调用方应升级人工。

        Note:
            对账不是一次性的：外部系统可能「稍后会成功」。
            真实实现应该带退避地查几轮，仍不明确才升级人工。
        """
        if not step.idempotency_key:
            # 没有幂等键就无法对账。这本身是一个设计缺陷的信号——
            # 所有写操作都应该在执行前就把键落库。
            logger.error(
                "reconcile_without_idempotency_key",
                task_id=task_id,
                step_id=step.step_id,
                tool_name=tool_name,
            )
            return None

        tool = self.registry.get(tool_name)
        exec_ctx = ToolExecutionContext(
            task_id=task_id,
            step_id=step.step_id,
            step_name=step.step_name,
            execution_id=new_execution_id(),
            idempotency_key=step.idempotency_key,
            trace_id=trace_id,
            user_id=user_id,
            agent_id=agent_id,
            session=session,
        )

        with tracer.span(
            f"reconcile.{tool_name}",
            trace_id=trace_id,
            task_id=task_id,
            step_id=step.step_id,
            idempotency_key=step.idempotency_key,
        ):
            result = await tool.query_external_status(step.idempotency_key, exec_ctx)

        # ------------------------------------------------------------------
        # **把对账查明的真相写回去重记录。** 这一步不能省。
        #
        # 去重记录里此刻存的还是「TIMEOUT / IN_FLIGHT」——也就是「我不知道」。
        # 如果对账查明「其实没执行」之后不更新它，那么下一次重试调用 `reserve()`
        # 时会再次命中这条未决记录，被 `_handle_idempotent_hit` 当作
        # 「结果未知」直接返回，于是重试永远执行不下去，
        # 最后耗尽重试次数变成一个假的失败。
        #
        # 换个角度说：去重表记录的是**外部副作用的真相**。
        # 对账的全部意义就是把「不知道」变成「知道」，
        # 那就必须把这个「知道」落回表里。
        # ------------------------------------------------------------------
        if result is not None:
            existing = await self.execution_repo.find_by_idempotency_key(step.idempotency_key)
            if existing is not None:
                await self.execution_repo.complete(
                    existing.execution_id,
                    status=(
                        ToolExecutionStatus.SUCCESS
                        if result.succeeded
                        # 确认未发生 → 记为可重试的失败，下一次重试才能真的跑起来。
                        else ToolExecutionStatus.FAILED
                    ),
                    result=result.result,
                    error_code=result.error_code,
                    error_message=result.error_message,
                    retryable=result.retryable,
                    external_reference_id=result.external_reference_id,
                )

        metrics.increment(
            "agent_reconciliations_total",
            tool=tool_name,
            outcome=str(result.status) if result else "unresolvable",
        )
        return result

    # ------------------------------------------------------------------ 内部
    def _handle_idempotent_hit(
        self, record: Any, tool_name: str, idem_key: str
    ) -> ToolExecutionResult | None:
        """处理幂等键命中的三种情况。

        Returns:
            * 已成功 → 直接返回历史结果（**不重复执行**）；
            * 执行中（IN_FLIGHT）→ 返回 UNKNOWN，让上层走对账；
            * 失败 → 返回 ``None``，允许按重试策略重新执行。
        """
        status = ToolExecutionStatus(record.status)

        if status in (ToolExecutionStatus.SUCCESS, ToolExecutionStatus.SKIPPED_IDEMPOTENT):
            logger.info(
                "idempotent_hit_success",
                tool_name=tool_name,
                idempotency_key=idem_key,
                execution_id=record.execution_id,
            )
            return ToolExecutionResult(
                tool_name=tool_name,
                execution_id=record.execution_id,
                status=ToolExecutionStatus.SKIPPED_IDEMPOTENT,
                result=record.result,
                external_reference_id=record.external_reference_id,
                idempotency_key=idem_key,
                started_at=record.started_at,
                completed_at=record.completed_at,
            )

        if status == ToolExecutionStatus.IN_FLIGHT:
            # 存在一条占位记录但没有结果：要么有另一个 worker 正在执行，
            # 要么上一次执行崩在了中途。两种情况都是「结果未知」，
            # 必须对账，绝不能假设它失败了然后重来。
            logger.warning(
                "idempotent_hit_in_flight",
                tool_name=tool_name,
                idempotency_key=idem_key,
                execution_id=record.execution_id,
            )
            return ToolExecutionResult(
                tool_name=tool_name,
                execution_id=record.execution_id,
                status=ToolExecutionStatus.UNKNOWN,
                error_code=ErrorCode.UNKNOWN_EXECUTION_STATE,
                error_message="存在同幂等键的执行中记录，结果未知，需对账",
                retryable=False,
                idempotency_key=idem_key,
                started_at=record.started_at,
            )

        if status in (ToolExecutionStatus.TIMEOUT, ToolExecutionStatus.UNKNOWN):
            return ToolExecutionResult(
                tool_name=tool_name,
                execution_id=record.execution_id,
                status=status,
                error_code=record.error_code,
                error_message=record.error_message,
                retryable=False,
                external_reference_id=record.external_reference_id,
                idempotency_key=idem_key,
                started_at=record.started_at,
            )

        # FAILED：允许重新执行。返回 None 让调用方继续走正常执行路径。
        return None

    async def _finalize(
        self, execution_id: str, result: ToolExecutionResult
    ) -> ToolExecutionResult:
        """把结果落盘到执行记录表。"""
        await self.execution_repo.complete(
            execution_id,
            status=result.status,
            result=result.result,
            error_code=result.error_code,
            error_message=result.error_message,
            retryable=result.retryable,
            external_reference_id=result.external_reference_id,
        )
        return result
