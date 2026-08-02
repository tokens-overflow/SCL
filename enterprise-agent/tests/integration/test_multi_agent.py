"""多 Agent 编排集成测试。

重点验证：并行执行、必需项失败终止、部分成功聚合、超时槽位。
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from app.context.models import AgentContext
from app.core.enums import AgentResultStatus
from app.core.errors import AgentError
from app.core.ids import new_id, new_trace_id
from app.examples.discount_workflow import (
    DiscountRequestInput,
    DiscountRequestOrchestrator,
)
from app.runtime.models import AgentResult
from app.runtime.multi_agent import (
    AggregationRule,
    FunctionAgentWorker,
    MultiAgentOrchestrator,
)
from app.security.identity import MockIdentityProvider, ResolvedIdentity


async def _context() -> AgentContext:
    provider = MockIdentityProvider()
    identity = ResolvedIdentity(
        user=await provider.get_user("user_001"),
        agent=await provider.get_agent("discount_agent"),
    )
    return AgentContext(
        task_id="task_multi", trace_id=new_trace_id(), user_id="user_001",
        agent_id="discount_agent", identity=identity, user_input="给客户 C001 打九五折",
        extra={"business_facts": {"customer_tier": "STANDARD"}},
    )


class _Input(BaseModel):
    value: int = 1


class _SlowWorker:
    def __init__(self, agent_id: str, delay: float) -> None:
        self.agent_id = agent_id
        self.delay = delay

    async def run(self, input_data: BaseModel, context: AgentContext) -> AgentResult:
        await asyncio.sleep(self.delay)
        return AgentResult(
            agent_id=self.agent_id, task_id=new_id("t"),
            status=AgentResultStatus.SUCCESS, result={"ok": True}, trace_id=context.trace_id,
        )


class _FailingWorker:
    def __init__(self, agent_id: str, *, retryable: bool = False) -> None:
        self.agent_id = agent_id
        self.retryable = retryable

    async def run(self, input_data: BaseModel, context: AgentContext) -> AgentResult:
        from app.core.enums import ErrorCode

        raise AgentError(
            "子 Agent 内部失败", error_code=ErrorCode.NOT_FOUND, retryable=self.retryable
        )


class TestParallelExecution:
    async def test_asyncio_gather_runs_in_parallel(self) -> None:
        """并行执行：总耗时接近**最慢的那个**，不是各个之和。

        这也是「延迟账」的具体体现。
        """
        import time

        ctx = await _context()
        orch = MultiAgentOrchestrator(AggregationRule(slot_timeout_seconds=5))
        workers = {
            f"agent_{i}": (_SlowWorker(f"agent_{i}", 0.15), _Input())
            for i in range(4)
        }
        started = time.perf_counter()
        result = await orch.run_parallel(workers, ctx)
        elapsed = time.perf_counter() - started

        assert result.status == AgentResultStatus.SUCCESS
        assert len(result.results) == 4
        # 串行需要 0.6s，并行只要 ~0.15s
        assert elapsed < 0.4, f"看起来不是并行执行，耗时 {elapsed:.2f}s"

    async def test_one_worker_exception_does_not_kill_the_batch(self) -> None:
        """一个子 Agent 抛异常，另外几个的结果不能丢。"""
        ctx = await _context()
        orch = MultiAgentOrchestrator(AggregationRule(min_success=1))
        result = await orch.run_parallel(
            {
                "ok_agent": (_SlowWorker("ok_agent", 0.01), _Input()),
                "bad_agent": (_FailingWorker("bad_agent"), _Input()),
            },
            ctx,
        )
        assert len(result.results) == 2
        assert result.status == AgentResultStatus.PARTIAL_SUCCESS

    async def test_slot_timeout(self) -> None:
        """每个槽位有独立超时——并行的总耗时等于最慢的那个。"""
        ctx = await _context()
        orch = MultiAgentOrchestrator(
            AggregationRule(slot_timeout_seconds=0.05, retry_on=[], min_success=1)
        )
        result = await orch.run_parallel(
            {
                "fast": (_SlowWorker("fast", 0.01), _Input()),
                "slow": (_SlowWorker("slow", 1.0), _Input()),
            },
            ctx,
        )
        by_agent = result.by_agent
        assert by_agent["fast"].status == AgentResultStatus.SUCCESS
        assert by_agent["slow"].status == AgentResultStatus.TIMEOUT
        # 超时的子 Agent **不自动标记为可重试**——它可能有写副作用
        assert by_agent["slow"].retryable is False


class TestAggregation:
    async def test_required_failure_stops_everything(self) -> None:
        """必需项失败 → 整单终止。

        「合同风险没跑出来」绝不能被当成「没有风险」。
        """
        ctx = await _context()
        orch = MultiAgentOrchestrator(AggregationRule(required=["critical_agent"]))
        result = await orch.run_parallel(
            {
                "critical_agent": (_FailingWorker("critical_agent"), _Input()),
                "optional_agent": (_SlowWorker("optional_agent", 0.01), _Input()),
            },
            ctx,
        )
        assert result.status == AgentResultStatus.FAILED
        assert "critical_agent" in result.reason

    async def test_min_success_threshold(self) -> None:
        ctx = await _context()
        orch = MultiAgentOrchestrator(AggregationRule(min_success=3))
        result = await orch.run_parallel(
            {
                "a": (_SlowWorker("a", 0.01), _Input()),
                "b": (_SlowWorker("b", 0.01), _Input()),
                "c": (_FailingWorker("c"), _Input()),
            },
            ctx,
        )
        assert result.status == AgentResultStatus.FAILED
        assert "低于要求" in result.reason

    async def test_partial_success(self) -> None:
        ctx = await _context()
        orch = MultiAgentOrchestrator(AggregationRule(min_success=2))
        result = await orch.run_parallel(
            {
                "a": (_SlowWorker("a", 0.01), _Input()),
                "b": (_SlowWorker("b", 0.01), _Input()),
                "c": (_FailingWorker("c"), _Input()),
            },
            ctx,
        )
        assert result.status == AgentResultStatus.PARTIAL_SUCCESS
        assert len(result.successes()) == 2
        assert len(result.failures()) == 1

    async def test_human_review_rule(self) -> None:
        """高风险结论不该被自动放行。"""
        ctx = await _context()
        orch = MultiAgentOrchestrator(
            AggregationRule(
                human_review_if=lambda rs: any(
                    (r.result or {}).get("risk_level") == "CRITICAL" for r in rs
                )
            )
        )
        risky = FunctionAgentWorker(
            "risk_agent", lambda i, c: _risky_result()
        )
        result = await orch.run_parallel({"risk_agent": (risky, _Input())}, ctx)
        assert result.status == AgentResultStatus.WAITING_APPROVAL

    async def test_skipped_is_not_success(self) -> None:
        """跳过 ≠ 成功。把跳过记成成功会让 min_success 算错。"""
        ctx = await _context()
        orch = MultiAgentOrchestrator(AggregationRule(min_success=1))
        result = await orch.run_conditional(
            "cond_agent", _SlowWorker("cond_agent", 0.01), _Input(), ctx,
            condition=lambda c: False,
        )
        assert result.status == AgentResultStatus.SKIPPED
        agg = orch.aggregate([result])
        assert agg.status == AgentResultStatus.FAILED


async def _risky_result() -> dict:
    return {"risk_level": "CRITICAL"}


class TestSequentialAndFailFast:
    async def test_fail_fast_marks_rest_as_skipped(self) -> None:
        """快速终止时，剩余槽位标记为 SKIPPED —— 「没跑」和「跑了没结果」必须能区分。"""
        ctx = await _context()
        orch = MultiAgentOrchestrator(AggregationRule(required=["first"], min_success=0))
        result = await orch.run_sequential(
            [
                ("first", _FailingWorker("first"), _Input()),
                ("second", _SlowWorker("second", 0.01), _Input()),
                ("third", _SlowWorker("third", 0.01), _Input()),
            ],
            ctx,
            stop_on_failure=True,
        )
        by_agent = result.by_agent
        assert by_agent["first"].status == AgentResultStatus.FAILED
        assert by_agent["second"].status == AgentResultStatus.SKIPPED
        assert by_agent["third"].status == AgentResultStatus.SKIPPED


class TestDiscountRequestOrchestrator:
    """折扣申请的完整多 Agent 编排。"""

    async def test_eligible_customer_gets_recommendation(self, seeded_session) -> None:
        ctx = await _context()
        orch = DiscountRequestOrchestrator(seeded_session)
        result = await orch.evaluate(
            DiscountRequestInput(customer_id="C001", requested_rate=0.05), ctx
        )
        assert result.status == AgentResultStatus.SUCCESS
        rec = result.by_agent["discount_recommendation_agent"].result
        assert rec["decision"] == "AUTO_APPROVE"
        # 子 Agent 明确声明自己只是建议
        assert "建议" in rec["note"]

    async def test_ten_percent_needs_approval(self, seeded_session) -> None:
        ctx = await _context()
        orch = DiscountRequestOrchestrator(seeded_session)
        result = await orch.evaluate(
            DiscountRequestInput(customer_id="C001", requested_rate=0.10), ctx
        )
        rec = result.by_agent["discount_recommendation_agent"].result
        assert rec["decision"] == "NEEDS_APPROVAL"

    async def test_vip_gets_wider_range(self, seeded_session) -> None:
        ctx = await _context()
        orch = DiscountRequestOrchestrator(seeded_session)
        result = await orch.evaluate(
            DiscountRequestInput(customer_id="C002", requested_rate=0.07), ctx
        )
        rec = result.by_agent["discount_recommendation_agent"].result
        assert rec["decision"] == "AUTO_APPROVE"
        assert rec["self_service_max"] > 0.05

    async def test_missing_customer_stops_orchestration(self, seeded_session) -> None:
        """必需的资格检查失败 → 整单停，后面的 Agent 都不跑。"""
        ctx = await _context()
        orch = DiscountRequestOrchestrator(seeded_session)
        result = await orch.evaluate(
            DiscountRequestInput(customer_id="C999", requested_rate=0.05), ctx
        )
        assert result.status == AgentResultStatus.FAILED
        assert "customer_eligibility_agent" in result.reason
        assert "discount_recommendation_agent" not in result.by_agent

    async def test_thirty_percent_is_rejected(self, seeded_session) -> None:
        ctx = await _context()
        orch = DiscountRequestOrchestrator(seeded_session)
        result = await orch.evaluate(
            DiscountRequestInput(customer_id="C001", requested_rate=0.30), ctx
        )
        rec = result.by_agent["discount_recommendation_agent"].result
        assert rec["decision"] == "REJECT"

    async def test_cost_is_aggregated(self, seeded_session) -> None:
        """成本必须能按编排归因——否则算不出这次编排花了多少钱。"""
        ctx = await _context()
        orch = DiscountRequestOrchestrator(seeded_session)
        result = await orch.evaluate(
            DiscountRequestInput(customer_id="C001", requested_rate=0.05), ctx
        )
        payload = result.to_dict()
        assert "total_cost" in payload
        assert set(payload["total_cost"]) == {"tokens_in", "tokens_out", "amount"}
        assert payload["elapsed_ms"] >= 0


class TestStableContract:
    async def test_pure_code_worker_is_indistinguishable(self) -> None:
        """**子 Agent 可以被替换成一段纯代码，上层完全无感。**

        这就是稳定契约的价值：业务跑顺之后发现不需要模型，
        降级是无痛的。
        """
        ctx = await _context()
        worker = FunctionAgentWorker("rule_agent", lambda i, c: _plain_result())
        orch = MultiAgentOrchestrator(AggregationRule())
        result = await orch.run_parallel({"rule_agent": (worker, _Input())}, ctx)
        assert result.status == AgentResultStatus.SUCCESS
        assert result.by_agent["rule_agent"].result == {"computed": 42}


async def _plain_result() -> dict:
    return {"computed": 42}
