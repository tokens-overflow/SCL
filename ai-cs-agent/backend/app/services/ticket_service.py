from sqlalchemy.orm import Session

from backend.app.domain.enums import TicketPriority
from backend.app.domain.models import Ticket
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
