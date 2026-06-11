from sqlalchemy.orm import Session

from backend.app.agent.state import SessionState
from backend.app.core.config import REFUND_AUTO_ESCALATE_THRESHOLD
from backend.app.domain.enums import OrderStatus, RefundStatus
from backend.app.domain.models import Refund
from backend.app.domain.schemas import CreateRefundResult
from backend.app.repositories.orders import get_order_by_no
from backend.app.repositories.refunds import add_refund
from backend.app.services import guardrails
from backend.app.services.numbers import next_refund_no
from backend.app.services.ticket_service import create_ticket


def create_refund(
    session: Session,
    state: SessionState,
    order_no: str,
    reason: str,
    amount: float,
) -> CreateRefundResult:
    order = guardrails.require_own_order(state, get_order_by_no(session, order_no), order_no)
    guardrails.check_refund_basic(state, order, amount)

    if guardrails.refund_needs_escalation(amount):
        state.triggered_guardrails.append("refund_amount_threshold")
        ticket = create_ticket(
            session,
            user_id=state.verified_user_id,
            summary=f"大额退款申请需审核：订单 {order_no}，金额 ¥{amount:.2f}，原因：{reason}",
            priority="high",
        )
        session.commit()
        state.escalated = True
        return CreateRefundResult(
            status="escalated",
            ticket_no=ticket.ticket_no,
            message=(
                f"退款金额 ¥{amount:.2f} 超过自助上限 ¥{REFUND_AUTO_ESCALATE_THRESHOLD:.0f}，"
                f"已创建工单 {ticket.ticket_no}。请向用户说明情况并结束本次接待。"
            ),
        )

    refund = Refund(
        refund_no=next_refund_no(session),
        order_id=order.id,
        user_id=order.user_id,
        amount=amount,
        reason=reason,
        status=RefundStatus.PENDING,
    )
    order.status = OrderStatus.REFUNDED
    add_refund(session, refund)
    session.commit()
    return CreateRefundResult(
        status="created",
        refund_no=refund.refund_no,
        message=f"退款单 {refund.refund_no} 创建成功（¥{amount:.2f}），预计 1-3 个工作日原路退回。",
    )
