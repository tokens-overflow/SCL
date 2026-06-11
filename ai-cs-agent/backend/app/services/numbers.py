from datetime import datetime

from sqlalchemy.orm import Session

from backend.app.repositories.refunds import count_refunds
from backend.app.repositories.tickets import count_tickets


def next_refund_no(session: Session) -> str:
    ym = datetime.now().strftime("%Y%m")
    return f"RFD{ym}{count_refunds(session) + 1:04d}"


def next_ticket_no(session: Session) -> str:
    ym = datetime.now().strftime("%Y%m")
    return f"TKT{ym}{count_tickets(session) + 1:04d}"
