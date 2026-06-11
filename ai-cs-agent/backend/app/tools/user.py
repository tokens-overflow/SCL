from backend.app.agent.state import SessionState
from backend.app.db.session import get_session
from backend.app.domain.schemas import GetUserProfileInput, UserProfileResult
from backend.app.services.order_service import get_user_profile as get_user_profile_service
from backend.app.tools.registry import tool


@tool("get_user_profile", "Get verified user's profile.", GetUserProfileInput)
def get_user_profile(state: SessionState, p: GetUserProfileInput) -> UserProfileResult:
    session = get_session()
    try:
        return get_user_profile_service(session, state, p.user_id)
    finally:
        session.close()
