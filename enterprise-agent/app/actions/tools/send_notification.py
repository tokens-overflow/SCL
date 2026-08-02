"""发送客户通知（写工具 · 幂等 · **不可补偿**）。

这个工具存在的教学价值，全在 `supports_compensation = False` 这一行上。

**不可补偿的动作必须排在链路最后。**

短信一旦发出去就收不回来了。如果通知排在折扣前面，
那么折扣失败时你面对的局面是：客户已经收到「您的折扣已生效」的短信，
而折扣根本没生效。你能做的只有再发一条道歉短信——那不是补偿，那是善后。

由此引出「补偿的三条纪律」中的第二条：
**不可补偿的动作排在链路最后**（另外两条是「补偿本身必须幂等」
和「补偿不给模型调用权」）。

第二个教学点：这个工具是**非关键步骤**。
折扣成功但通知失败时，正确的处置是任务落 PARTIAL_SUCCESS + 通知单独重试，
**绝不是去撤销折扣**。「是否撤销折扣」必须由明确业务规则或人工决定，
不能因为一个下游通道抖动就自动回滚一笔已经生效的业务。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.actions.base import AgentTool, ToolExecutionContext, ToolExecutionResult
from app.actions.tools.fault_injection import fault_injector
from app.control.data_masking import mask_email, mask_phone
from app.core.enums import ErrorCode, RiskLevel, StepType, ToolExecutionStatus
from app.core.errors import ToolTimeoutError
from app.core.ids import new_id, utcnow
from app.security.identity import PERM_NOTIFICATION_SEND
from app.state.models import CustomerORM, NotificationORM

#: 通知模板。**内容由程序套模板生成，不由模型自由发挥。**
#: 原因：模板里的金额、折扣率是事实，事实不能让模型编。
#: 模型负责组织语言，不负责陈述事实。
TEMPLATES: dict[str, str] = {
    "discount_applied": "尊敬的{name}，您的专属折扣已生效，折扣幅度 {rate}。感谢您的支持。",
    "discount_revoked": "尊敬的{name}，您此前的专属折扣已按业务规则撤销，如有疑问请联系客服。",
    "generic": "尊敬的{name}，您的业务请求已处理完成。",
}


class SendNotificationArgs(BaseModel):
    """`send_notification` 的参数模型。"""

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=2, max_length=32)
    channel: Literal["sms", "email"] = "sms"
    template: Literal["discount_applied", "discount_revoked", "generic"] = "generic"
    #: 模板变量。**不接受自由文本正文**——
    #: 如果允许调用方（最终是模型）直接指定短信正文，
    #: 就等于把「对外说什么」的决定权交给了模型。
    template_vars: dict[str, str] = Field(default_factory=dict)


class SendNotificationTool(AgentTool):
    """向客户发送通知。"""

    name = "send_notification"
    description = (
        "向客户发送短信或邮件通知。只能使用预定义模板，不接受自由正文。"
        "该动作对外可见且不可撤回，应放在流程最后一步。"
    )
    risk_level = RiskLevel.MEDIUM
    required_permissions = {PERM_NOTIFICATION_SEND}
    idempotent = True
    #: 关键声明：**不可补偿**。已发出的短信收不回来。
    supports_compensation = False
    step_type = StepType.NOTIFY
    service_id = "notification_service"
    args_model = SendNotificationArgs
    default_timeout_seconds = 6.0

    async def execute(
        self,
        arguments: BaseModel,
        execution_context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        """发送通知。

        Returns:
            成功时 `result` 含通知单号与**脱敏后的**收件地址。
            日志和审计里绝不出现完整手机号或邮箱。
        """
        assert isinstance(arguments, SendNotificationArgs)
        started = utcnow()
        session = execution_context.session
        idem_key = execution_context.idempotency_key

        fault = fault_injector.take(self.name)
        if fault == "permanent_failure":
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.FAILED,
                error_code=ErrorCode.INVALID_ARGUMENT,
                error_message="客户已退订该渠道通知，不可重试",
                retryable=False,
                started_at=started,
            )
        if fault == "transient_failure":
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.FAILED,
                error_code=ErrorCode.UPSTREAM_UNAVAILABLE,
                error_message="短信网关暂时不可用（模拟 503）",
                retryable=True,
                started_at=started,
            )
        if fault == "timeout_before_commit":
            raise ToolTimeoutError("通知发送超时，结果未知", details={"idempotency_key": idem_key})

        # 幂等：同一个键只发一次。通知的重复发送虽然不涉及金钱，
        # 但「客户连收三条一样的短信」同样是事故。
        existing = await self._find_by_key(session, idem_key)
        if existing is not None:
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.SKIPPED_IDEMPOTENT,
                result={"notification_id": existing.notification_id, "status": existing.status},
                external_reference_id=existing.notification_id,
                started_at=started,
            )

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

        recipient = customer.email if arguments.channel == "email" else customer.phone
        if not recipient:
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.FAILED,
                error_code=ErrorCode.INVALID_ARGUMENT,
                error_message=f"客户未登记 {arguments.channel} 联系方式",
                retryable=False,
                started_at=started,
            )

        template = TEMPLATES.get(arguments.template, TEMPLATES["generic"])
        content = template.format(
            name=customer.name,
            rate=arguments.template_vars.get("rate", ""),
            **{k: v for k, v in arguments.template_vars.items() if k != "rate"},
        )

        notification = NotificationORM(
            notification_id=new_id("noti"),
            customer_id=arguments.customer_id,
            channel=arguments.channel,
            template=arguments.template,
            content=content,
            status="SENT",
            idempotency_key=idem_key,
        )
        session.add(notification)
        await session.flush()

        masked = (
            mask_email(recipient) if arguments.channel == "email" else mask_phone(recipient)
        )
        return self.build_result(
            execution_context,
            status=ToolExecutionStatus.SUCCESS,
            result={
                "notification_id": notification.notification_id,
                "channel": arguments.channel,
                # 只回传脱敏后的地址。上层会把它写进审计和上下文，
                # 完整手机号绝不能出现在那些地方。
                "recipient_masked": masked,
            },
            external_reference_id=notification.notification_id,
            started_at=started,
        )

    async def query_external_status(
        self,
        idempotency_key: str,
        execution_context: ToolExecutionContext,
    ) -> ToolExecutionResult | None:
        """对账：查询这条通知是否已经发出。

        通知虽然不涉及金钱，但同样需要对账——
        「超时了到底发没发出去」决定了要不要重试，
        而重复发送对客户来说也是事故。
        """
        session = execution_context.session
        existing = await self._find_by_key(session, idempotency_key)
        if existing is not None:
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.SUCCESS,
                result={"notification_id": existing.notification_id, "status": existing.status},
                external_reference_id=existing.notification_id,
            )
        return self.build_result(
            execution_context,
            status=ToolExecutionStatus.FAILED,
            error_code=ErrorCode.UPSTREAM_UNAVAILABLE,
            error_message="对账确认：通知未发出，可安全重试",
            retryable=True,
        )

    # compensate() 刻意不实现：继承基类的 NotImplementedError。
    # 这不是偷懒，而是**如实声明「这个动作不可逆」**。
    # 假装能补偿（比如发一条「上一条作废」的短信）会让上层误以为链路是可回滚的，
    # 从而把它排到链路中间——那才是真正的坑。

    @staticmethod
    async def _find_by_key(session, idem_key: str) -> NotificationORM | None:  # noqa: ANN001
        result = await session.execute(
            select(NotificationORM).where(NotificationORM.idempotency_key == idem_key)
        )
        return result.scalars().first()
