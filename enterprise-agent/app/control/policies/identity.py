"""IdentityPolicy：身份有效性检查。

这是策略链的**第一道**。它不判断「能不能做这件事」，
只判断「你是谁这件事本身站不站得住」：

* 用户存在且有角色吗？
* Agent 存在且被启用了吗？
* 工具背后的服务账号能用吗？

放在第一道的原因很实际：后面所有策略的判断都建立在身份之上。
如果身份本身就不可信，后面跑再多规则也是在错误的前提上做推理。
"""

from __future__ import annotations

from app.control.models import PolicyEvaluationRequest, PolicyEvaluationResult
from app.core.enums import RiskLevel


class IdentityPolicy:
    """校验三种身份的有效性。"""

    name = "IdentityPolicy"

    async def evaluate(self, request: PolicyEvaluationRequest) -> PolicyEvaluationResult:
        """评估身份有效性。

        Returns:
            身份不完整时返回 DENY；正常时返回 ALLOW。

        Note:
            「用户不存在」和「用户无权限」这里都归结为同一个
            reason_code（``IDENTITY_INVALID`` / ``PERMISSION_INSUFFICIENT``），
            对外话术也应保持统一——
            两种不同的错误提示会泄漏「这个用户是否存在」这类信息。
        """
        identity = request.identity

        if not identity.user.user_id:
            return PolicyEvaluationResult.deny(
                self.name,
                "IDENTITY_INVALID",
                "无法识别操作人身份",
                risk_level=RiskLevel.HIGH,
            )

        # 零权限用户：MockIdentityProvider 对未知用户返回的就是这种身份。
        # 统一按「权限不足」处理，路径唯一、审计一致。
        if not identity.user.roles and not identity.user.permissions:
            return PolicyEvaluationResult.deny(
                self.name,
                "IDENTITY_NO_ROLE",
                "操作人未分配任何角色，无法执行业务动作",
                risk_level=RiskLevel.HIGH,
                metadata={"user_id": identity.user.user_id},
            )

        if not identity.agent.agent_id:
            return PolicyEvaluationResult.deny(
                self.name,
                "AGENT_IDENTITY_INVALID",
                "无法识别 Agent 身份",
                risk_level=RiskLevel.HIGH,
            )

        # Agent 没有任何权限 = 未被正确授权。默认拒绝而不是默认放行。
        if not identity.agent.permissions and not identity.agent.allowed_tools:
            return PolicyEvaluationResult.deny(
                self.name,
                "AGENT_NOT_PROVISIONED",
                "该 Agent 尚未被授权任何能力",
                risk_level=RiskLevel.HIGH,
                metadata={"agent_id": identity.agent.agent_id},
            )

        # 服务账号是只读的，却要执行写操作 —— 三方交集里的第三方在这里发挥作用。
        if request.tool_is_write and identity.service is not None and identity.service.read_only:
            return PolicyEvaluationResult.deny(
                self.name,
                "SERVICE_ACCOUNT_READ_ONLY",
                f"工具 {request.tool_name} 的服务账号为只读，不能执行写操作",
                risk_level=RiskLevel.HIGH,
                metadata={"service_id": identity.service.service_id},
            )

        return PolicyEvaluationResult.allow(self.name)
