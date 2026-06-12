from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.domain.models import Ticket


def latest_ticket_no(session: Session, prefix: str) -> str | None:
    return session.scalar(
        select(Ticket.ticket_no)
        .where(Ticket.ticket_no.like(f"{prefix}%"))
        .order_by(Ticket.ticket_no.desc())
        .limit(1)
    )


def add_ticket(session: Session, ticket: Ticket) -> Ticket:
    session.add(ticket)
    session.flush()
    return ticket
