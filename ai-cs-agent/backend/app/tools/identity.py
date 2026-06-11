from backend.app.agent.state import SessionState
from backend.app.db.session import get_session
from backend.app.domain.schemas import VerifyIdentityInput, VerifyIdentityResult
from backend.app.services.order_service import verify_identity as verify_identity_service
from backend.app.tools.registry import tool


@tool("verify_identity", "Check registered phone and return user id.", VerifyIdentityInput)
def verify_identity(state: SessionState, p: VerifyIdentityInput) -> VerifyIdentityResult:
    session = get_session()
    try:
        return verify_identity_service(session, state, p.phone)
    finally:
        session.close()
