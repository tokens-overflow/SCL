"""业务护栏：代码硬校验层。

System prompt 里的约束是"软护栏"——模型通常会遵守，但不应依赖它。
这里的每一条校验都在工具执行前强制生效，模型绕不过去：

1. 未核身 → 拒绝执行任何需要身份的工具
2. 越权访问 → user_id / 订单归属必须与已核身用户一致
3. 已发货订单 → 拒绝改地址
4. 退款金额 > REFUND_AUTO_ESCALATE_THRESHOLD → 不执行退款，自动转人工
"""
from dataclasses import dataclass, field

from backend.models import Order, OrderStatus

# 超过该金额的退款不允许 agent 自助处理，自动升级人工
REFUND_AUTO_ESCALATE_THRESHOLD = 200.0


@dataclass
class SessionState:
    """单个会话的运行时状态，agent 循环与工具执行器共享。"""
    session_id: str
    verified_user_id: int | None = None
    verified_user_name: str | None = None
    escalated: bool = False
    # 记录本轮触发过的护栏，便于前端展示
    triggered_guardrails: list[str] = field(default_factory=list)


class GuardrailViolation(Exception):
    """护栏拦截。message 会作为 is_error 的 tool_result 返回给模型，
    模型据此向用户解释并给出替代方案。"""

    def __init__(self, rule: str, message: str):
        self.rule = rule
        super().__init__(message)


def require_verified(state: SessionState) -> int:
    """硬校验 1：未核身一律拒绝。返回已核身的 user_id。"""
    if state.verified_user_id is None:
        state.triggered_guardrails.append("require_verified")
        raise GuardrailViolation(
            "require_verified",
            "护栏拦截：用户尚未完成身份核验，禁止查询或操作任何账户数据。"
            "请先引导用户提供注册手机号，调用 verify_identity 完成核身。",
        )
    return state.verified_user_id


def require_own_user(state: SessionState, user_id: int) -> None:
    """硬校验 2a：user_id 参数必须是当前已核身用户本人。"""
    verified_id = require_verified(state)
    if user_id != verified_id:
        state.triggered_guardrails.append("require_own_user")
        raise GuardrailViolation(
            "require_own_user",
            f"护栏拦截：user_id={user_id} 不是当前核身用户（user_id={verified_id}），"
            "禁止跨用户访问数据。",
        )


def require_own_order(state: SessionState, order: Order | None, order_no: str) -> Order:
    """硬校验 2b：订单必须存在且归属于当前已核身用户。"""
    verified_id = require_verified(state)
    if order is None:
        raise GuardrailViolation(
            "order_not_found",
            f"订单 {order_no} 不存在，请确认订单号是否正确。",
        )
    if order.user_id != verified_id:
        state.triggered_guardrails.append("require_own_order")
        raise GuardrailViolation(
            "require_own_order",
            f"护栏拦截：订单 {order_no} 不属于当前核身用户，禁止跨用户访问。",
        )
    return order


def check_address_change_allowed(state: SessionState, order: Order) -> None:
    """硬校验 3：仅待发货订单允许修改收货地址。"""
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
    """硬校验 4a：退款基础校验（状态、金额上限）。
    金额阈值校验不在这里——超阈值走自动转人工，见 tools.create_refund。"""
    if order.status == OrderStatus.REFUNDED:
        raise GuardrailViolation(
            "already_refunded",
            f"订单 {order.order_no} 已是退款状态，无法重复退款。",
        )
    if order.status == OrderStatus.PENDING_SHIPMENT:
        raise GuardrailViolation(
            "refund_before_shipment",
            f"订单 {order.order_no} 尚未发货。未发货订单请直接取消订单（当前 demo "
            "未提供取消工具，请转人工处理），无需走退款流程。",
        )
    if amount > order.amount:
        state.triggered_guardrails.append("refund_amount_exceeds_order")
        raise GuardrailViolation(
            "refund_amount_exceeds_order",
            f"护栏拦截：退款金额 ¥{amount:.2f} 超过订单实付金额 ¥{order.amount:.2f}。",
        )


def refund_needs_escalation(amount: float) -> bool:
    """硬校验 4b：是否超过自助退款阈值。"""
    return amount > REFUND_AUTO_ESCALATE_THRESHOLD
