"""Hard business guardrails enforced before CRM mutations/queries."""

from backend.app.agent.state import SessionState
from backend.app.core.config import REFUND_AUTO_ESCALATE_THRESHOLD
from backend.app.domain.enums import OrderStatus
from backend.app.domain.models import Order


class GuardrailViolation(Exception):
    """Raised when a hard business rule blocks a tool call."""

    def __init__(self, rule: str, message: str):
        self.rule = rule
        super().__init__(message)


def require_verified(state: SessionState) -> int:
    if state.verified_user_id is None:
        state.triggered_guardrails.append("require_verified")
        raise GuardrailViolation(
            "require_verified",
            "护栏拦截：用户尚未完成身份核验，禁止查询或操作任何账户数据。"
            "请先引导用户提供注册手机号，调用 verify_identity 完成核身。",
        )
    return state.verified_user_id


def require_own_user(state: SessionState, user_id: int) -> None:
    verified_id = require_verified(state)
    if user_id != verified_id:
        state.triggered_guardrails.append("require_own_user")
        raise GuardrailViolation(
            "require_own_user",
            f"护栏拦截：user_id={user_id} 不是当前核身用户（user_id={verified_id}），"
            "禁止跨用户访问数据。",
        )


def require_own_order(state: SessionState, order: Order | None, order_no: str) -> Order:
    verified_id = require_verified(state)
    if order is None:
        raise GuardrailViolation("order_not_found", f"订单 {order_no} 不存在，请确认订单号是否正确。")
    if order.user_id != verified_id:
        state.triggered_guardrails.append("require_own_order")
        raise GuardrailViolation(
            "require_own_order",
            f"护栏拦截：订单 {order_no} 不属于当前核身用户，禁止跨用户访问。",
        )
    return order


def check_address_change_allowed(state: SessionState, order: Order) -> None:
    if order.status != OrderStatus.PENDING_SHIPMENT:
        state.triggered_guardrails.append("address_change_after_shipment")
        status_text = {
            OrderStatus.IN_TRANSIT: "运输中",
            OrderStatus.DELIVERED: "已签收",
            OrderStatus.REFUNDED: "已退款",
        }.get(order.status, order.status.value)
        raise GuardrailViolation(
            "address_change_after_shipment",
            f"护栏拦截：订单 {order.order_no} 当前状态为「{status_text}」，"
            "已发货的订单无法修改收货地址。请向用户解释原因，并提供替代方案："
            "①联系承运商尝试拦截改派；②签收后走退换货流程；③转人工处理。",
        )


def check_refund_basic(state: SessionState, order: Order, amount: float) -> None:
    if order.status == OrderStatus.REFUNDED:
        raise GuardrailViolation("already_refunded", f"订单 {order.order_no} 已是退款状态，无法重复退款。")
    if order.status == OrderStatus.PENDING_SHIPMENT:
        raise GuardrailViolation(
            "refund_before_shipment",
            f"订单 {order.order_no} 尚未发货。未发货订单请直接取消订单（当前 demo 未提供取消工具，请转人工处理），无需走退款流程。",
        )
    if amount > order.amount:
        state.triggered_guardrails.append("refund_amount_exceeds_order")
        raise GuardrailViolation(
            "refund_amount_exceeds_order",
            f"护栏拦截：退款金额 ¥{amount:.2f} 超过订单实付金额 ¥{order.amount:.2f}。",
        )


def refund_needs_escalation(amount: float) -> bool:
    return amount > REFUND_AUTO_ESCALATE_THRESHOLD
