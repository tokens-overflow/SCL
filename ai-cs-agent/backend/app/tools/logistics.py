from backend.app.agent.state import SessionState
from backend.app.db.session import get_session
from backend.app.domain.schemas import GetLogisticsInput, GetLogisticsResult
from backend.app.services.order_service import get_logistics as get_logistics_service
from backend.app.tools.registry import tool


@tool("get_logistics", "Get logistics events for a verified user's order.", GetLogisticsInput)
def get_logistics(state: SessionState, p: GetLogisticsInput) -> GetLogisticsResult:
    session = get_session()
    try:
        return get_logistics_service(session, state, p.order_no)
    finally:
        session.close()
