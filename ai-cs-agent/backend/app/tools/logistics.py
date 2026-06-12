from sqlalchemy.orm import Session

from backend.app.agent.state import SessionState
from backend.app.domain.schemas import GetLogisticsInput, GetLogisticsResult
from backend.app.services.order_service import get_logistics as get_logistics_service
from backend.app.tools.registry import tool


@tool(
    "get_logistics",
    "查询订单的物流轨迹（承运商、运单号、各节点时间线）。需要先核身。",
    GetLogisticsInput,
)
def get_logistics(session: Session, state: SessionState, p: GetLogisticsInput) -> GetLogisticsResult:
    return get_logistics_service(session, state, p.order_no)
