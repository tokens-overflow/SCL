"""示例业务：客户折扣申请（含多 Agent 编排）。

这个模块把前面所有零件串成一个可运行的完整案例：

    DiscountRequestOrchestrator
    ├── CustomerEligibilityAgent   客户资格（并行）
    ├── RiskAssessmentAgent        风险评估（并行）
    ├── DiscountRecommendationAgent 折扣建议（依赖前两者，串行）
    └── NotificationAgent          通知（最后，不可撤回）

刻意展示的几个点：

* 前两个子 Agent **并行**执行（`asyncio.gather`），因为它们互不依赖；
* `CustomerEligibilityAgent` 是**必需项**——它失败整单必须停，
  而不是「查不到客户就当没有限制」；
* `RiskAssessmentAgent` 是**可选项**——它超时可以丢，
  但丢了要在结果里体现，不能假装它成功了；
* `DiscountRecommendationAgent` 是一段**纯规则代码**，没有 LLM。
  这演示了「子 Agent 可以被替换成纯代码，上层无感」——
  同时也提醒：折扣额度这类判断本来就不该交给模型。
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.context.models import AgentContext
from app.core.config import Settings, get_settings
from app.core.enums import AgentResultStatus, ErrorCode, RiskLevel
from app.core.ids import new_id
from app.operations.logging import get_logger
from app.runtime.models import AgentResult
from app.runtime.multi_agent import (
    AggregatedResult,
    AggregationRule,
    MultiAgentOrchestrator,
)
from app.state.models import CustomerORM, DiscountORM

logger = get_logger(__name__)


# ======================================================================
# 子 Agent 的输入契约
# ======================================================================
class DiscountRequestInput(BaseModel):
    """折扣申请的输入。"""

    customer_id: str = Field(min_length=2, max_length=32)
    requested_rate: float = Field(gt=0.0, le=1.0)
    requested_by: str = ""
    notify: bool = True


# ======================================================================
# 子 Agent 实现
# ======================================================================
class CustomerEligibilityAgent:
    """客户资格检查子 Agent（**必需项**）。

    内部实现：直接查数据库。没有 LLM。

    为什么这是必需项：如果它失败了，我们就不知道这个客户是否存在、
    是否已有生效折扣。**「查不到」绝不能被当成「没有限制」**——
    这是多 Agent 编排里最贵的一类错误。
    """

    agent_id = "customer_eligibility_agent"

    def __init__(self, session: Any) -> None:
        self.session = session

    async def run(self, input_data: BaseModel, context: AgentContext) -> AgentResult:
        """检查客户资格。"""
        assert isinstance(input_data, DiscountRequestInput)
        started = time.perf_counter()
        task_id = new_id("subtask")

        customer = await self.session.get(CustomerORM, input_data.customer_id)
        if customer is None:
            return AgentResult(
                agent_id=self.agent_id,
                task_id=task_id,
                status=AgentResultStatus.FAILED,
                error_code=ErrorCode.NOT_FOUND,
                error_message=f"客户 {input_data.customer_id} 不存在",
                # 被调方声明可重试性：客户不存在，重试一万次也一样。
                retryable=False,
                trace_id=context.trace_id,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

        if customer.status != "ACTIVE":
            return AgentResult(
                agent_id=self.agent_id,
                task_id=task_id,
                status=AgentResultStatus.FAILED,
                error_code=ErrorCode.BUSINESS_RULE_VIOLATION,
                error_message=f"客户状态为 {customer.status}，不符合折扣发放条件",
                retryable=False,
                trace_id=context.trace_id,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

        result = await self.session.execute(
            select(DiscountORM)
            .where(DiscountORM.customer_id == input_data.customer_id)
            .where(DiscountORM.status == "ACTIVE")
        )
        active = result.scalars().first()

        return AgentResult(
            agent_id=self.agent_id,
            task_id=task_id,
            status=AgentResultStatus.SUCCESS,
            result={
                "eligible": active is None,
                "customer_tier": customer.tier,
                "customer_department": customer.department,
                "lifetime_value": customer.lifetime_value,
                "active_discount_rate": active.discount_rate if active else None,
                "blocking_reason": "已有生效折扣" if active else None,
            },
            trace_id=context.trace_id,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


class RiskAssessmentAgent:
    """风险评估子 Agent（**可选项**）。

    内部实现：基于客户价值与折扣幅度的确定性评分。没有 LLM。

    为什么是可选项：风险评估失败时，我们仍然可以按最保守的规则处理
    （当作高风险，走审批）。**注意「可选」不等于「失败了就当没事」**——
    它的缺失会体现在聚合结果里，并且会让折扣建议按保守路径走。
    """

    agent_id = "risk_assessment_agent"

    def __init__(self, session: Any, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    async def run(self, input_data: BaseModel, context: AgentContext) -> AgentResult:
        """评估折扣风险。"""
        assert isinstance(input_data, DiscountRequestInput)
        started = time.perf_counter()

        customer = await self.session.get(CustomerORM, input_data.customer_id)
        ltv = customer.lifetime_value if customer else 0.0
        tier = customer.tier if customer else "STANDARD"

        score = 0.0
        factors: list[str] = []

        if input_data.requested_rate > self.settings.discount_manager_approve_max:
            score += 60
            factors.append("超过公司最高折扣上限")
        elif input_data.requested_rate > self.settings.discount_auto_approve_max:
            score += 30
            factors.append("超过自助额度")

        if ltv < 10_000:
            score += 20
            factors.append("客户历史贡献较低")
        if tier.upper() in ("VIP", "PLATINUM"):
            score -= 10
            factors.append("VIP 客户，风险下调")

        score = max(0.0, min(100.0, score))
        level = (
            RiskLevel.CRITICAL
            if score >= 70
            else RiskLevel.HIGH
            if score >= 40
            else RiskLevel.MEDIUM
            if score >= 20
            else RiskLevel.LOW
        )

        return AgentResult(
            agent_id=self.agent_id,
            task_id=new_id("subtask"),
            status=AgentResultStatus.SUCCESS,
            result={"risk_score": score, "risk_level": str(level), "factors": factors},
            trace_id=context.trace_id,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


class DiscountRecommendationAgent:
    """折扣建议子 Agent。

    **这是一段纯规则代码，刻意如此。**

    折扣额度的判定有唯一正确答案（由制度规定），
    交给模型只会花钱买不确定性。把它做成一个「子 Agent」，
    是为了演示：契约稳定之后，上层根本不关心里面是模型还是规则。

    同时它也说明了另一件事——**建议不等于放行**。
    这个 Agent 给出的 `recommended_rate` 仍然要经过控制层，
    它自己没有任何执行权。
    """

    agent_id = "discount_recommendation_agent"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def run(self, input_data: BaseModel, context: AgentContext) -> AgentResult:
        """生成折扣建议。"""
        assert isinstance(input_data, DiscountRequestInput)
        started = time.perf_counter()

        facts = context.extra.get("business_facts", {})
        tier = str(facts.get("customer_tier") or "STANDARD")
        is_vip = tier.upper() in ("VIP", "PLATINUM")

        self_service_max = self.settings.discount_auto_approve_max
        if is_vip:
            self_service_max += self.settings.discount_vip_bonus
        absolute_max = self.settings.discount_manager_approve_max

        requested = input_data.requested_rate
        if requested > absolute_max:
            decision = "REJECT"
            recommended = 0.0
            reason = f"申请 {requested:.0%} 超过公司上限 {absolute_max:.0%}，不可批准"
        elif requested > self_service_max:
            decision = "NEEDS_APPROVAL"
            recommended = requested
            reason = f"申请 {requested:.0%} 超过自助额度 {self_service_max:.0%}，需经理审批"
        else:
            decision = "AUTO_APPROVE"
            recommended = requested
            reason = f"申请 {requested:.0%} 在自助额度 {self_service_max:.0%} 内"

        return AgentResult(
            agent_id=self.agent_id,
            task_id=new_id("subtask"),
            status=AgentResultStatus.SUCCESS,
            result={
                "decision": decision,
                "recommended_rate": recommended,
                "self_service_max": self_service_max,
                "absolute_max": absolute_max,
                "reason": reason,
                # 明确标注：这只是建议，最终放行由控制层决定。
                "note": "本结果为建议，实际执行仍需通过控制层策略评估",
            },
            trace_id=context.trace_id,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


class NotificationAgent:
    """通知子 Agent（**最后执行，不可撤回**）。

    这里只做「是否应该发通知」的判定，真正的发送走 `send_notification` 工具——
    因为发送是一个有副作用的动作，必须经过控制层和幂等保护。
    """

    agent_id = "notification_agent"

    async def run(self, input_data: BaseModel, context: AgentContext) -> AgentResult:
        """判定通知策略。"""
        assert isinstance(input_data, DiscountRequestInput)
        started = time.perf_counter()
        return AgentResult(
            agent_id=self.agent_id,
            task_id=new_id("subtask"),
            status=AgentResultStatus.SUCCESS,
            result={
                "should_notify": input_data.notify,
                "channel": "sms",
                "template": "discount_applied",
                "irreversible": True,
                "note": "通知不可撤回，必须排在链路最后",
            },
            trace_id=context.trace_id,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


# ======================================================================
# 编排器
# ======================================================================
class DiscountRequestOrchestrator:
    """折扣申请的多 Agent 编排。

    Args:
        session: 数据库会话。
        settings: 配置对象。
    """

    def __init__(self, session: Any, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.eligibility = CustomerEligibilityAgent(session)
        self.risk = RiskAssessmentAgent(session, self.settings)
        self.recommendation = DiscountRecommendationAgent(self.settings)
        self.notification = NotificationAgent()

        # 聚合规则是**配置**，不是模型的临时判断。
        # 写成声明式之后，既能被测试穷举，也能在审计里说清楚
        # 「当时用的是哪套聚合规则」。
        self.rule = AggregationRule(
            # 资格检查是必需的：查不到客户绝不能当成「没有限制」。
            required=["customer_eligibility_agent"],
            min_success=1,
            retry_on=[AgentResultStatus.TIMEOUT.value],
            max_retry=1,
            # 高风险结论不该被自动放行。
            human_review_if=lambda results: any(
                (r.result or {}).get("risk_level") == str(RiskLevel.CRITICAL) for r in results
            ),
            slot_timeout_seconds=10.0,
        )
        self.orchestrator = MultiAgentOrchestrator(self.rule)

    async def evaluate(
        self, request: DiscountRequestInput, context: AgentContext
    ) -> AggregatedResult:
        """执行完整的折扣评估编排。

        流程：

        1. **并行**跑资格检查与风险评估（`asyncio.gather`，互不依赖）；
        2. 聚合前两者的结果；
        3. **串行**跑折扣建议（依赖前两者的产出）；
        4. 跑通知策略判定。

        Args:
            request: 折扣申请输入。
            context: 共享上下文。

        Returns:
            :class:`AggregatedResult`。**注意它只是评估结果**——
            真正的折扣发放要走 Orchestrator + 控制层 + 工具，
            这个编排器没有任何执行权。
        """
        # —— 阶段一：并行 ——
        # 延迟账：这一阶段的耗时是两者中较慢的那个，不是两者之和。
        parallel = await self.orchestrator.run_parallel(
            {
                self.eligibility.agent_id: (self.eligibility, request),
                self.risk.agent_id: (self.risk, request),
            },
            context,
        )

        if parallel.status == AgentResultStatus.FAILED:
            # 必需项失败 → 整单停，后面的都不跑。
            logger.info(
                "discount_orchestration_stopped",
                task_id=context.task_id,
                reason=parallel.reason,
            )
            return parallel

        # 把前两阶段的产出回填进上下文，供后续 Agent 使用。
        facts = dict(context.extra.get("business_facts", {}))
        for result in parallel.results:
            facts.update(result.result or {})
        enriched = context.model_copy(
            update={"extra": {**context.extra, "business_facts": facts}}
        )

        # —— 阶段二：串行（有依赖）——
        sequential = await self.orchestrator.run_sequential(
            [
                (self.recommendation.agent_id, self.recommendation, request),
                (self.notification.agent_id, self.notification, request),
            ],
            enriched,
            stop_on_failure=True,
        )

        combined = self.orchestrator.aggregate(parallel.results + sequential.results)
        combined.elapsed_ms = parallel.elapsed_ms + sequential.elapsed_ms
        logger.info(
            "discount_orchestration_finished",
            task_id=context.task_id,
            status=str(combined.status),
            elapsed_ms=combined.elapsed_ms,
        )
        return combined


# ======================================================================
# 演示数据
# ======================================================================
DEMO_CUSTOMERS: list[dict[str, Any]] = [
    {
        "customer_id": "C001",
        "name": "张三",
        "tier": "STANDARD",
        "email": "zhangsan@example.com",
        "phone": "13812345678",
        "department": "cs_north",
        "lifetime_value": 25_000.0,
    },
    {
        "customer_id": "C002",
        "name": "李四",
        "tier": "VIP",
        "email": "lisi@example.com",
        "phone": "13987654321",
        "department": "cs_north",
        "lifetime_value": 180_000.0,
    },
    {
        "customer_id": "C003",
        "name": "王五",
        "tier": "STANDARD",
        "email": "wangwu@example.com",
        "phone": "13700001111",
        # 归属不同部门，用于演示数据范围越权拒绝。
        "department": "cs_south",
        "lifetime_value": 5_000.0,
    },
]


async def seed_demo_data(session: Any) -> int:
    """写入演示客户数据（幂等）。

    Args:
        session: 数据库会话。

    Returns:
        新增的客户数量。
    """
    created = 0
    for row in DEMO_CUSTOMERS:
        existing = await session.get(CustomerORM, row["customer_id"])
        if existing is not None:
            continue
        session.add(CustomerORM(**row))
        created += 1
    await session.flush()
    return created
