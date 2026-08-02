"""查询客户信息（只读工具）。

只读工具的特点，以及为什么它们要和写工具明确区分：

* **重试无副作用** → 不需要幂等键，不需要对账，失败了直接重试就行。
* **不需要补偿** → 没有产生任何需要撤销的东西。
* **风险等级低** → 通常不需要审批。

把读写混在一起处理，会导致要么读操作背上不必要的幂等成本，
要么写操作被当成读操作直接重试——后者是会赔钱的。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.actions.base import AgentTool, ToolExecutionContext, ToolExecutionResult
from app.core.enums import ErrorCode, RiskLevel, StepType, ToolExecutionStatus
from app.core.ids import utcnow
from app.security.identity import PERM_CUSTOMER_READ
from app.state.models import CustomerORM, DiscountORM


class QueryCustomerArgs(BaseModel):
    """`query_customer` 的参数模型。

    每个工具都必须有自己的参数 Pydantic 模型——
    **禁止把未经验证的字典直接传进工具**。
    `extra="forbid"` 也很重要：模型偶尔会自作主张多塞一个字段，
    静默忽略它意味着我们不知道模型其实想做别的事。
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(
        min_length=2, max_length=32, description="客户编号，例如 C001"
    )


class QueryCustomerTool(AgentTool):
    """查询客户主数据与当前生效折扣。"""

    name = "query_customer"
    description = "根据客户编号查询客户等级、状态与当前生效折扣。用于折扣申请前的资格核对。"
    risk_level = RiskLevel.NONE
    required_permissions = {PERM_CUSTOMER_READ}
    idempotent = True
    supports_compensation = False
    step_type = StepType.READ
    service_id = "crm_service"
    args_model = QueryCustomerArgs
    default_timeout_seconds = 5.0

    async def execute(
        self,
        arguments: BaseModel,
        execution_context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        """查询客户信息。

        Args:
            arguments: :class:`QueryCustomerArgs` 实例。
            execution_context: 执行上下文。

        Returns:
            成功时 `result` 含客户等级、状态、部门以及当前生效折扣；
            客户不存在时返回 `NOT_FOUND` 且 **retryable=False**——
            重试一万次也不会把一个不存在的客户变出来。
        """
        assert isinstance(arguments, QueryCustomerArgs)
        started = utcnow()
        session = execution_context.session

        customer = await session.get(CustomerORM, arguments.customer_id)
        if customer is None:
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.FAILED,
                error_code=ErrorCode.NOT_FOUND,
                error_message=f"客户不存在：{arguments.customer_id}",
                retryable=False,
                started_at=started,
            )

        result = await session.execute(
            select(DiscountORM)
            .where(DiscountORM.customer_id == arguments.customer_id)
            .where(DiscountORM.status == "ACTIVE")
        )
        active = result.scalars().first()

        return self.build_result(
            execution_context,
            status=ToolExecutionStatus.SUCCESS,
            result={
                "customer_id": customer.customer_id,
                "name": customer.name,
                "tier": customer.tier,
                "status": customer.status,
                "department": customer.department,
                "lifetime_value": customer.lifetime_value,
                # 邮箱手机号**不在这里返回给上层拼进 Prompt**——
                # 需要时由脱敏组件转成代号。这里返回是否存在即可。
                "has_email": bool(customer.email),
                "has_phone": bool(customer.phone),
                "active_discount": (
                    {
                        "discount_id": active.discount_id,
                        "discount_rate": active.discount_rate,
                        "created_at": active.created_at.isoformat(),
                    }
                    if active
                    else None
                ),
            },
            started_at=started,
        )
