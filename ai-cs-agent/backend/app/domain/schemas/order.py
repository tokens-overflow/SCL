"""Order, logistics, and address update tool schemas."""

from pydantic import BaseModel, Field

from backend.app.domain.enums import OrderStatus


class ListOrdersInput(BaseModel):
    user_id: int = Field(description="核身后获得的用户 ID")
    status: OrderStatus | None = Field(
        default=None,
        description=(
            "可选，按状态筛选：pending_shipment 待发货 / in_transit 运输中 / "
            "delivered 已签收 / refunded 已退款"
        ),
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


class UpdateShippingAddressInput(BaseModel):
    order_no: str = Field(description="订单号，形如 ORD2026060001")
    new_address: str = Field(description="新的收货地址（省市区 + 详细地址）", min_length=8)


class UpdateShippingAddressResult(BaseModel):
    success: bool
    order_no: str
    old_address: str | None = None
    new_address: str | None = None
    message: str
