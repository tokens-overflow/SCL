from sqlalchemy.orm import Session

from backend.app.agent.state import SessionState
from backend.app.domain.schemas import UpdateShippingAddressInput, UpdateShippingAddressResult
from backend.app.services.order_service import (
    update_shipping_address as update_shipping_address_service,
)
from backend.app.tools.registry import tool


@tool(
    "update_shipping_address",
    "修改订单收货地址。仅限「待发货」状态的订单；已发货订单会被护栏拦截。"
    "这是写操作：执行前必须先向用户复述新地址并获得确认。需要先核身。",
    UpdateShippingAddressInput,
)
def update_shipping_address(
    session: Session, state: SessionState, p: UpdateShippingAddressInput
) -> UpdateShippingAddressResult:
    return update_shipping_address_service(session, state, p.order_no, p.new_address)
