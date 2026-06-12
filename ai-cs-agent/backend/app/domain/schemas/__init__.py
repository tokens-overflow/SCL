"""Tool input/output schemas grouped by business domain."""

from backend.app.domain.schemas.identity import VerifyIdentityInput, VerifyIdentityResult
from backend.app.domain.schemas.user import GetUserProfileInput, UserProfileResult
from backend.app.domain.schemas.order import (
    CancelOrderInput,
    CancelOrderResult,
    GetOrderDetailInput,
    GetLogisticsInput,
    GetLogisticsResult,
    ListOrdersInput,
    ListOrdersResult,
    LogisticsEvent,
    OrderDetailResult,
    OrderSummary,
    UpdateShippingAddressInput,
    UpdateShippingAddressResult,
)
from backend.app.domain.schemas.refund import CreateRefundInput, CreateRefundResult
from backend.app.domain.schemas.ticket import EscalateToHumanInput, EscalateToHumanResult

__all__ = [
    "VerifyIdentityInput",
    "VerifyIdentityResult",
    "GetUserProfileInput",
    "UserProfileResult",
    "ListOrdersInput",
    "OrderSummary",
    "ListOrdersResult",
    "GetOrderDetailInput",
    "OrderDetailResult",
    "GetLogisticsInput",
    "LogisticsEvent",
    "GetLogisticsResult",
    "UpdateShippingAddressInput",
    "UpdateShippingAddressResult",
    "CancelOrderInput",
    "CancelOrderResult",
    "CreateRefundInput",
    "CreateRefundResult",
    "EscalateToHumanInput",
    "EscalateToHumanResult",
]
