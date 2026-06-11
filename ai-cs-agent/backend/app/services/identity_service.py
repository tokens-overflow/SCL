"""Identity and user-profile business logic (核身 + 用户资料)。"""
from sqlalchemy.orm import Session

from backend.app.agent.state import SessionState
from backend.app.domain.schemas import (
    UserProfileResult,
    VerifyIdentityResult,
)
from backend.app.repositories.users import get_user_by_id, get_user_by_phone
from backend.app.services import guardrails


def mask_phone(phone: str) -> str:
    """脱敏手机号：138****0001。"""
    return phone[:3] + "****" + phone[-4:]


def verify_identity(session: Session, state: SessionState, phone: str) -> VerifyIdentityResult:
    """手机号核身。成功后把 user_id/name 记入会话状态。"""
    user = get_user_by_phone(session, phone)
    if user is None:
        return VerifyIdentityResult(
            verified=False,
            message=f"手机号 {mask_phone(phone)} 未注册，核身失败。请用户确认手机号，或转人工处理。",
        )
    state.verified_user_id = user.id
    state.verified_user_name = user.name
    return VerifyIdentityResult(
        verified=True,
        user_id=user.id,
        name=user.name,
        member_level=user.member_level,
        message=f"核身成功，用户：{user.name}（{user.member_level.value} 会员）",
    )


def get_user_profile(session: Session, state: SessionState, user_id: int) -> UserProfileResult:
    """查询已核身用户本人资料。"""
    guardrails.require_own_user(state, user_id)
    user = get_user_by_id(session, user_id)
    return UserProfileResult(
        user_id=user.id,
        name=user.name,
        phone_masked=mask_phone(user.phone),
        email=user.email,
        member_level=user.member_level,
        registered_at=user.created_at.strftime("%Y-%m-%d"),
    )
