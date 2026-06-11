from sqlalchemy.orm import Session

from backend.app.agent.state import SessionState
from backend.app.domain.enums import TicketPriority
from backend.app.domain.models import Ticket
from backend.app.domain.schemas import EscalateToHumanResult
from backend.app.repositories.tickets import add_ticket
from backend.app.services.numbers import next_ticket_no


def create_ticket(
    session: Session,
    user_id: int | None,
    summary: str,
    priority: str | TicketPriority,
) -> Ticket:
    priority_value = priority.value if isinstance(priority, TicketPriority) else priority
    ticket = Ticket(
        ticket_no=next_ticket_no(session),
        user_id=user_id,
        summary=summary,
        priority=TicketPriority(priority_value),
    )
    return add_ticket(session, ticket)


def escalate_to_human(
    session: Session,
    state: SessionState,
    summary: str,
    priority: TicketPriority,
) -> EscalateToHumanResult:
    """建工单并结束 agent 接待。未核身用户也可调用。"""
    ticket = create_ticket(
        session,
        user_id=state.verified_user_id,
        summary=summary,
        priority=priority,
    )
    session.commit()
    state.escalated = True
    return EscalateToHumanResult(
        ticket_no=ticket.ticket_no,
        message=f"工单 {ticket.ticket_no} 已创建（{priority.value}），"
        "人工客服将尽快跟进。请向用户告知工单号并礼貌收尾。",
    )
