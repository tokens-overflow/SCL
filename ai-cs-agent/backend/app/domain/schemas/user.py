"""User related tool schemas."""

from pydantic import BaseModel, Field

from backend.app.domain.enums import MemberLevel


class GetUserProfileInput(BaseModel):
    user_id: int = Field(description="核身后获得的用户 ID")


class UserProfileResult(BaseModel):
    user_id: int
    name: str
    phone_masked: str = Field(description="脱敏手机号，如 138****0001")
    email: str
    member_level: MemberLevel
    registered_at: str
