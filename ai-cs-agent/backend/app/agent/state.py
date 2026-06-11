from dataclasses import dataclass, field


@dataclass
class SessionState:
    """Runtime state shared by the agent loop and CRM tools."""

    session_id: str
    verified_user_id: int | None = None
    verified_user_name: str | None = None
    escalated: bool = False
    triggered_guardrails: list[str] = field(default_factory=list)
