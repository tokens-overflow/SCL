"""Identity verification tool schemas."""

from pydantic import BaseModel, Field

from backend.app.domain.enums import MemberLevel


class VerifyIdentityInput(BaseModel):
    """核身入参"""

    phone: str = Field(description="用户注册手机号，11 位数字", pattern=r"^1\d{10}$")


class VerifyIdentityResult(BaseModel):
    """核身结果"""

    verified: bool
    user_id: int | None = None
    name: str | None = None
    member_level: MemberLevel | None = None
    message: str
