"""控制层策略单元测试。

覆盖验收要求的：权限不足、参数超限、高风险审批、未注册工具拒绝。
"""

from __future__ import annotations

from app.cognitive.models import ActionProposal
from app.context.models import AgentContext
from app.control.models import PolicyEvaluationRequest
from app.control.policies import (
    AgentPermissionPolicy,
    ApprovalPolicy,
    BusinessRulePolicy,
    DataAccessPolicy,
    IdentityPolicy,
    ParameterPolicy,
    RateLimitPolicy,
    RiskPolicy,
    SensitiveDataPolicy,
    ToolPermissionPolicy,
)
from app.core.enums import ApprovalType, DecisionType, RiskLevel
from app.core.ids import new_trace_id
from app.security.identity import MockIdentityProvider, ResolvedIdentity


async def _identity(user_id: str = "user_001", agent_id: str = "discount_agent", service_id: str | None = "billing_service") -> ResolvedIdentity:
    provider = MockIdentityProvider()
    return ResolvedIdentity(
        user=await provider.get_user(user_id),
        agent=await provider.get_agent(agent_id),
        service=await provider.get_service(service_id) if service_id else None,
    )


def _context(identity: ResolvedIdentity, **extra) -> AgentContext:
    return AgentContext(
        task_id="task_test", trace_id=new_trace_id(),
        user_id=identity.user.user_id, agent_id=identity.agent.agent_id,
        identity=identity, user_input="给客户 C001 打九折", extra=extra,
    )


async def _request(
    *, tool_name: str = "apply_discount", arguments: dict | None = None,
    identity: ResolvedIdentity | None = None, registered: bool = True,
    # 默认与真实的 ApplyDiscountTool 一致：静态基线是 MEDIUM，
    # 实际风险由 RiskPolicy 按 discount_rate 向上抬。
    risk: RiskLevel = RiskLevel.MEDIUM, perms: set[str] | None = None,
    is_write: bool = True, facts: dict | None = None, approval_granted: bool = False,
    context_extra: dict | None = None,
) -> PolicyEvaluationRequest:
    identity = identity or await _identity()
    return PolicyEvaluationRequest(
        proposal=ActionProposal(
            intent="apply_discount", tool_name=tool_name,
            arguments=arguments if arguments is not None else {"customer_id": "C001", "discount_rate": 0.05},
            confidence=0.9,
        ),
        identity=identity,
        context=_context(identity, **(context_extra or {})),
        tool_name=tool_name, tool_registered=registered, tool_risk_level=risk,
        tool_required_permissions=perms if perms is not None else {"discount:apply"},
        tool_is_write=is_write,
        business_facts=facts if facts is not None else {"customer_department": "cs_north", "customer_tier": "STANDARD"},
        approval_granted=approval_granted,
    )


class TestIdentityPolicy:
    async def test_unknown_user_denied(self) -> None:
        identity = await _identity(user_id="nobody_999")
        result = await IdentityPolicy().evaluate(await _request(identity=identity))
        assert result.decision == DecisionType.DENY
        assert result.reason_code == "IDENTITY_NO_ROLE"

    async def test_readonly_service_cannot_write(self) -> None:
        """三方交集里的第三方：服务账号只读就不能执行写操作。"""
        identity = await _identity(service_id="crm_service")
        result = await IdentityPolicy().evaluate(await _request(identity=identity))
        assert result.decision == DecisionType.DENY
        assert result.reason_code == "SERVICE_ACCOUNT_READ_ONLY"


class TestAgentPermissionPolicy:
    async def test_unregistered_tool_denied(self) -> None:
        """未注册工具必须被拒——这是模型幻觉工具名的落点。"""
        result = await AgentPermissionPolicy().evaluate(
            await _request(tool_name="refund_payment", registered=False)
        )
        assert result.decision == DecisionType.DENY
        assert result.reason_code == "TOOL_NOT_REGISTERED"
        assert result.metadata["signal"] == "possible_hallucination"

    async def test_tool_not_in_agent_whitelist(self) -> None:
        """场景七：模型试图切换到一个不在白名单里的工具。"""
        result = await AgentPermissionPolicy().evaluate(
            await _request(tool_name="refund_payment", registered=True)
        )
        assert result.decision == DecisionType.DENY
        assert result.reason_code == "TOOL_NOT_IN_AGENT_WHITELIST"

    async def test_admin_cannot_bypass_agent_whitelist(self) -> None:
        """即使操作人是管理员，也不能让只读 Agent 执行折扣发放。

        我们授权给 Agent 的只是用户权限的一个子集。
        """
        identity = await _identity(user_id="admin_001", agent_id="readonly_agent")
        result = await AgentPermissionPolicy().evaluate(await _request(identity=identity))
        assert result.decision == DecisionType.DENY
        assert result.reason_code == "TOOL_NOT_IN_AGENT_WHITELIST"

    async def test_risk_exceeds_agent_limit(self) -> None:
        identity = await _identity(agent_id="readonly_agent")
        result = await AgentPermissionPolicy().evaluate(
            await _request(tool_name="query_customer", identity=identity, risk=RiskLevel.CRITICAL)
        )
        assert result.decision == DecisionType.DENY
        assert result.reason_code == "TOOL_RISK_EXCEEDS_AGENT_LIMIT"


class TestToolPermissionPolicy:
    async def test_permission_intersection(self) -> None:
        """有效权限 = 用户 ∩ Agent ∩ 服务账号。"""
        result = await ToolPermissionPolicy().evaluate(
            await _request(perms={"refund:issue"})
        )
        assert result.decision == DecisionType.DENY
        assert result.reason_code == "PERMISSION_INSUFFICIENT"
        assert "refund:issue" in result.missing_permissions
        # 对外话术不暴露内部权限名
        assert "refund:issue" not in result.human_readable_reason

    async def test_sufficient_permission_allows(self) -> None:
        result = await ToolPermissionPolicy().evaluate(await _request())
        assert result.decision == DecisionType.ALLOW


class TestParameterPolicy:
    async def test_out_of_range_rejected(self, registry) -> None:
        """参数超限：discount_rate = 3.0 被 Pydantic 挡下。"""
        result = await ParameterPolicy(registry).evaluate(
            await _request(arguments={"customer_id": "C001", "discount_rate": 3.0})
        )
        assert result.decision == DecisionType.DENY
        assert result.reason_code == "PARAMETER_INVALID"
        assert result.metadata["errors"][0]["field"] == "discount_rate"

    async def test_extra_field_rejected(self, registry) -> None:
        """模型多塞一个字段必须报错，不能静默忽略。"""
        result = await ParameterPolicy(registry).evaluate(
            await _request(arguments={"customer_id": "C001", "discount_rate": 0.05, "bypass_approval": True})
        )
        assert result.decision == DecisionType.DENY

    async def test_valid_arguments_normalized(self, registry) -> None:
        result = await ParameterPolicy(registry).evaluate(await _request())
        assert result.decision == DecisionType.ALLOW
        assert result.validated_arguments["customer_id"] == "C001"
        assert result.validated_arguments["reason"] == ""  # 默认值被填充


class TestBusinessRulePolicy:
    async def test_within_self_service_limit(self, settings) -> None:
        """场景一：5% 折扣，普通客服可自助批准。"""
        req = await _request(arguments={"customer_id": "C001", "discount_rate": 0.05})
        req = req.model_copy(update={"validated_arguments": req.proposal.arguments})
        result = await BusinessRulePolicy(settings).evaluate(req)
        assert result.decision == DecisionType.ALLOW

    async def test_requires_manager_approval(self, settings) -> None:
        """场景二：10% 折扣需要经理审批。"""
        req = await _request(arguments={"customer_id": "C001", "discount_rate": 0.10})
        req = req.model_copy(update={"validated_arguments": req.proposal.arguments})
        result = await BusinessRulePolicy(settings).evaluate(req)
        assert result.decision == DecisionType.REQUIRE_APPROVAL
        assert result.approval_type == ApprovalType.MANAGER

    async def test_hard_denial_above_absolute_limit(self, settings) -> None:
        """场景三：30% 折扣直接拒绝，**审批也不能覆盖**。"""
        req = await _request(arguments={"customer_id": "C001", "discount_rate": 0.30})
        req = req.model_copy(update={"validated_arguments": req.proposal.arguments, "approval_granted": True})
        result = await BusinessRulePolicy(settings).evaluate(req)
        assert result.decision == DecisionType.DENY
        assert result.reason_code == "DISCOUNT_EXCEEDS_ABSOLUTE_LIMIT"
        assert result.metadata["override_allowed"] is False

    async def test_vip_gets_wider_self_service_range(self, settings) -> None:
        """VIP 客户放宽额度，但仍要过控制层。"""
        req = await _request(
            arguments={"customer_id": "C002", "discount_rate": 0.07},
            facts={"customer_tier": "VIP", "customer_department": "cs_north"},
        )
        req = req.model_copy(update={"validated_arguments": req.proposal.arguments})
        result = await BusinessRulePolicy(settings).evaluate(req)
        assert result.decision == DecisionType.ALLOW
        assert result.metadata["vip_bonus_applied"] is True

    async def test_vip_still_bounded_by_absolute_limit(self, settings) -> None:
        """VIP 不能突破绝对上限——否则它就不是硬上限了。"""
        req = await _request(
            arguments={"customer_id": "C002", "discount_rate": 0.20},
            facts={"customer_tier": "VIP", "customer_department": "cs_north"},
        )
        req = req.model_copy(update={"validated_arguments": req.proposal.arguments})
        result = await BusinessRulePolicy(settings).evaluate(req)
        assert result.decision == DecisionType.DENY

    async def test_duplicate_active_discount_denied(self, settings) -> None:
        req = await _request(
            facts={
                "customer_tier": "STANDARD", "customer_department": "cs_north",
                "active_discount": {"discount_id": "d1", "discount_rate": 0.05},
            },
        )
        req = req.model_copy(update={"validated_arguments": req.proposal.arguments})
        result = await BusinessRulePolicy(settings).evaluate(req)
        assert result.decision == DecisionType.DENY
        assert result.reason_code == "DUPLICATE_ACTIVE_DISCOUNT"


class TestDataAccessPolicy:
    async def test_cross_department_denied(self) -> None:
        """RBAC 通过但数据范围越界——这是只做 RBAC 的系统的典型漏洞。"""
        req = await _request(
            arguments={"customer_id": "C003", "discount_rate": 0.03},
            facts={"customer_department": "cs_south"},
        )
        req = req.model_copy(update={"validated_arguments": req.proposal.arguments})
        result = await DataAccessPolicy().evaluate(req)
        assert result.decision == DecisionType.DENY
        assert result.reason_code == "DATA_SCOPE_VIOLATION"
        # 话术不区分「不存在」和「无权限」，防止枚举探测
        assert "不存在" not in result.human_readable_reason

    async def test_unverifiable_scope_goes_to_human(self) -> None:
        """查不到归属就转人工。「查不到就放行」是数据越权最常见的成因。"""
        req = await _request(facts={})
        req = req.model_copy(update={"validated_arguments": req.proposal.arguments})
        result = await DataAccessPolicy().evaluate(req)
        assert result.decision == DecisionType.MANUAL_REVIEW

    async def test_admin_full_scope(self) -> None:
        identity = await _identity(user_id="admin_001")
        req = await _request(identity=identity)
        req = req.model_copy(update={"validated_arguments": req.proposal.arguments})
        result = await DataAccessPolicy().evaluate(req)
        assert result.decision == DecisionType.ALLOW


class TestSensitiveDataPolicy:
    async def test_id_card_in_arguments_denied(self) -> None:
        req = await _request(
            arguments={"customer_id": "C001", "discount_rate": 0.05, "reason": "身份证 110101199001011234"}
        )
        req = req.model_copy(update={"validated_arguments": req.proposal.arguments})
        result = await SensitiveDataPolicy().evaluate(req)
        assert result.decision == DecisionType.DENY
        assert result.reason_code == "SENSITIVE_DATA_IN_ARGUMENTS"
        # 审计里只记字段名，绝不记原文
        assert "110101199001011234" not in str(result.metadata)

    async def test_phone_is_flagged_not_blocked(self) -> None:
        req = await _request(
            arguments={"customer_id": "C001", "discount_rate": 0.05, "reason": "联系 13812345678"}
        )
        req = req.model_copy(update={"validated_arguments": req.proposal.arguments})
        result = await SensitiveDataPolicy().evaluate(req)
        assert result.decision == DecisionType.ALLOW
        assert "phone" in result.metadata["pii_fields"]


class TestRiskPolicy:
    async def test_model_hint_can_only_escalate(self, settings) -> None:
        """模型说高就至少按高算；说低我们不理会。"""
        req = await _request(risk=RiskLevel.LOW)
        req.proposal.risk_hint = RiskLevel.CRITICAL
        result = await RiskPolicy(settings).evaluate(req)
        assert result.risk_level == RiskLevel.CRITICAL

    async def test_model_cannot_downgrade_risk(self, settings) -> None:
        req = await _request(risk=RiskLevel.HIGH)
        req.proposal.risk_hint = RiskLevel.NONE
        result = await RiskPolicy(settings).evaluate(req)
        assert result.risk_level.order >= RiskLevel.HIGH.order

    async def test_suspicious_input_escalates(self, settings) -> None:
        req = await _request(context_extra={"input_suspicious": True})
        result = await RiskPolicy(settings).evaluate(req)
        assert "suspicious_input" in result.metadata["risk_factors"]

    async def test_low_confidence_write_goes_to_human(self, settings) -> None:
        req = await _request()
        req.proposal.confidence = 0.2
        result = await RiskPolicy(settings).evaluate(req)
        assert result.decision == DecisionType.MANUAL_REVIEW


class TestApprovalPolicy:
    async def test_high_risk_write_requires_approval(self) -> None:
        result = await ApprovalPolicy().evaluate(await _request(risk=RiskLevel.HIGH))
        assert result.decision == DecisionType.REQUIRE_APPROVAL

    async def test_read_never_requires_approval(self) -> None:
        """读操作不需要审批——审批疲劳会让真正重要的审批被随手点过。"""
        result = await ApprovalPolicy().evaluate(
            await _request(tool_name="query_customer", risk=RiskLevel.HIGH, is_write=False)
        )
        assert result.decision == DecisionType.ALLOW

    async def test_granted_approval_short_circuits(self) -> None:
        result = await ApprovalPolicy().evaluate(await _request(approval_granted=True))
        assert result.decision == DecisionType.ALLOW


class TestRateLimitPolicy:
    async def test_write_rate_limit(self) -> None:
        policy = RateLimitPolicy(max_writes_per_minute=2)
        req = await _request()
        for _ in range(2):
            policy.record_write("user_001")
        result = await policy.evaluate(req)
        assert result.decision == DecisionType.DENY
        assert result.reason_code == "RATE_LIMIT_EXCEEDED"
        assert result.metadata["retry_after_seconds"] > 0

    async def test_circuit_breaker_goes_to_human(self) -> None:
        """熔断是 MANUAL_REVIEW 不是 DENY：这不是这笔请求的错。"""
        policy = RateLimitPolicy(circuit_failure_threshold=3)
        for _ in range(3):
            policy.record_failure("apply_discount")
        result = await policy.evaluate(await _request())
        assert result.decision == DecisionType.MANUAL_REVIEW
        assert result.reason_code == "CIRCUIT_BREAKER_OPEN"


class TestRiskLevelOrdering:
    """RiskLevel 比较运算的回归测试。

    背景：StrEnum 继承 str，自带字典序比较。如果只重写 `__lt__`，
    `max()` 会走 str 的 `__gt__`，按字典序判定 "MEDIUM" > "HIGH"，
    于是「取最高风险」变成「取字母序最大的风险」，
    一个 HIGH 风险的写操作会被静默降级成 MEDIUM 从而绕过审批。

    这类 Bug 不报错、没有异常栈，只会让审批悄悄失效——
    所以必须有专门的测试钉住它。
    """

    def test_max_returns_highest_risk_not_lexicographic(self) -> None:
        assert max(RiskLevel.HIGH, RiskLevel.MEDIUM) == RiskLevel.HIGH
        assert max(RiskLevel.LOW, RiskLevel.HIGH) == RiskLevel.HIGH
        assert max(RiskLevel.CRITICAL, RiskLevel.MEDIUM) == RiskLevel.CRITICAL
        # 字典序陷阱的具体反例：按字母序 MEDIUM > HIGH > CRITICAL > LOW
        assert max(RiskLevel.CRITICAL, RiskLevel.LOW) == RiskLevel.CRITICAL

    def test_all_comparison_operators(self) -> None:
        assert RiskLevel.LOW < RiskLevel.HIGH
        assert RiskLevel.HIGH > RiskLevel.LOW
        assert RiskLevel.HIGH >= RiskLevel.HIGH
        assert RiskLevel.MEDIUM <= RiskLevel.HIGH
        assert not (RiskLevel.MEDIUM > RiskLevel.HIGH)

    def test_sorted_order(self) -> None:
        levels = [RiskLevel.HIGH, RiskLevel.NONE, RiskLevel.CRITICAL, RiskLevel.LOW, RiskLevel.MEDIUM]
        assert sorted(levels) == [
            RiskLevel.NONE, RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL,
        ]


class TestApprovalDowngradeInEngine:
    """审批通过后的降级语义（由 PolicyEngine 统一实现）。

    这条语义必须放在引擎里而不是每条策略各写一遍：
    任何策略都可能要求审批，漏掉一条就会「批了之后又要批」。
    """

    async def test_approval_downgrades_require_approval(self, registry, settings, rate_limiter) -> None:
        from app.control.policy_engine import build_default_policy_engine

        engine = build_default_policy_engine(registry, settings=settings, rate_limiter=rate_limiter)
        req = await _request(arguments={"customer_id": "C001", "discount_rate": 0.10})

        before = await engine.evaluate(req)
        assert before.decision == DecisionType.REQUIRE_APPROVAL

        after = await engine.evaluate(req.model_copy(update={"approval_granted": True}))
        assert after.decision == DecisionType.ALLOW
        assert any("__APPROVED" in e.reason_code for e in after.evaluations)

    async def test_approval_never_downgrades_deny(self, registry, settings, rate_limiter) -> None:
        """**审批覆盖不了硬拒绝。** 30% 折扣即使经理点了同意也不放行。"""
        from app.control.policy_engine import build_default_policy_engine

        engine = build_default_policy_engine(registry, settings=settings, rate_limiter=rate_limiter)
        req = await _request(
            arguments={"customer_id": "C001", "discount_rate": 0.30}, approval_granted=True
        )
        decision = await engine.evaluate(req)
        assert decision.decision == DecisionType.DENY
        assert decision.reason_code == "DISCOUNT_EXCEEDS_ABSOLUTE_LIMIT"

    async def test_five_percent_allowed_without_approval(self, registry, settings, rate_limiter) -> None:
        """场景一：5% 折扣在自助额度内，直接放行，不触发审批。"""
        from app.control.policy_engine import build_default_policy_engine

        engine = build_default_policy_engine(registry, settings=settings, rate_limiter=rate_limiter)
        decision = await engine.evaluate(
            await _request(arguments={"customer_id": "C001", "discount_rate": 0.05})
        )
        assert decision.decision == DecisionType.ALLOW
