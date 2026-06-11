from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.domain.models import Ticket


def count_tickets(session: Session) -> int:
    return len(session.scalars(select(Ticket.ticket_no)).all())


def add_ticket(session: Session, ticket: Ticket) -> Ticket:
    session.add(ticket)
    session.flush()
    return ticket
