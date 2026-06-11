"""全部工具的 Pydantic v2 入参/出参模型。

工具的 JSON Schema 直接由入参模型的 model_json_schema() 生成（见 tools.py），
出参模型统一经 model_dump_json() 序列化后作为 tool_result 返回给模型。
"""
from typing import Literal

from pydantic import BaseModel, Field

from backend.models import MemberLevel, OrderStatus, TicketPriority

# ---------------------------------------------------------------------------
# 1. verify_identity
# ---------------------------------------------------------------------------


class VerifyIdentityInput(BaseModel):
    """核身入参"""
    phone: str = Field(description="用户注册手机号，11 位数字", pattern=r"^1\d{10}$")


class VerifyIdentityResult(BaseModel):
    """核身结果"""
    verified: bool
    user_id: int | None = None
    name: str | None = None
    member_level: MemberLevel | None = None
    message: str


# ---------------------------------------------------------------------------
# 2. get_user_profile
# ---------------------------------------------------------------------------


class GetUserProfileInput(BaseModel):
    user_id: int = Field(description="核身后获得的用户 ID")


class UserProfileResult(BaseModel):
    user_id: int
    name: str
    phone_masked: str = Field(description="脱敏手机号，如 138****0001")
    email: str
    member_level: MemberLevel
    registered_at: str


# ---------------------------------------------------------------------------
# 3. list_orders
# ---------------------------------------------------------------------------


class ListOrdersInput(BaseModel):
    user_id: int = Field(description="核身后获得的用户 ID")
    status: OrderStatus | None = Field(
        default=None,
        description="可选，按状态筛选：pending_shipment 待发货 / in_transit 运输中 / "
                    "delivered 已签收 / refunded 已退款",
    )


class OrderSummary(BaseModel):
    order_no: str
    product_name: str
    quantity: int
    amount: float
    status: OrderStatus
    created_at: str


class ListOrdersResult(BaseModel):
    total: int
    orders: list[OrderSummary]


# ---------------------------------------------------------------------------
# 4. get_order_detail
# ---------------------------------------------------------------------------


class GetOrderDetailInput(BaseModel):
    order_no: str = Field(description="订单号，形如 ORD2026060001")


class OrderDetailResult(BaseModel):
    order_no: str
    product_name: str
    quantity: int
    amount: float
    status: OrderStatus
    shipping_address: str
    carrier: str | None
    tracking_no: str | None
    created_at: str
    refund_status: str | None = Field(
        default=None, description="若该订单存在退款单，此处为其状态"
    )


# ---------------------------------------------------------------------------
# 5. get_logistics
# ---------------------------------------------------------------------------


class GetLogisticsInput(BaseModel):
    order_no: str = Field(description="订单号，形如 ORD2026060001")


class LogisticsEvent(BaseModel):
    time: str
    location: str
    description: str


class GetLogisticsResult(BaseModel):
    order_no: str
    status: OrderStatus
    carrier: str | None
    tracking_no: str | None
    events: list[LogisticsEvent]
    message: str


# ---------------------------------------------------------------------------
# 6. update_shipping_address
# ---------------------------------------------------------------------------


class UpdateShippingAddressInput(BaseModel):
    order_no: str = Field(description="订单号，形如 ORD2026060001")
    new_address: str = Field(description="新的收货地址（省市区 + 详细地址）", min_length=8)


class UpdateShippingAddressResult(BaseModel):
    success: bool
    order_no: str
    old_address: str | None = None
    new_address: str | None = None
    message: str


# ---------------------------------------------------------------------------
# 7. create_refund
# ---------------------------------------------------------------------------


class CreateRefundInput(BaseModel):
    order_no: str = Field(description="订单号，形如 ORD2026060001")
    reason: str = Field(description="退款原因", min_length=2)
    amount: float = Field(description="退款金额（元）", gt=0)


class CreateRefundResult(BaseModel):
    # created: 退款单已创建；escalated: 触发护栏已自动转人工；rejected: 校验不通过
    status: Literal["created", "escalated", "rejected"]
    refund_no: str | None = None
    ticket_no: str | None = Field(
        default=None, description="status=escalated 时自动创建的工单号"
    )
    message: str


# ---------------------------------------------------------------------------
# 8. escalate_to_human
# ---------------------------------------------------------------------------


class EscalateToHumanInput(BaseModel):
    summary: str = Field(description="问题摘要，供人工客服快速了解上下文", min_length=5)
    priority: TicketPriority = Field(
        description="工单优先级：low / medium / high / urgent"
    )


class EscalateToHumanResult(BaseModel):
    ticket_no: str
    message: str
    # 该字段为 True 时 agent 循环会在本轮结束后终止接管
    session_ended: bool = True
