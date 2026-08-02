"""多 Agent 编排。

**多 Agent 的难点从来不是「怎么让它们说话」，而是「其中两个失败了，
这一单还算不算数」。**

所以本模块的重点不在通信，在两件事：

1. **稳定的对外契约**（:class:`~app.runtime.models.AgentResult`）。
   子 Agent 内部可以有自己的 LLM、工具、状态机、子工作流、重试与补偿，
   但这些细节不穿透到上层。契约稳定之后有一个很实际的好处：
   **子 Agent 可以被替换成一段纯代码，上层完全无感**——
   很多「Agent」在业务跑顺之后会发现根本不需要模型，规则就够了。

2. **声明式的聚合规则**（:class:`AggregationRule`）。
   拿到五个结果之后怎么办，**是配置，不是模型的临时判断**。
   写成声明式的规则既能测试也能审计。

关于什么时候真的需要多 Agent，合理的理由只有四个：

* 不同角色需要不同的工具权限边界
* 上下文互相污染必须隔离
* 不同子任务的成本差异大到该分开
* 有明确的并行加速收益

如果单 Agent 挂几个工具就能做，拆开只是把误差复合的链路又拉长了一截，
还多了一套状态同步要维护。

**两个容易忽略的账**：

* 延迟账：并行五个 Agent，总耗时不是平均值而是**最大值**。
  所以必须给每个槽位设独立超时，并想清楚「超时的那个能不能丢」。
* 成本账：五个 Agent 各自带完整上下文，成本大致是单 Agent 的
  **五倍以上**（不是五分之一）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from app.context.models import AgentContext
from app.core.enums import AgentResultStatus, ErrorCode
from app.core.errors import AgentError
from app.core.ids import new_id
from app.operations.logging import get_logger
from app.operations.metrics import metrics
from app.runtime.models import AgentResult

logger = get_logger(__name__)


class AgentWorker(Protocol):
    """子 Agent 的统一接口。

    上层编排器只关心输入、输出和状态。
    子 Agent 内部是一个完整的 Agent、一段规则代码、
    还是一次远程调用（甚至是别的团队用别的语言写的），
    对编排器来说没有区别。
    """

    agent_id: str

    async def run(self, input_data: BaseModel, context: AgentContext) -> AgentResult:
        """执行子任务。"""
        ...


@dataclass
class AggregationRule:
    """聚合规则。

    **每一条都必须显式声明**，因为默认值往往是错的：

    Attributes:
        required: 必需的子 Agent。它们失败 → 整单必须停。
            **不显式声明的话，「合同风险没跑出来」会被当成「没有风险」**——
            这是多 Agent 编排里最贵的一类错误。
        min_success: 最低成功数。区分「必须全对」和「尽力而为」两类场景。
        retry_on: 哪些状态允许自动重试。
            注意 UNKNOWN 不在默认列表里——它要先对账，不能直接重试。
        max_retry: 最大重试次数。
        human_review_if: 什么结果必须人来看。
        slot_timeout_seconds: 每个槽位的独立超时。
            **并行的总耗时等于最慢的那个**，所以这个值必须有。
        fail_fast: 必需项失败时是否立即取消其余任务。
    """

    required: list[str] = field(default_factory=list)
    min_success: int = 0
    retry_on: list[str] = field(default_factory=lambda: [AgentResultStatus.TIMEOUT.value])
    max_retry: int = 1
    human_review_if: Callable[[list[AgentResult]], bool] | None = None
    slot_timeout_seconds: float = 15.0
    fail_fast: bool = True


@dataclass
class AggregatedResult:
    """聚合后的编排结果。"""

    status: AgentResultStatus
    results: list[AgentResult] = field(default_factory=list)
    reason: str = ""
    elapsed_ms: int = 0

    @property
    def by_agent(self) -> dict[str, AgentResult]:
        """按 agent_id 索引结果。"""
        return {r.agent_id: r for r in self.results}

    def successes(self) -> list[AgentResult]:
        """成功的子结果。"""
        return [r for r in self.results if r.status == AgentResultStatus.SUCCESS]

    def failures(self) -> list[AgentResult]:
        """失败或超时的子结果。"""
        return [
            r
            for r in self.results
            if r.status in (AgentResultStatus.FAILED, AgentResultStatus.TIMEOUT)
        ]

    def to_dict(self) -> dict[str, Any]:
        """序列化，用于审计与 API 返回。"""
        return {
            "status": str(self.status),
            "reason": self.reason,
            "elapsed_ms": self.elapsed_ms,
            "results": [r.model_dump() for r in self.results],
            # 成本必须能按编排归因：五个子 Agent 各带完整上下文，
            # 成本是单 Agent 的五倍以上，没有这个字段就算不出来。
            "total_cost": _sum_cost(self.results),
        }


class MultiAgentOrchestrator:
    """多 Agent 编排器。

    **状态槽位由编排器创建和维护，不是由大模型临时决定。**
    模型可以建议「这次要不要跑供应商分析」，但槽位的建立、
    状态的流转、聚合的判定，全都是程序的事。
    """

    def __init__(self, rule: AggregationRule | None = None) -> None:
        self.rule = rule or AggregationRule()

    async def run_parallel(
        self,
        workers: dict[str, tuple[AgentWorker, BaseModel]],
        context: AgentContext,
    ) -> AggregatedResult:
        """并行执行多个子 Agent。

        Args:
            workers: ``{槽位名: (worker, 输入)}``。
            context: 共享上下文。

        Returns:
            :class:`AggregatedResult`。

        Note:
            用 `asyncio.gather(..., return_exceptions=True)`：
            **一个子 Agent 抛异常不能让整批 gather 炸掉**，
            否则另外四个已经跑完的结果就全丢了。
        """
        started = time.perf_counter()

        async def _run_slot(slot: str, worker: AgentWorker, payload: BaseModel) -> AgentResult:
            return await self._run_with_timeout(slot, worker, payload, context)

        outcomes = await asyncio.gather(
            *(_run_slot(slot, w, p) for slot, (w, p) in workers.items()),
            return_exceptions=True,
        )

        results: list[AgentResult] = []
        for slot, outcome in zip(workers.keys(), outcomes, strict=True):
            if isinstance(outcome, AgentResult):
                results.append(outcome)
            else:
                # 兜底：子 Agent 连自己的异常都没接住。
                results.append(
                    AgentResult(
                        agent_id=slot,
                        task_id=new_id("subtask"),
                        status=AgentResultStatus.FAILED,
                        error_code=ErrorCode.INTERNAL_ERROR,
                        error_message=f"{type(outcome).__name__}: {outcome}",
                        # retryable 由被调方声明；连异常都没接住的情况下
                        # 我们无法判断，保守地按不可重试处理。
                        retryable=False,
                        trace_id=context.trace_id,
                    )
                )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        aggregated = self.aggregate(results)
        aggregated.elapsed_ms = elapsed_ms

        metrics.observe("agent_multi_orchestration_seconds", elapsed_ms / 1000)
        logger.info(
            "multi_agent_finished",
            task_id=context.task_id,
            trace_id=context.trace_id,
            status=str(aggregated.status),
            elapsed_ms=elapsed_ms,
            slot_results={r.agent_id: str(r.status) for r in results},
        )
        return aggregated

    async def run_sequential(
        self,
        workers: list[tuple[str, AgentWorker, BaseModel]],
        context: AgentContext,
        *,
        stop_on_failure: bool = True,
    ) -> AggregatedResult:
        """串行执行多个子 Agent。

        Args:
            workers: ``[(槽位名, worker, 输入)]``，按执行顺序。
            context: 共享上下文。
            stop_on_failure: 失败时是否快速终止后续步骤。

        Returns:
            :class:`AggregatedResult`。
        """
        started = time.perf_counter()
        results: list[AgentResult] = []

        for slot, worker, payload in workers:
            result = await self._run_with_timeout(slot, worker, payload, context)
            results.append(result)
            failed = result.status in (AgentResultStatus.FAILED, AgentResultStatus.TIMEOUT)
            if failed and stop_on_failure and (slot in self.rule.required or self.rule.fail_fast):
                # 剩余槽位标记为 SKIPPED，而不是干脆不出现。
                # 「没跑」和「跑了没结果」必须能区分开——
                # 否则聚合规则里的 min_success 会把「没跑」误算成一种结果。
                for rest_slot, _, _ in workers[len(results) :]:
                    results.append(
                        AgentResult(
                            agent_id=rest_slot,
                            task_id=new_id("subtask"),
                            status=AgentResultStatus.SKIPPED,
                            error_message=f"前置槽位 {slot} 失败，快速终止",
                            trace_id=context.trace_id,
                        )
                    )
                break

        aggregated = self.aggregate(results)
        aggregated.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return aggregated

    async def run_conditional(
        self,
        slot: str,
        worker: AgentWorker,
        payload: BaseModel,
        context: AgentContext,
        *,
        condition: Callable[[AgentContext], bool],
    ) -> AgentResult:
        """条件分支执行。

        条件不满足时返回 SKIPPED——**不是 SUCCESS**。
        把「跳过」记成「成功」会让聚合规则算错：
        `min_success=3` 时，三个被跳过的槽位会被误判为达标。
        """
        if not condition(context):
            return AgentResult(
                agent_id=slot,
                task_id=new_id("subtask"),
                status=AgentResultStatus.SKIPPED,
                error_message="条件分支未命中",
                trace_id=context.trace_id,
            )
        return await self._run_with_timeout(slot, worker, payload, context)

    def aggregate(self, results: list[AgentResult]) -> AggregatedResult:
        """按声明式规则聚合结果。

        判定顺序：

        1. 必需项失败/超时/跳过 → FAILED（**整单停**）
        2. 有等待审批 → WAITING_APPROVAL
        3. 成功数 < min_success → FAILED
        4. 存在失败但满足最低成功数 → PARTIAL_SUCCESS
        5. 全部成功 → SUCCESS

        Args:
            results: 各槽位结果。

        Returns:
            :class:`AggregatedResult`。
        """
        by_agent = {r.agent_id: r for r in results}

        # 1. 必需项
        for required_id in self.rule.required:
            result = by_agent.get(required_id)
            if result is None or result.status != AgentResultStatus.SUCCESS:
                status = result.status if result else "MISSING"
                return AggregatedResult(
                    status=AgentResultStatus.FAILED,
                    results=results,
                    reason=(
                        f"必需子 Agent「{required_id}」未成功（{status}），"
                        "按聚合规则整单终止"
                    ),
                )

        # 2. 等待审批：只要有一个在等，整单就是等待状态。
        if any(r.status == AgentResultStatus.WAITING_APPROVAL for r in results):
            return AggregatedResult(
                status=AgentResultStatus.WAITING_APPROVAL,
                results=results,
                reason="存在等待人工审批的子 Agent",
            )

        successes = [r for r in results if r.status == AgentResultStatus.SUCCESS]

        # 3. 最低成功数
        if len(successes) < self.rule.min_success:
            return AggregatedResult(
                status=AgentResultStatus.FAILED,
                results=results,
                reason=(
                    f"成功子 Agent 数 {len(successes)} 低于要求的 {self.rule.min_success}"
                ),
            )

        # 4. 人工复核条件
        if self.rule.human_review_if and self.rule.human_review_if(results):
            return AggregatedResult(
                status=AgentResultStatus.WAITING_APPROVAL,
                results=results,
                reason="命中人工复核条件",
            )

        # 5. 部分成功 vs 全部成功
        if len(successes) == len(results):
            return AggregatedResult(
                status=AgentResultStatus.SUCCESS, results=results, reason="全部子 Agent 成功"
            )
        return AggregatedResult(
            status=AgentResultStatus.PARTIAL_SUCCESS,
            results=results,
            reason=(
                f"{len(successes)}/{len(results)} 个子 Agent 成功，"
                "已满足最低成功数与必需项要求"
            ),
        )

    async def _run_with_timeout(
        self,
        slot: str,
        worker: AgentWorker,
        payload: BaseModel,
        context: AgentContext,
    ) -> AgentResult:
        """带独立超时地执行一个槽位，并按规则重试。"""
        attempt = 0
        while True:
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    worker.run(payload, context), timeout=self.rule.slot_timeout_seconds
                )
            except TimeoutError:
                result = AgentResult(
                    agent_id=slot,
                    task_id=new_id("subtask"),
                    status=AgentResultStatus.TIMEOUT,
                    error_code=ErrorCode.TIMEOUT,
                    error_message=f"子 Agent 超过 {self.rule.slot_timeout_seconds} 秒未返回",
                    # 超时**不代表可重试**：如果子 Agent 有写副作用，
                    # 直接重试就是第二笔。是否重试要看聚合规则的 retry_on，
                    # 而写类子 Agent 应该在自己内部完成对账。
                    retryable=False,
                    trace_id=context.trace_id,
                )
            except AgentError as exc:
                result = AgentResult(
                    agent_id=slot,
                    task_id=new_id("subtask"),
                    status=AgentResultStatus.FAILED,
                    error_code=exc.error_code,
                    error_message=exc.message,
                    # retryable 由被调方声明——只有它知道这次失败的性质。
                    retryable=exc.retryable,
                    trace_id=context.trace_id,
                )
            result.elapsed_ms = result.elapsed_ms or int((time.perf_counter() - started) * 1000)

            if str(result.status) not in self.rule.retry_on or attempt >= self.rule.max_retry:
                return result
            attempt += 1
            logger.info(
                "sub_agent_retry",
                slot=slot,
                attempt=attempt,
                status=str(result.status),
                task_id=context.task_id,
            )


def _sum_cost(results: list[AgentResult]) -> dict[str, float]:
    total: dict[str, float] = {"tokens_in": 0, "tokens_out": 0, "amount": 0.0}
    for r in results:
        for key in total:
            total[key] += float(r.cost.get(key, 0) or 0)
    return total


class FunctionAgentWorker:
    """把一个普通异步函数包装成 AgentWorker。

    这个类的存在本身就是一个论点：**子 Agent 不一定要是 Agent。**
    契约稳定之后，一段纯规则代码可以无缝顶替一个真正的 LLM Agent，
    上层完全无感。这种降级在业务跑顺之后非常常见，
    而有统一契约的话它是无痛的。
    """

    def __init__(
        self,
        agent_id: str,
        fn: Callable[[BaseModel, AgentContext], Awaitable[dict[str, Any]]],
    ) -> None:
        self.agent_id = agent_id
        self._fn = fn

    async def run(self, input_data: BaseModel, context: AgentContext) -> AgentResult:
        """执行被包装的函数并转成统一契约。"""
        started = time.perf_counter()
        payload = await self._fn(input_data, context)
        return AgentResult(
            agent_id=self.agent_id,
            task_id=new_id("subtask"),
            status=AgentResultStatus.SUCCESS,
            result=payload,
            trace_id=context.trace_id,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
