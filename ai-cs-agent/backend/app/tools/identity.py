from backend.app.agent.state import SessionState
from backend.app.db.session import get_session
from backend.app.domain.schemas import VerifyIdentityInput, VerifyIdentityResult
from backend.app.services.identity_service import verify_identity as verify_identity_service
from backend.app.tools.registry import tool


@tool(
    "verify_identity",
    "通过注册手机号核验用户身份。在查询/操作任何账户数据之前必须先调用本工具完成核身。"
    "核身成功后会返回 user_id，后续工具调用使用该 ID。",
    VerifyIdentityInput,
)
def verify_identity(state: SessionState, p: VerifyIdentityInput) -> VerifyIdentityResult:
    session = get_session()
    try:
        return verify_identity_service(session, state, p.phone)
    finally:
        session.close()
