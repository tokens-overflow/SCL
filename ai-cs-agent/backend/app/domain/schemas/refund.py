"""Refund tool schemas."""

from typing import Literal

from pydantic import BaseModel, Field


class CreateRefundInput(BaseModel):
    order_no: str = Field(description="订单号，形如 ORD2026060001")
    reason: str = Field(description="退款原因", min_length=2)
    amount: float = Field(description="退款金额（元）", gt=0)


class CreateRefundResult(BaseModel):
    status: Literal["created", "escalated", "rejected"]
    refund_no: str | None = None
    ticket_no: str | None = Field(
        default=None, description="status=escalated 时自动创建的工单号"
    )
    message: str
