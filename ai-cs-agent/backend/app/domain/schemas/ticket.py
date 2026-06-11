"""Human escalation ticket tool schemas."""

from pydantic import BaseModel, Field

from backend.app.domain.enums import TicketPriority


class EscalateToHumanInput(BaseModel):
    summary: str = Field(description="问题摘要，供人工客服快速了解上下文", min_length=5)
    priority: TicketPriority = Field(
        description="工单优先级：low / medium / high / urgent"
    )


class EscalateToHumanResult(BaseModel):
    ticket_no: str
    message: str
    session_ended: bool = True
