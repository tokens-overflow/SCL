from sqlalchemy.orm import Session

from backend.app.agent.state import SessionState
from backend.app.domain.schemas import GetUserProfileInput, UserProfileResult
from backend.app.services.identity_service import get_user_profile as get_user_profile_service
from backend.app.tools.registry import tool


@tool(
    "get_user_profile",
    "查询已核身用户的资料（姓名、会员等级、注册时间等）。需要先核身。",
    GetUserProfileInput,
)
def get_user_profile(session: Session, state: SessionState, p: GetUserProfileInput) -> UserProfileResult:
    return get_user_profile_service(session, state, p.user_id)
