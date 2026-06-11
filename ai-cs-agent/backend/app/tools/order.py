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


@tool("list_orders", "List verified user's orders, optionally filtered by status.", ListOrdersInput)
def list_orders(state: SessionState, p: ListOrdersInput) -> ListOrdersResult:
    session = get_session()
    try:
        return list_orders_service(session, state, p.user_id, p.status)
    finally:
        session.close()


@tool("get_order_detail", "Get one verified user's order detail.", GetOrderDetailInput)
def get_order_detail(state: SessionState, p: GetOrderDetailInput) -> OrderDetailResult:
    session = get_session()
    try:
        return get_order_detail_service(session, state, p.order_no)
    finally:
        session.close()
