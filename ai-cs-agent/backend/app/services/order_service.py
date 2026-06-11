from sqlalchemy.orm import Session

from backend.app.agent.state import SessionState
from backend.app.domain.enums import OrderStatus
from backend.app.domain.schemas import (
    GetLogisticsResult,
    ListOrdersResult,
    LogisticsEvent,
    OrderDetailResult,
    OrderSummary,
    UpdateShippingAddressResult,
    UserProfileResult,
    VerifyIdentityResult,
)
from backend.app.repositories.orders import get_order_by_no, list_orders_for_user
from backend.app.repositories.refunds import get_latest_refund_for_order
from backend.app.repositories.users import get_user_by_id, get_user_by_phone
from backend.app.services import guardrails


def mask_phone(phone: str) -> str:
    return phone[:3] + "****" + phone[-4:]


def verify_identity(session: Session, state: SessionState, phone: str) -> VerifyIdentityResult:
    user = get_user_by_phone(session, phone)
    if user is None:
        return VerifyIdentityResult(
            verified=False,
            message=f"手机号 {mask_phone(phone)} 未注册，核身失败。请用户确认手机号，或转人工处理。",
        )
    state.verified_user_id = user.id
    state.verified_user_name = user.name
    return VerifyIdentityResult(
        verified=True,
        user_id=user.id,
        name=user.name,
        member_level=user.member_level,
        message=f"核身成功，用户：{user.name}（{user.member_level.value} 会员）",
    )


def get_user_profile(session: Session, state: SessionState, user_id: int) -> UserProfileResult:
    guardrails.require_own_user(state, user_id)
    user = get_user_by_id(session, user_id)
    return UserProfileResult(
        user_id=user.id,
        name=user.name,
        phone_masked=mask_phone(user.phone),
        email=user.email,
        member_level=user.member_level,
        registered_at=user.created_at.strftime("%Y-%m-%d"),
    )


def list_orders(
    session: Session,
    state: SessionState,
    user_id: int,
    status: OrderStatus | None = None,
) -> ListOrdersResult:
    guardrails.require_own_user(state, user_id)
    orders = list_orders_for_user(session, user_id, status)
    return ListOrdersResult(
        total=len(orders),
        orders=[
            OrderSummary(
                order_no=o.order_no,
                product_name=o.product_name,
                quantity=o.quantity,
                amount=o.amount,
                status=o.status,
                created_at=o.created_at.strftime("%Y-%m-%d %H:%M"),
            )
            for o in orders
        ],
    )


def get_order_detail(session: Session, state: SessionState, order_no: str) -> OrderDetailResult:
    order = guardrails.require_own_order(state, get_order_by_no(session, order_no), order_no)
    refund = get_latest_refund_for_order(session, order.id)
    return OrderDetailResult(
        order_no=order.order_no,
        product_name=order.product_name,
        quantity=order.quantity,
        amount=order.amount,
        status=order.status,
        shipping_address=order.shipping_address,
        carrier=order.carrier,
        tracking_no=order.tracking_no,
        created_at=order.created_at.strftime("%Y-%m-%d %H:%M"),
        refund_status=refund.status.value if refund else None,
    )


def get_logistics(session: Session, state: SessionState, order_no: str) -> GetLogisticsResult:
    order = guardrails.require_own_order(state, get_order_by_no(session, order_no), order_no)
    events = [LogisticsEvent(**e) for e in (order.logistics_events or [])]
    if order.status == OrderStatus.PENDING_SHIPMENT:
        message = "订单尚未发货，暂无物流信息。"
    else:
        message = f"共 {len(events)} 条物流记录，最新：{events[-1].description}" if events else "暂无轨迹记录。"
    return GetLogisticsResult(
        order_no=order.order_no,
        status=order.status,
        carrier=order.carrier,
        tracking_no=order.tracking_no,
        events=events,
        message=message,
    )


def update_shipping_address(
    session: Session,
    state: SessionState,
    order_no: str,
    new_address: str,
) -> UpdateShippingAddressResult:
    order = guardrails.require_own_order(state, get_order_by_no(session, order_no), order_no)
    guardrails.check_address_change_allowed(state, order)
    old = order.shipping_address
    order.shipping_address = new_address
    session.commit()
    return UpdateShippingAddressResult(
        success=True,
        order_no=order.order_no,
        old_address=old,
        new_address=new_address,
        message="收货地址修改成功。",
    )
