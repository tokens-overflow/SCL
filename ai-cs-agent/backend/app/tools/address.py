from backend.app.agent.state import SessionState
from backend.app.db.session import get_session
from backend.app.domain.schemas import UpdateShippingAddressInput, UpdateShippingAddressResult
from backend.app.services.order_service import update_shipping_address as update_shipping_address_service
from backend.app.tools.registry import tool


@tool("update_shipping_address", "Update shipping address for a pending-shipment order.", UpdateShippingAddressInput)
def update_shipping_address(
    state: SessionState, p: UpdateShippingAddressInput
) -> UpdateShippingAddressResult:
    session = get_session()
    try:
        return update_shipping_address_service(session, state, p.order_no, p.new_address)
    finally:
        session.close()
