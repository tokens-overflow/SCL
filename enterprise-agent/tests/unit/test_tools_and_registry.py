"""工具与注册表单元测试。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.actions.base import AgentTool, ToolExecutionContext
from app.actions.registry import ToolRegistry
from app.actions.tools.apply_discount import ApplyDiscountArgs, ApplyDiscountTool
from app.actions.tools.send_notification import SendNotificationTool
from app.control.parameter_validator import validate_tool_arguments
from app.core.enums import RiskLevel, StepType, ToolExecutionStatus
from app.core.errors import ToolNotRegisteredError, ValidationError
from app.security.identity import MockIdentityProvider, ResolvedIdentity


class TestRegistry:
    def test_unregistered_tool_raises(self, registry: ToolRegistry) -> None:
        with pytest.raises(ToolNotRegisteredError) as exc:
            registry.get("refund_payment")
        assert exc.value.details["tool_name"] == "refund_payment"

    def test_duplicate_registration_rejected(self, registry: ToolRegistry) -> None:
        """重名默认报错，不静默覆盖——静默覆盖是一类很难查的事故。"""
        with pytest.raises(ValueError, match="工具名冲突"):
            registry.register(ApplyDiscountTool())

    def test_non_idempotent_write_tool_rejected(self, registry: ToolRegistry) -> None:
        """写操作必须幂等，否则注册就失败——与其在生产上发现，不如现在拒绝。"""

        class BadArgs(BaseModel):
            x: int = 0

        class BadTool(AgentTool):
            name = "bad_write_tool"
            description = "不幂等的写工具"
            step_type = StepType.WRITE
            idempotent = False
            args_model = BadArgs

            async def execute(self, arguments, execution_context):  # noqa: ANN001
                raise NotImplementedError

        with pytest.raises(ValueError, match="必须支持幂等"):
            registry.register(BadTool())

    def test_tool_without_args_model_rejected(self, registry: ToolRegistry) -> None:
        class NoArgsTool(AgentTool):
            name = "no_args_tool"
            description = "没有参数模型"

            async def execute(self, arguments, execution_context):  # noqa: ANN001
                raise NotImplementedError

        with pytest.raises(ValueError, match="args_model"):
            registry.register(NoArgsTool())

    async def test_agent_whitelist_filters_visibility(self, registry: ToolRegistry) -> None:
        provider = MockIdentityProvider()
        readonly = ResolvedIdentity(
            user=await provider.get_user("admin_001"),
            agent=await provider.get_agent("readonly_agent"),
        )
        names = {t.name for t in registry.visible_to_agent(readonly.agent)}
        assert names == {"query_customer"}

    async def test_callable_by_respects_permissions(self, registry: ToolRegistry) -> None:
        provider = MockIdentityProvider()
        identity = ResolvedIdentity(
            user=await provider.get_user("user_001"),
            agent=await provider.get_agent("discount_agent"),
        )
        names = {t.name for t in registry.callable_by(identity)}
        assert "apply_discount" in names
        assert "refund_payment" not in names

    def test_describe_hides_permissions(self, registry: ToolRegistry) -> None:
        """给模型/调用方的工具描述里不含 required_permissions。"""
        for desc in registry.describe_all():
            assert "required_permissions" not in desc


class TestToolArgumentValidation:
    def test_valid(self) -> None:
        tool = ApplyDiscountTool()
        args = validate_tool_arguments(tool, {"customer_id": "C001", "discount_rate": 0.05})
        assert isinstance(args, ApplyDiscountArgs)
        assert args.discount_rate == 0.05

    def test_out_of_range(self) -> None:
        tool = ApplyDiscountTool()
        with pytest.raises(ValidationError) as exc:
            validate_tool_arguments(tool, {"customer_id": "C001", "discount_rate": 5.0})
        assert exc.value.details["errors"][0]["field"] == "discount_rate"

    def test_extra_field_forbidden(self) -> None:
        tool = ApplyDiscountTool()
        with pytest.raises(ValidationError):
            validate_tool_arguments(tool, {"customer_id": "C001", "discount_rate": 0.05, "hack": 1})

    def test_missing_required(self) -> None:
        tool = ApplyDiscountTool()
        with pytest.raises(ValidationError):
            validate_tool_arguments(tool, {"customer_id": "C001"})


class TestToolContracts:
    def test_notification_is_not_compensable(self) -> None:
        """已发出的短信收不回来——如实声明，不假装能补偿。"""
        assert SendNotificationTool.supports_compensation is False
        assert SendNotificationTool.step_type == StepType.NOTIFY

    def test_discount_is_compensable_and_idempotent(self) -> None:
        assert ApplyDiscountTool.supports_compensation is True
        assert ApplyDiscountTool.idempotent is True
        # 静态风险等级是**基线**：实际等级由 RiskPolicy 按 discount_rate 向上抬。
        # 如果这里写死 HIGH，连 1% 的折扣也要走审批，而审批疲劳会让
        # 真正重要的审批被随手点过。
        assert ApplyDiscountTool.risk_level == RiskLevel.MEDIUM

    async def test_notification_compensate_raises(self, seeded_session) -> None:
        tool = SendNotificationTool()
        ctx = ToolExecutionContext(
            task_id="t", step_id="s", step_name="n", execution_id="e",
            idempotency_key="k", session=seeded_session,
        )
        from app.actions.base import ToolExecutionResult

        previous = ToolExecutionResult(
            tool_name="send_notification", execution_id="e",
            status=ToolExecutionStatus.SUCCESS,
        )
        with pytest.raises(NotImplementedError, match="不支持补偿"):
            await tool.compensate(previous, ctx)


class TestToolExecution:
    async def test_discount_idempotent_write(self, seeded_session) -> None:
        """同一个幂等键写两次，第二次命中幂等返回原结果。"""
        tool = ApplyDiscountTool()
        ctx = ToolExecutionContext(
            task_id="t1", step_id="s1", step_name="apply_discount", execution_id="e1",
            idempotency_key="idem-abc", user_id="user_001", session=seeded_session,
        )
        args = ApplyDiscountArgs(customer_id="C001", discount_rate=0.05)

        first = await tool.execute(args, ctx)
        assert first.status == ToolExecutionStatus.SUCCESS
        assert first.external_reference_id

        second = await tool.execute(args, ctx)
        assert second.status == ToolExecutionStatus.SKIPPED_IDEMPOTENT
        assert second.external_reference_id == first.external_reference_id

    async def test_duplicate_active_discount_rejected_by_downstream(self, seeded_session) -> None:
        """下游系统自己也守住「不能重复创建」——不能假设调用方都守规矩。"""
        tool = ApplyDiscountTool()
        args = ApplyDiscountArgs(customer_id="C001", discount_rate=0.05)
        ctx1 = ToolExecutionContext(
            task_id="t1", step_id="s1", step_name="apply_discount", execution_id="e1",
            idempotency_key="k1", session=seeded_session,
        )
        await tool.execute(args, ctx1)

        ctx2 = ToolExecutionContext(
            task_id="t2", step_id="s2", step_name="apply_discount", execution_id="e2",
            idempotency_key="k2", session=seeded_session,
        )
        second = await tool.execute(ApplyDiscountArgs(customer_id="C001", discount_rate=0.03), ctx2)
        assert second.status == ToolExecutionStatus.FAILED
        assert second.error_code == "DUPLICATE_ACTIVE_DISCOUNT"
        assert second.retryable is False

    async def test_compensation_revokes_discount(self, seeded_session) -> None:
        """补偿是一次**新的业务动作**：把折扣改成 REVOKED，不是数据库回滚。"""
        tool = ApplyDiscountTool()
        ctx = ToolExecutionContext(
            task_id="t1", step_id="s1", step_name="apply_discount", execution_id="e1",
            idempotency_key="k-comp", session=seeded_session,
        )
        result = await tool.execute(ApplyDiscountArgs(customer_id="C001", discount_rate=0.05), ctx)
        assert result.status == ToolExecutionStatus.SUCCESS

        comp_ctx = ctx.model_copy(update={"idempotency_key": "k-comp:comp", "execution_id": "e2"})
        comp = await tool.compensate(result, comp_ctx)
        assert comp.status == ToolExecutionStatus.SUCCESS
        assert comp.result["status"] == "REVOKED"

        # 补偿必须幂等：再撤一次也是成功。
        comp2 = await tool.compensate(result, comp_ctx)
        assert comp2.succeeded

    async def test_reconcile_finds_committed_write(self, seeded_session) -> None:
        """对账：拿幂等键查到已生效的写入。"""
        tool = ApplyDiscountTool()
        ctx = ToolExecutionContext(
            task_id="t1", step_id="s1", step_name="apply_discount", execution_id="e1",
            idempotency_key="k-recon", session=seeded_session,
        )
        await tool.execute(ApplyDiscountArgs(customer_id="C001", discount_rate=0.05), ctx)

        found = await tool.query_external_status("k-recon", ctx)
        assert found is not None and found.succeeded

        missing = await tool.query_external_status("k-never-used", ctx)
        assert missing is not None
        assert missing.status == ToolExecutionStatus.FAILED
        assert missing.retryable is True
