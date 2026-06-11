from backend.app.agent.state import SessionState
from backend.app.core.config import REFUND_AUTO_ESCALATE_THRESHOLD
from backend.app.db.session import get_session
from backend.app.domain.schemas import CreateRefundInput, CreateRefundResult
from backend.app.services.refund_service import create_refund as create_refund_service
from backend.app.tools.registry import tool


@tool(
    "create_refund",
    "为订单创建退款单。这是写操作：执行前必须先向用户复述订单号、金额、原因并获得确认。"
    f"金额超过 {REFUND_AUTO_ESCALATE_THRESHOLD:.0f} 元的退款不会执行，"
    "系统会自动创建人工工单（返回 status=escalated）。需要先核身。",
    CreateRefundInput,
)
def create_refund(state: SessionState, p: CreateRefundInput) -> CreateRefundResult:
    session = get_session()
    try:
        return create_refund_service(session, state, p.order_no, p.reason, p.amount)
    finally:
        session.close()
