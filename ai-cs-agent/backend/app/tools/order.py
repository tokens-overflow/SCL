from sqlalchemy.orm import Session

from backend.app.agent.state import SessionState
from backend.app.domain.schemas import (
    CancelOrderInput,
    CancelOrderResult,
    GetOrderDetailInput,
    ListOrdersInput,
    ListOrdersResult,
    OrderDetailResult,
)
from backend.app.services.order_service import (
    cancel_order as cancel_order_service,
    get_order_detail as get_order_detail_service,
    list_orders as list_orders_service,
)
from backend.app.tools.registry import tool


@tool(
    "list_orders",
    "查询已核身用户的订单列表，可按状态筛选。需要先核身。",
    ListOrdersInput,
)
def list_orders(session: Session, state: SessionState, p: ListOrdersInput) -> ListOrdersResult:
    return list_orders_service(session, state, p.user_id, p.status)


@tool(
    "get_order_detail",
    "查询单个订单的详细信息（商品、金额、状态、收货地址、退款状态等）。需要先核身。",
    GetOrderDetailInput,
)
def get_order_detail(session: Session, state: SessionState, p: GetOrderDetailInput) -> OrderDetailResult:
    return get_order_detail_service(session, state, p.order_no)


@tool(
    "cancel_order",
    "取消「待发货」状态的订单，已支付金额原路退回。已发货订单无法取消（会被护栏拦截），"
    "应引导用户签收后走退款流程。这是写操作：执行前必须先向用户复述订单号、商品并获得确认。需要先核身。",
    CancelOrderInput,
)
def cancel_order(session: Session, state: SessionState, p: CancelOrderInput) -> CancelOrderResult:
    return cancel_order_service(session, state, p.order_no, p.reason)
