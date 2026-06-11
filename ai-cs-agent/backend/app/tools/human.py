from backend.app.agent.state import SessionState
from backend.app.db.session import get_session
from backend.app.domain.schemas import EscalateToHumanInput, EscalateToHumanResult
from backend.app.services.ticket_service import escalate_to_human as escalate_to_human_service
from backend.app.tools.registry import tool


@tool(
    "escalate_to_human",
    "创建人工工单并结束 agent 接待。适用场景：用户明确要求人工、表达强烈不满、"
    "提到投诉或法律途径、或问题超出现有工具能力。未核身用户也可以转人工。",
    EscalateToHumanInput,
)
def escalate_to_human(state: SessionState, p: EscalateToHumanInput) -> EscalateToHumanResult:
    session = get_session()
    try:
        return escalate_to_human_service(session, state, p.summary, p.priority)
    finally:
        session.close()
