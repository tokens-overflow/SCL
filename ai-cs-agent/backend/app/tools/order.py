from backend.app.agent.state import SessionState
from backend.app.db.session import get_session
from backend.app.domain.schemas import (
    GetOrderDetailInput,
    ListOrdersInput,
    ListOrdersResult,
    OrderDetailResult,
)
from backend.app.services.order_service import (
    get_order_detail as get_order_detail_service,
    list_orders as list_orders_service,
)
from backend.app.tools.registry import tool


@tool(
    "list_orders",
    "查询已核身用户的订单列表，可按状态筛选。需要先核身。",
    ListOrdersInput,
)
def list_orders(state: SessionState, p: ListOrdersInput) -> ListOrdersResult:
    session = get_session()
    try:
        return list_orders_service(session, state, p.user_id, p.status)
    finally:
        session.close()


@tool(
    "get_order_detail",
    "查询单个订单的详细信息（商品、金额、状态、收货地址、退款状态等）。需要先核身。",
    GetOrderDetailInput,
)
def get_order_detail(state: SessionState, p: GetOrderDetailInput) -> OrderDetailResult:
    session = get_session()
    try:
        return get_order_detail_service(session, state, p.order_no)
    finally:
        session.close()
