"""8 个 CRM 工具 + 工具注册表。

核心技巧：工具的 JSON Schema 不手写，直接从 Pydantic 入参模型生成；
执行时入参先过 Pydantic 校验，出参也是 Pydantic 模型统一序列化。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Type

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from backend import guardrails
from backend.db import get_session
from backend.guardrails import GuardrailViolation, SessionState
from backend.models import Order, OrderStatus, Refund, RefundStatus, Ticket, User
from backend.schemas import (
    CreateRefundInput,
    CreateRefundResult,
    EscalateToHumanInput,
    EscalateToHumanResult,
    GetLogisticsInput,
    GetLogisticsResult,
    GetOrderDetailInput,
    GetUserProfileInput,
    ListOrdersInput,
    ListOrdersResult,
    LogisticsEvent,
    OrderDetailResult,
    OrderSummary,
    UpdateShippingAddressInput,
    UpdateShippingAddressResult,
    UserProfileResult,
    VerifyIdentityInput,
    VerifyIdentityResult,
)

# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------


@dataclass
class ToolDef:
    name: str
    description: str
    input_model: Type[BaseModel]
    handler: Callable[[SessionState, BaseModel], BaseModel]


TOOL_REGISTRY: dict[str, ToolDef] = {}


def tool(name: str, description: str, input_model: Type[BaseModel]):
    """注册一个工具。handler 签名：(state, params) -> 出参模型"""

    def decorator(fn):
        TOOL_REGISTRY[name] = ToolDef(name, description, input_model, fn)
        return fn

    return decorator


def _clean_schema(schema: dict) -> dict:
    """去掉 Pydantic 生成的 title 噪音，让发给模型的 schema 更干净。"""
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    for defn in schema.get("$defs", {}).values():
        defn.pop("title", None)
    return schema


def get_anthropic_tools() -> list[dict[str, Any]]:
    """生成 Anthropic Messages API 的 tools 参数。"""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": _clean_schema(t.input_model.model_json_schema()),
        }
        for t in TOOL_REGISTRY.values()
    ]


def execute_tool(state: SessionState, name: str, raw_input: dict) -> tuple[str, bool]:
    """执行工具，返回 (结果 JSON 字符串, is_error)。

    - 入参经 Pydantic 校验，校验失败的错误信息原样回给模型让它修正重试
    - GuardrailViolation 作为错误返回，模型据此向用户解释
    """
    tool_def = TOOL_REGISTRY.get(name)
    if tool_def is None:
        return f"未知工具：{name}", True
    try:
        params = tool_def.input_model.model_validate(raw_input)
    except ValidationError as e:
        return f"入参校验失败，请修正后重试：{e}", True
    try:
        result = tool_def.handler(state, params)
        return result.model_dump_json(), False
    except GuardrailViolation as e:
        return str(e), True
    except Exception as e:  # 数据库等运行时错误，让模型有机会重试
        return f"工具执行出错：{type(e).__name__}: {e}", True


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


def _mask_phone(phone: str) -> str:
    return phone[:3] + "****" + phone[-4:]


@tool(
    "verify_identity",
    "通过注册手机号核验用户身份。在查询/操作任何账户数据之前必须先调用本工具完成核身。"
    "核身成功后会返回 user_id，后续工具调用使用该 ID。",
    VerifyIdentityInput,
)
def verify_identity(state: SessionState, p: VerifyIdentityInput) -> VerifyIdentityResult:
    session = get_session()
    try:
        user = session.scalar(select(User).where(User.phone == p.phone))
        if user is None:
            return VerifyIdentityResult(
                verified=False,
                message=f"手机号 {_mask_phone(p.phone)} 未注册，核身失败。"
                        "请用户确认手机号，或转人工处理。",
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
    finally:
        session.close()


@tool(
    "get_user_profile",
    "查询已核身用户的资料（姓名、会员等级、注册时间等）。需要先核身。",
    GetUserProfileInput,
)
def get_user_profile(state: SessionState, p: GetUserProfileInput) -> UserProfileResult:
    guardrails.require_own_user(state, p.user_id)
    session = get_session()
    try:
        user = session.get(User, p.user_id)
        return UserProfileResult(
            user_id=user.id,
            name=user.name,
            phone_masked=_mask_phone(user.phone),
            email=user.email,
            member_level=user.member_level,
            registered_at=user.created_at.strftime("%Y-%m-%d"),
        )
    finally:
        session.close()


@tool(
    "list_orders",
    "查询已核身用户的订单列表，可按状态筛选。需要先核身。",
    ListOrdersInput,
)
def list_orders(state: SessionState, p: ListOrdersInput) -> ListOrdersResult:
    guardrails.require_own_user(state, p.user_id)
    session = get_session()
    try:
        stmt = (
            select(Order)
            .where(Order.user_id == p.user_id)
            .order_by(Order.created_at.desc())
        )
        if p.status is not None:
            stmt = stmt.where(Order.status == p.status)
        orders = session.scalars(stmt).all()
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
    finally:
        session.close()


def _get_order(session, order_no: str) -> Order | None:
    return session.scalar(select(Order).where(Order.order_no == order_no))


@tool(
    "get_order_detail",
    "查询单个订单的详细信息（商品、金额、状态、收货地址、退款状态等）。需要先核身。",
    GetOrderDetailInput,
)
def get_order_detail(state: SessionState, p: GetOrderDetailInput) -> OrderDetailResult:
    session = get_session()
    try:
        order = guardrails.require_own_order(state, _get_order(session, p.order_no), p.order_no)
        refund = session.scalar(
            select(Refund).where(Refund.order_id == order.id).order_by(Refund.id.desc())
        )
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
    finally:
        session.close()


@tool(
    "get_logistics",
    "查询订单的物流轨迹（承运商、运单号、各节点时间线）。需要先核身。",
    GetLogisticsInput,
)
def get_logistics(state: SessionState, p: GetLogisticsInput) -> GetLogisticsResult:
    session = get_session()
    try:
        order = guardrails.require_own_order(state, _get_order(session, p.order_no), p.order_no)
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
    finally:
        session.close()


@tool(
    "update_shipping_address",
    "修改订单收货地址。仅限「待发货」状态的订单；已发货订单会被护栏拦截。"
    "这是写操作：执行前必须先向用户复述新地址并获得确认。需要先核身。",
    UpdateShippingAddressInput,
)
def update_shipping_address(
    state: SessionState, p: UpdateShippingAddressInput
) -> UpdateShippingAddressResult:
    session = get_session()
    try:
        order = guardrails.require_own_order(state, _get_order(session, p.order_no), p.order_no)
        guardrails.check_address_change_allowed(state, order)
        old = order.shipping_address
        order.shipping_address = p.new_address
        session.commit()
        return UpdateShippingAddressResult(
            success=True,
            order_no=order.order_no,
            old_address=old,
            new_address=p.new_address,
            message="收货地址修改成功。",
        )
    finally:
        session.close()


@tool(
    "create_refund",
    "为订单创建退款单。这是写操作：执行前必须先向用户复述订单号、金额、原因并获得确认。"
    f"金额超过 {guardrails.REFUND_AUTO_ESCALATE_THRESHOLD:.0f} 元的退款不会执行，"
    "系统会自动创建人工工单（返回 status=escalated）。需要先核身。",
    CreateRefundInput,
)
def create_refund(state: SessionState, p: CreateRefundInput) -> CreateRefundResult:
    session = get_session()
    try:
        order = guardrails.require_own_order(state, _get_order(session, p.order_no), p.order_no)
        guardrails.check_refund_basic(state, order, p.amount)

        # 硬护栏：大额退款不执行，代码直接创建工单（不依赖模型自觉调用 escalate）
        if guardrails.refund_needs_escalation(p.amount):
            state.triggered_guardrails.append("refund_amount_threshold")
            ticket = _create_ticket(
                session,
                user_id=state.verified_user_id,
                summary=f"大额退款申请需人工审核：订单 {p.order_no}，"
                        f"金额 ¥{p.amount:.2f}，原因：{p.reason}",
                priority="high",
            )
            session.commit()
            state.escalated = True
            return CreateRefundResult(
                status="escalated",
                ticket_no=ticket.ticket_no,
                message=f"退款金额 ¥{p.amount:.2f} 超过自助上限 "
                        f"¥{guardrails.REFUND_AUTO_ESCALATE_THRESHOLD:.0f}，已自动创建"
                        f"人工工单 {ticket.ticket_no}（高优先级），人工客服将在 24 小时内"
                        "联系用户。请向用户说明情况并结束本次接待。",
            )

        refund = Refund(
            refund_no=_next_no(session, Refund, "refund_no", "RFD"),
            order_id=order.id,
            user_id=order.user_id,
            amount=p.amount,
            reason=p.reason,
            status=RefundStatus.PENDING,
        )
        order.status = OrderStatus.REFUNDED
        session.add(refund)
        session.commit()
        return CreateRefundResult(
            status="created",
            refund_no=refund.refund_no,
            message=f"退款单 {refund.refund_no} 创建成功（¥{p.amount:.2f}），"
                    "预计 1-3 个工作日原路退回。",
        )
    finally:
        session.close()


@tool(
    "escalate_to_human",
    "创建人工工单并结束 agent 接待。适用场景：用户明确要求人工、表达强烈不满、"
    "提到投诉或法律途径、或问题超出现有工具能力。未核身用户也可以转人工。",
    EscalateToHumanInput,
)
def escalate_to_human(state: SessionState, p: EscalateToHumanInput) -> EscalateToHumanResult:
    session = get_session()
    try:
        ticket = _create_ticket(
            session,
            user_id=state.verified_user_id,
            summary=p.summary,
            priority=p.priority.value,
        )
        session.commit()
        state.escalated = True
        return EscalateToHumanResult(
            ticket_no=ticket.ticket_no,
            message=f"工单 {ticket.ticket_no} 已创建（{p.priority.value}），"
                    "人工客服将尽快跟进。请向用户告知工单号并礼貌收尾。",
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _next_no(session, model, field: str, prefix: str) -> str:
    """生成业务单号：前缀 + 年月 + 4 位序号"""
    ym = datetime.now().strftime("%Y%m")
    count = len(session.scalars(select(getattr(model, field))).all())
    return f"{prefix}{ym}{count + 1:04d}"


def _create_ticket(session, user_id: int | None, summary: str, priority: str) -> Ticket:
    from backend.models import TicketPriority

    ticket = Ticket(
        ticket_no=_next_no(session, Ticket, "ticket_no", "TKT"),
        user_id=user_id,
        summary=summary,
        priority=TicketPriority(priority),
    )
    session.add(ticket)
    session.flush()
    return ticket
